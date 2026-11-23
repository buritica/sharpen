#!/usr/bin/env python3
"""Run grumpy's review or imagine pass with no coding agent installed: one
model, over HTTP.

Usage:
  oneshot.py review|imagine [--provider openai|compatible] [--model ID]
                    [--worktree PATH] [--aspects NAME,...] [--jobs N]
                    [--timeout SECONDS] [--dry-run]

The no-agent path. A coding agent (Claude Code, Codex, pi) runs
`commands/review.md` or `commands/imagine.md` itself: it spawns one pass per
review aspect (or imagine simulation), reads files as it goes, aggregates,
saves the report and writes the receipt. This script does the same steps for
a model that has no tools, so "reviewed" means one thing on every path:

  1. Resolve the diff the way the command does (`diff_base.py`, the branch's
     own layer on a stack), capped at 200000 characters like the command.
  2. Build every prompt from the command at run time (`extract_review`,
     `extract_imagine`), so this path changes when the command changes. A
     command edit that breaks the extraction fails loudly here and in
     `tests/test_oneshot.py`; it never falls back to a prompt of its own.
  3. Send one call per aspect (in parallel: `--jobs`, 4 for openai and 1 for
     a compatible server, which usually serializes anyway), each with the
     command's own pass prompt: review's template filled in per aspect, or
     imagine's five simulation prompts (`happy-path`, `state`, `concurrency`,
     `observability`, `docs`, the names `--aspects` takes). Save what they
     returned to `.sharpen/grumpy/<branch>/<pass>-passes.jsonl`, then send one
     aggregation call with the command's audit discipline, aggregation step
     and report template. The model never runs git or writes a file.
  4. Save the report to `.sharpen/grumpy/<branch>/<pass>.md` with the
     `_Model:_` line and the base line (review's `_Base:_`, imagine's
     `_Analyzed:_`) this script writes itself (the model line names the
     aspects that ran), then write the same receipt any host writes
     (`receipt.py`), plus an `executor` naming this path, the provider, the
     model and the aspects. `record-gate.py --verify review` (or `imagine`)
     records gate 3 (or 5) from it, and `--status` shows that the gate came
     from this path.

What it can't do that an agent host does: open files beyond the changed
files handed to it, follow call sites, append the note to the sdlc plan
(review's Step 6, imagine's Step 5), or run simplify and fix at all. The diff and the changed
files' text are sent to the provider: with `openai` the code leaves this
machine; `compatible` pointed at a local server (Ollama, llama.cpp, vLLM, LM
Studio) keeps it here.

Providers (both speak OpenAI's chat-completions format; `PROVIDERS` is where
another one goes):
  openai      https://api.openai.com/v1/chat/completions, key in OPENAI_API_KEY.
              Default model: the `analysis` role's `openai` column in
              models.yaml for both passes (UNVERIFIED until the benchmark has
              measured it).
  compatible  SHARPEN_LLM_URL (a base URL like http://localhost:11434/v1, or
              the full .../chat/completions URL), optional SHARPEN_LLM_API_KEY.
              No default model: pass --model or set SHARPEN_LLM_MODEL.

--provider defaults to SHARPEN_LLM_PROVIDER, else openai. --model beats
SHARPEN_LLM_MODEL beats the map. Keys are read from the environment only and
never written to the report, the receipt or any log line; provider error text
is redacted before it is printed.

Exit codes: 0 report saved and receipt written. 1 the pass earned nothing:
a dirty tree, an empty diff, a git failure, any pass that failed or
answered without pass lines (prose, or nothing at all; the report is
saved so you can read it, stamped `_Incomplete:_`, but no receipt), an
unusable aggregation, HEAD moving during the run, or a refused receipt.
2 usage or configuration: unknown flag, provider or aspect, no key, no model,
a detached HEAD or the default branch, a provider that rejects the request
as unauthorized or unknown (400/401/403/404), or an extraction error.
Pure stdlib.
"""

import collections
import concurrent.futures
import http.client
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import diff_base  # noqa: E402
import receipt  # noqa: E402

PLUGIN_ROOT = os.path.dirname(HERE)
PASSES = ("review", "imagine")
DIFF_CAP = 200000  # the command's own cap (`| head -c 200000`)
FILES_CAP = 200000  # total text of changed files sent as context
FILE_CAP = 40000  # per file
EXECUTOR_HOST = "oneshot"
# `--jobs` when the flag is absent. A hosted API takes parallel calls; a local
# single-GPU server serializes prompt evaluation, so parallel ~100k-token
# prompts queue behind each other while every socket timeout keeps ticking.
DEFAULT_JOBS = {"openai": 4, "compatible": 1}
RETRY_WAIT_CAP = 60  # seconds: the most a Retry-After header can make us wait
# What the passes returned (`<pass>-passes.jsonl`), saved next to the report
# before the aggregation call so a failed aggregation loses nothing that was
# paid for.
PASSES_FILE = "{}-passes.jsonl"
# Imagine's five simulation passes: the name `--aspects` takes, and the start
# of the `### Agent N:` heading in imagine.md whose fence is that pass's
# prompt. A sixth agent in imagine.md stops the run until it is named here.
IMAGINE_AGENTS = (
    ("happy-path", "Agent 1: Happy Path"),
    ("state", "Agent 2: State Transitions"),
    ("concurrency", "Agent 3: Concurrency"),
    ("observability", "Agent 4: Observability"),
    ("docs", "Agent 5: Documentation"),
)

# One finding pass's outcome. `failure` is the call's error text (None when it
# answered); `errored` means it answered without finding lines; `dropped`
# counts malformed lines the parser threw away.
PassResult = collections.namedtuple("PassResult", "lines errored failure dropped")
Reply = collections.namedtuple("Reply", "text usage model request_id")

# grumpy's pass severities and the report section each one lands in on
# review. Both commands define the same three ("SEVERITY is one of CRIT ...,
# WARN ..., NOTE ..."; imagine names its sections differently); the
# extractors read them back and a test pins them together.
SEVERITY_SECTIONS = {
    "CRIT": "## Critical Issues",
    "WARN": "## Serious Concerns",
    "NOTE": "## Questionable Decisions",
}
FACTS = ("fact", "judgment")

PROVIDERS = {
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "key_env": "OPENAI_API_KEY",
        "key_required": True,
        "default_from_map": True,
    },
    "compatible": {
        "url_env": "SHARPEN_LLM_URL",
        "key_env": "SHARPEN_LLM_API_KEY",
        "key_required": False,
        "default_from_map": False,
    },
}

PREAMBLE = (
    "You are running one step of grumpy's code review as a single API call. "
    "You have no tools: you cannot read files, run git or shell commands, "
    "spawn agents, or write files, so skip any instruction that needs one. "
    "Everything you may use is in this message. Answer in the exact output "
    "format the instructions ask for, and nothing else. If you have nothing "
    "to report, answer with one short line saying so, never an empty message."
)


class ExtractionError(ValueError):
    """review.md no longer has a part this path builds its prompts from."""


class ProviderError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# A provider answering with one of these won't do better on a retry or for
# another pass: the key, the model or the URL is wrong. That's setup (exit 2).
SETUP_STATUSES = (400, 401, 403, 404)


# --- reading the command ----------------------------------------------------


def _strip_frontmatter(text):
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5 :]
    return text


def _headings(lines):
    """(index, level, title) for every markdown heading outside a code fence."""
    out, fenced = [], False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        m = None if fenced else re.match(r"^(#{1,6})\s+(.*\S)\s*$", line)
        if m:
            out.append((i, len(m.group(1)), m.group(2)))
    return out


def section(text, title, *, prefix=False):
    """The body of the heading named `title` (or starting with it, with
    `prefix`), up to the next heading of the same or a higher level. Headings
    inside code fences don't count, so a template's own `## Verdict` stays in.
    Raises ExtractionError when the heading isn't there."""
    lines = text.split("\n")
    heads = _headings(lines)
    for n, (i, level, name) in enumerate(heads):
        if name == title or (prefix and name.startswith(title)):
            end = len(lines)
            for j, lvl, _ in heads[n + 1 :]:
                if lvl <= level:
                    end = j
                    break
            return "\n".join(lines[i + 1 : end]).strip("\n")
    raise ExtractionError(f"heading {title!r} not found")


def _fence_after(text, marker):
    at = text.find(marker)
    if at == -1:
        raise ExtractionError(f"{marker!r} not found")
    m = re.search(r"```[^\n]*\n(.*?)\n```", text[at:], re.S)
    if not m:
        raise ExtractionError(f"no code fence after {marker!r}")
    return m.group(1)


def extract_review(text):
    """The parts of review.md this path sends, and nothing else.

    Taken: the persona (the text under `# Grumpy Review`), the aspect list
    (`### Available Review Aspects`), the finding-pass prompt template (the
    first fence after "Example agent prompt structure" in Step 3) and the
    severities it defines, `## Audit discipline`, all of Step 4 (settled
    findings and the report template) and `## Personality Guidelines`.

    Left out, because a single call can't do them and this script does them
    instead: level flags and worktree targeting (CLI flags here), Step 1's
    git commands (`resolve_diff`), Step 3's subagent dispatch and model
    routing (one call per aspect here), Step 5's save, Step 6's plan note
    (skipped), Step 7's receipt (`receipt.write_receipt`).
    """
    body = _strip_frontmatter(text)
    persona = section(body, "Grumpy Review").split("\n## ")[0].strip()
    if "grumpy principal engineer" not in persona:
        raise ExtractionError("persona no longer names the grumpy principal engineer")
    aspects = re.findall(
        r"^- \*\*([a-z]+)\*\* - (.+?)(?=\n- \*\*|\n\n|\Z)",
        section(body, "Available Review Aspects"),
        re.S | re.M,
    )
    if not any(name == "code" for name, _ in aspects):
        raise ExtractionError("aspect list has no `code` aspect")
    step3 = section(body, "Step 3:", prefix=True)
    template = _fence_after(step3, "Example agent prompt structure")
    for slot in ("[ASPECT]", "[DIFF_BASE]", "[DIFF_CONTENT]", "SEVERITY|file:line"):
        if slot not in template:
            raise ExtractionError(f"pass template lost {slot}")
    sentence = re.search(r"SEVERITY is one of (.+?)\.\n", template, re.S)
    if not sentence:
        raise ExtractionError("pass template no longer says which severities exist")
    severities = re.findall(r"\b([A-Z]{3,5}) \(", sentence.group(1))
    step4 = section(body, "Step 4:", prefix=True)
    for anchor in (
        "# Code Review:",
        "_Model:",
        "## Verdict",
        "Settled findings held back",
    ):
        if anchor not in step4:
            raise ExtractionError(f"Step 4 report template lost {anchor!r}")
    aspects = [(n, " ".join(d.split())) for n, d in aspects]
    audit = section(body, "Audit discipline")
    personality = section(body, "Personality Guidelines")
    return {
        "persona": persona,
        "aspects": aspects,
        "pass_template": template,
        "severities": severities,
        "audit": audit,
        "step4": step4,
        "personality": personality,
        # What `run` uses, the same shape for both passes: one prompt per
        # unit, the report's title, the aggregation sections in order.
        "units": [
            (n, d, template.replace("[ASPECT]", f"{n} ({d})")) for n, d in aspects
        ],
        "title": "# Code Review",
        "aggregate": [
            ("## Audit discipline", audit),
            ("## Step 4: Aggregate and Deliver the Verdict", step4),
            ("## Personality Guidelines", personality),
        ],
        "context_labels": [],
    }


def extract_imagine(text):
    """The parts of imagine.md this path sends, and nothing else.

    Taken: the persona (the text under `# Grumpy Imagine`), each of Step 2's
    five agent prompts (the first fence under each `### Agent N:` heading,
    named by `IMAGINE_AGENTS`), the severities Agent 1's prompt defines, the
    CONTEXT line labels the prompts define (`transport:`, `cleanup`, ...),
    `## Audit Discipline`, all of Step 3 (section mapping, settled findings
    and the report template) and `## Personality Guidelines`.

    Left out, as for review: level flags and worktree targeting, Step 1's
    git commands, Step 2's subagent dispatch, Step 4's save, Step 5's plan
    note and Step 6's receipt, which this script does or skips itself.
    """
    body = _strip_frontmatter(text)
    persona = section(body, "Grumpy Imagine").split("\n## ")[0].strip()
    if "grumpy principal engineer" not in persona:
        raise ExtractionError("persona no longer names the grumpy principal engineer")
    step2 = section(body, "Step 2:", prefix=True)
    heads = [t for _, lvl, t in _headings(step2.split("\n")) if lvl == 3]
    agents = [t for t in heads if re.match(r"Agent \d+:", t)]
    if len(agents) != len(IMAGINE_AGENTS):
        raise ExtractionError(
            f"Step 2 has {len(agents)} agent prompts, this path knows "
            f"{len(IMAGINE_AGENTS)}: update IMAGINE_AGENTS"
        )
    units, labels = [], []
    for name, start in IMAGINE_AGENTS:
        title = next((t for t in agents if t.startswith(start)), None)
        if title is None:
            raise ExtractionError(f"Step 2 lost the {start!r} agent")
        m = re.search(r"```[^\n]*\n(.*?)\n```", section(step2, title), re.S)
        if not m:
            raise ExtractionError(f"no prompt fence under {title!r}")
        prompt = m.group(1)
        for slot in ("[DIFF_BASE]", "[DIFF_CONTENT]", "SEVERITY|file:line"):
            if slot not in prompt:
                raise ExtractionError(f"{title!r} prompt lost {slot}")
        labels += re.findall(r"^`([a-z][a-z-]*)(?::\[Name\])?\|", prompt, re.M)
        units.append((name, title, prompt))
    sentence = re.search(r"SEVERITY is (.+?)\.\n", units[0][2], re.S)
    if not sentence:
        raise ExtractionError("Agent 1's prompt no longer says which severities exist")
    severities = re.findall(r"\b([A-Z]{3,5})\s+\(", sentence.group(1))
    for name, title, prompt in units:
        missing = [s for s in severities if s not in prompt]
        if missing:
            raise ExtractionError(f"{title!r} prompt doesn't name {missing}")
    if not labels:
        raise ExtractionError("no agent prompt defines a CONTEXT line any more")
    step3 = section(body, "Step 3:", prefix=True)
    for anchor in (
        "# Production Imagination:",
        "_Model:",
        "_Analyzed:",
        "## Verdict",
        "Settled findings held back",
    ):
        if anchor not in step3:
            raise ExtractionError(f"Step 3 report template lost {anchor!r}")
    return {
        "persona": persona,
        "units": units,
        "severities": severities,
        "context_labels": sorted(set(labels)),
        "title": "# Production Imagination",
        "aggregate": [
            ("## Audit Discipline", section(body, "Audit Discipline")),
            ("## Step 3: Aggregate and Deliver the Simulation Report", step3),
            ("## Personality Guidelines", section(body, "Personality Guidelines")),
        ],
    }


EXTRACTORS = {"review": extract_review, "imagine": extract_imagine}


def load_command(pass_name, plugin_root=PLUGIN_ROOT):
    path = os.path.join(plugin_root, "commands", f"{pass_name}.md")
    with open(path, encoding="utf-8") as f:
        parts = EXTRACTORS[pass_name](f.read())
    if parts["severities"] != list(SEVERITY_SECTIONS):
        raise ExtractionError(
            f"{pass_name}.md's severities are {parts['severities']}, this path "
            f"knows {list(SEVERITY_SECTIONS)}: update SEVERITY_SECTIONS"
        )
    return parts


# --- models.yaml --------------------------------------------------------------


def parse_models(text):
    """`roles:` -> {role: {column: value}} for models.yaml's one shape.
    Anything else in the file (comments, `escalation:`) is ignored; a line
    under `roles:` that isn't `role:` or `column: value` raises ValueError."""
    roles, current, inside = {}, None, False
    for raw in text.split("\n"):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            inside = line == "roles:"
            continue
        if not inside:
            continue
        key, sep, value = line.strip().partition(":")
        if not sep:
            raise ValueError(f"models.yaml: can't read {raw.strip()!r}")
        if indent == 2 and not value.strip():
            current = roles.setdefault(key, {})
        elif indent >= 4 and current is not None and value.strip():
            current[key] = value.strip().strip("'\"")
        else:
            raise ValueError(f"models.yaml: can't read {raw.strip()!r}")
    return roles


def model_from_map(worktree, column="openai", role="analysis"):
    """(model, note). The override at <worktree>/.sharpen/models.yaml wins per
    role and column, as the file's header says; a broken override is ignored
    and named in the note."""
    with open(os.path.join(PLUGIN_ROOT, "models.yaml"), encoding="utf-8") as f:
        model = parse_models(f.read()).get(role, {}).get(column)
    note = ""
    try:
        root = _git(worktree, "rev-parse", "--show-toplevel").strip()
    except (OSError, subprocess.CalledProcessError):
        root = worktree
    override = os.path.join(root, ".sharpen", "models.yaml")
    if os.path.exists(override):
        try:
            with open(override, encoding="utf-8") as f:
                value = parse_models(f.read()).get(role, {}).get(column)
            if value:
                model, note = value, f"override .sharpen/models.yaml {role}.{column}"
        except (OSError, ValueError) as e:
            note = f"override .sharpen/models.yaml ignored: {e}"
    return model, note


# --- the provider -------------------------------------------------------------

_KEYLIKE = re.compile(r"\b(?:sk|key|tok)-[A-Za-z0-9_\-*]{6,}")


def redact(text, secrets=()):
    for s in secrets:
        if s:
            text = text.replace(s, "[redacted]")
    return _KEYLIKE.sub("[redacted]", text)


def chat_url(base):
    base = base.rstrip("/")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


class ChatClient:
    """OpenAI's chat-completions wire format, which every provider here speaks.

    Only `model` and `messages` are sent: reasoning models reject sampling
    parameters, and a compatible server's defaults are the user's choice."""

    def __init__(self, provider, env=None):
        env = os.environ if env is None else env
        spec = PROVIDERS[provider]
        self.provider = provider
        self.url = spec.get("url") or env.get(spec.get("url_env", ""), "")
        if not self.url:
            raise ValueError(f"{spec['url_env']} is not set (the server's base URL)")
        self.url = chat_url(self.url)
        self._key = env.get(spec["key_env"], "")
        if spec["key_required"] and not self._key:
            raise ValueError(f"{spec['key_env']} is not set")

    def chat(self, model, messages, timeout=300, attempts=2, label="call"):
        """Reply(text, usage, model, request_id). `label` names the call in
        the retry line on stderr (the aspect, or "aggregation")."""
        body = json.dumps({"model": model, "messages": messages}).encode()
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        last, status, wait = None, None, None
        for attempt in range(attempts):
            req = urllib.request.Request(self.url, data=body, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.load(resp)
                    return Reply(_content(data), *_meta(data, resp.headers))
            except urllib.error.HTTPError as e:
                status = e.code
                what = "rate limited: " if e.code == 429 else ""
                last = f"{what}HTTP {e.code}: {_snippet(e)}"
                wait = _retry_after(e.headers)
                if e.code != 429 and e.code < 500:
                    break
            except (
                urllib.error.URLError,
                http.client.HTTPException,
                OSError,
                ValueError,
            ) as e:
                # HTTPException covers a truncated body (IncompleteRead) and a
                # dropped connection; ValueError a body that isn't JSON or an
                # answer cut short. All of them are worth one retry.
                status, last, wait = None, f"{type(e).__name__}: {e}", None
            if attempt + 1 < attempts:
                # The server's Retry-After when it sent one (capped), else a
                # short backoff; jitter so parallel passes don't retry as the
                # same burst that was just rejected.
                delay = (
                    wait if wait is not None else 2 * (attempt + 1)
                ) + random.random()
                _log(
                    f"{label}: attempt {attempt + 1} failed "
                    f"({redact(last, [self._key])}); retrying in {delay:.0f}s"
                )
                time.sleep(delay)
        raise ProviderError(redact(f"{self.provider}: {last}", [self._key]), status)


def _retry_after(headers):
    """Seconds the server asked us to wait, capped, or None. Only the
    delay-seconds form; an HTTP-date is rare from these APIs and falls back
    to the fixed wait."""
    try:
        value = int((headers or {}).get("Retry-After", "").strip())
    except (TypeError, ValueError, AttributeError):
        return None
    return max(0, min(value, RETRY_WAIT_CAP))


def _snippet(error):
    try:
        return error.read(500).decode("utf-8", "replace")
    except (http.client.HTTPException, OSError):
        return "(no readable body)"


def _content(data):
    """The answer's text. An answer the provider cut short (`finish_reason`
    length or content_filter) is an error, not a short review."""
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("response has no choices[0].message.content") from None
    if choice.get("finish_reason") in ("length", "content_filter"):
        raise ValueError(f"answer cut short (finish_reason {choice['finish_reason']})")
    if isinstance(content, list):  # content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not isinstance(content, str):
        raise ValueError("response content is not text")
    return content


def _meta(data, headers=None):
    """(usage total tokens or None, the model the server says it served or
    None, request id or None): what the log line and the pass record keep."""
    usage = data.get("usage") if isinstance(data, dict) else None
    total = usage.get("total_tokens") if isinstance(usage, dict) else None
    served = data.get("model") if isinstance(data, dict) else None
    request_id = (headers or {}).get("x-request-id") if headers is not None else None
    return (
        total if isinstance(total, int) else None,
        served if isinstance(served, str) and served else None,
        request_id or None,
    )


# --- the diff -----------------------------------------------------------------


class GitError(subprocess.CalledProcessError):
    """A failed git call that prints git's own reason, not the command dump
    `CalledProcessError` renders ("returned non-zero exit status 128")."""

    def __str__(self):
        reason = (self.stderr or "").strip().split("\n")[-1]
        return f"git {' '.join(self.cmd[3:])}: {reason or f'exit {self.returncode}'}"


def _git(wt, *args):
    # errors="replace": one latin-1 file in the diff must not end the review.
    # stderr is kept: "no merge base" in a shallow clone is the message the
    # user needs, and it is the only one git gives.
    cmd = ["git", "-C", wt, *args]
    proc = subprocess.run(
        cmd, capture_output=True, encoding="utf-8", errors="replace", check=False
    )
    if proc.returncode:
        raise GitError(proc.returncode, cmd, proc.stdout, proc.stderr)
    return proc.stdout


def resolve_diff(wt, require_clean=True):
    """(base, head sha, diff, files, where the base came from). Same base as
    the command. The tree must be clean, and the diff is taken at fixed shas:
    the receipt must name the commit that was reviewed (`write_receipt`
    refuses it if HEAD moved meanwhile), and a `git fetch` moving the base
    ref mid-run must not make the file list disagree with the diff. The diff
    is capped like the command's `head -c`; a cut is marked in the prompt
    and said on stderr, since the model can't open the rest."""
    if require_clean:
        receipt.require_clean(wt)
    head = _git(wt, "rev-parse", "HEAD").strip()
    base, why = diff_base.resolve(wt)
    if not base:
        base, why = "HEAD~1", "no branch base resolved: the last commit only"
        _log("no branch base resolved: reviewing the last commit only (HEAD~1)")
    base_sha = _git(wt, "rev-parse", "--verify", f"{base}^{{commit}}").strip()
    diff = _git(wt, "diff", f"{base_sha}...{head}")
    if len(diff) > DIFF_CAP:
        _log(
            f"diff is {len(diff)} characters; sending the first {DIFF_CAP} "
            "(the rest is not reviewed)"
        )
        diff = (
            diff[:DIFF_CAP]
            + f"\n[grumpy oneshot: diff truncated at {DIFF_CAP} characters; "
            "the rest of the diff was not sent]\n"
        )
    names = [
        n
        for n in _git(wt, "diff", "-z", "--name-only", f"{base_sha}...{head}").split(
            "\0"
        )
        if n
    ]
    return base, head, diff, names, why


def changed_files_text(wt, names, head="HEAD"):
    out, total, cut, omitted = [], 0, [], []
    for name in names:
        try:
            text = _git(wt, "show", f"{head}:{name}")
        except subprocess.CalledProcessError:
            continue  # deleted
        if "\0" in text:
            continue  # binary
        if len(text) > FILE_CAP:
            cut.append(name)
            text = (
                text[:FILE_CAP]
                + f"\n[grumpy oneshot: file truncated at {FILE_CAP} characters]\n"
            )
        if total + len(text) > FILES_CAP:
            omitted.append(name)
            out.append(f"--- {name} (omitted: context cap reached)")
            continue
        total += len(text)
        out.append(f"--- {name}\n{text}")
    if cut:
        _log(f"file text truncated at {FILE_CAP} characters: {', '.join(cut)}")
    if omitted:
        _log(
            f"file text omitted (context cap {FILES_CAP} reached): {', '.join(omitted)}"
        )
    return "\n".join(out)


def settled_entries(wt, branch):
    """The `## Settled` entries of fix.md, as Step 4 reads them: the new
    folder's, or the legacy `.claude/grumpy/` one only when the new file is
    missing (a migrated fix.md carries its settled entries along)."""
    root = _git(wt, "rev-parse", "--show-toplevel").strip()
    entries = []
    for folder in (receipt.GRUMPY_ROOT, receipt.LEGACY_GRUMPY_ROOT):
        path = os.path.join(root, *folder, branch, "fix.md")
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        for block in re.split(r"^## Settled\s*$", text, flags=re.M)[1:]:
            for line in block.split("\n"):
                if line.startswith("## ") or line.startswith("# "):
                    break
                if line.startswith("- "):
                    entries.append(line)
        break  # the new file wins; the legacy one is only a fallback
    return entries


# --- prompts and parsing ----------------------------------------------------


def pass_messages(parts, template, base, diff, files_text):
    """One pass's messages: `template` is a unit's prompt from the command
    (review's aspect already filled in), with the base and diff put in."""
    prompt = template.replace("[DIFF_BASE]", base).replace("[DIFF_CONTENT]", diff)
    if files_text:
        prompt += (
            "\n\nFull text of the changed files at HEAD, for context only "
            "(report findings against the diff):\n<<<FILES_START>>>\n"
            f"{files_text}\n<<<FILES_END>>>"
        )
    return [
        {"role": "system", "content": f"{PREAMBLE}\n\n{parts['persona']}"},
        {"role": "user", "content": prompt},
    ]


def is_finding(line):
    return line.split("|", 1)[0] in SEVERITY_SECTIONS


def parse_pass_lines(text, context_labels=()):
    """(lines, errored, dropped). A finding line is read outside-in, as the
    command's audit discipline says: SEVERITY and file:line from the left,
    FACT and ASPECT (or DOMAIN) from the right, the rest is the text.
    Imagine's passes also return CONTEXT lines (first field one of
    `context_labels`, or `transport:<Name>`) and `HANDLED|area|why` lines;
    those are kept as they are, for the aggregator's narrative sections and
    Strengths. A line with pipes that still doesn't parse (four fields, a
    lowercase severity, a `file` with no `:N`) is dropped and counted, so the
    aggregator and stderr can say the aspect may be incomplete. A response
    with no usable line is errored, never salvaged, unless it is a short
    "nothing found": an empty answer is not that, it is a provider that
    answered nothing (a full queue, say) and must not pass for a clean
    review."""
    found, dropped = [], 0
    for line in text.strip().strip("`").split("\n"):
        fields = [f.strip() for f in line.strip().split("|")]
        head = fields[0]
        if len(fields) >= 3 and (
            head == "HANDLED"
            or head in context_labels
            or (":" in head and head.split(":", 1)[0] in context_labels)
        ):
            found.append("|".join(fields))
            continue
        if len(fields) < 5:
            dropped += len(fields) > 1  # pipes, but not a finding line
            continue
        sev, loc, fact, aspect = fields[0], fields[1], fields[-2], fields[-1]
        if sev not in SEVERITY_SECTIONS or fact not in FACTS:
            dropped += 1
            continue
        if not re.search(r":\d", loc):
            dropped += 1
            continue
        middle = "|".join(fields[2:-2])
        found.append("|".join([sev, loc, middle, fact, aspect]))
    body = text.strip()
    errored = not found and (not body or len(body) > 120)
    return found, errored, dropped


def aggregate_messages(parts, results, settled, model_line):
    blocks = []
    titles = {n: t for n, t, _ in parts["units"]}
    for aspect, r in results.items():
        # Imagine's pass names are this script's own; the agent title is the
        # name the aggregation step knows the pass by.
        label = f"{aspect}: {titles[aspect]}" if parts["context_labels"] else aspect
        if r.failure:
            blocks.append(
                f"[{label}] errored: the call failed ({r.failure}). Say this aspect needs manual review."
            )
        elif r.errored:
            blocks.append(
                f"[{label}] errored: the response was not pipe lines. Say this aspect needs manual review."
            )
        else:
            block = f"[{label}]\n" + ("\n".join(r.lines) or "(nothing found)")
            if r.dropped:
                block += (
                    f"\n(the runner dropped {r.dropped} malformed line(s) from this "
                    "pass; say that this aspect may be incomplete)"
                )
            blocks.append(block)
    prompt = "\n\n".join(
        [
            "You are the aggregator. The runner already ran one finding pass per "
            "aspect and gives you their pipe lines below. Follow grumpy's "
            "aggregation rules and render the report.",
            *(f"{heading}\n\n{text}" for heading, text in parts["aggregate"]),
            "Settled entries from fix.md (the runner read them for you):\n"
            + ("\n".join(settled) or "(none)"),
            "Pass outputs:\n\n" + "\n\n".join(blocks),
            f"Reply with only the report, from `{parts['title']}:` through "
            f"`## Verdict`. Its model line is exactly: {model_line}",
        ]
    )
    return [
        {"role": "system", "content": f"{PREAMBLE}\n\n{parts['persona']}"},
        {"role": "user", "content": prompt},
    ]


def finish_report(text, model_line, *stamps, title="# Code Review"):
    """The model's report with the `_Model:_` line (and any other `_Name:_`
    stamp lines this script writes itself: `_Base:_` or `_Analyzed:_`,
    `_Incomplete:_`) set here, or ValueError when it isn't a report (then
    nothing is saved). `title` is the report's heading (`# Code Review`,
    `# Production Imagination`). A preamble before it, or a note after a
    closing fence, is dropped rather than failing the run at its last
    step."""
    text = text.strip()
    fence = re.match(r"^```[a-z]*\n(.*)\n```$", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start = re.search(rf"^{re.escape(title)}", text, re.M)
    if not start or "## Verdict" not in text[start.start() :]:
        raise ValueError(
            f"the aggregation reply is not a report ({title} ... ## Verdict)"
        )
    lines = text[start.start() :].strip().split("\n")
    # After the Verdict heading, a closing fence that no fence opened is the
    # one the preamble opened: the report ends there, the note after it goes.
    verdict = next(
        (i for i, ln in enumerate(lines) if ln.startswith("## Verdict")), len(lines)
    )
    fenced = False
    for i in range(verdict + 1, len(lines)):
        if lines[i].startswith("```"):
            if not fenced and lines[i].strip() == "```":
                lines = lines[:i]
                break
            fenced = not fenced
    ours = tuple(s.split(":", 1)[0] + ":" for s in (model_line, *stamps))
    lines = [ln for ln in lines if not ln.startswith(ours)]
    return "\n".join([lines[0], "", model_line, *stamps, *lines[1:]]).rstrip() + "\n"


# --- running ------------------------------------------------------------------


def _log(msg):
    sys.stderr.write(f"[grumpy oneshot] {msg}\n")


def _parse_args(argv):
    args = {
        "pass": None,
        "provider": None,
        "model": None,
        "worktree": ".",
        "aspects": None,
        "jobs": None,  # DEFAULT_JOBS[provider] when absent
        "timeout": 300,
        "dry_run": False,
    }
    it = iter(argv)
    for a in it:
        if a == "--dry-run":
            args["dry_run"] = True
        elif a in (
            "--provider",
            "--model",
            "--worktree",
            "--aspects",
            "--jobs",
            "--timeout",
        ):
            value = next(it, None)
            if value is None:
                raise ValueError(f"{a} needs a value")
            args[a[2:]] = value
        elif a.startswith("-"):
            raise ValueError(f"unknown flag {a}")
        elif args["pass"] is None:
            args["pass"] = a
        else:
            raise ValueError(f"unexpected argument {a!r}")
    if args["pass"] not in PASSES:
        raise ValueError(f"pass must be one of: {', '.join(PASSES)}")
    if args["jobs"] is not None:
        args["jobs"] = int(args["jobs"])
    args["timeout"] = int(args["timeout"])
    return args


class Stop(Exception):
    """End the run with `code` (1: earned nothing, 2: setup error) and a message."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def configure(args, env):
    """(provider, prompt parts, model, model note), or Stop(2)."""
    provider = args["provider"] or env.get("SHARPEN_LLM_PROVIDER") or "openai"
    if provider not in PROVIDERS:
        raise Stop(2, f"unknown provider {provider!r} (one of: {', '.join(PROVIDERS)})")
    try:
        parts = load_command(args["pass"])
    except (OSError, ExtractionError) as e:
        raise Stop(
            2, f"can't build prompts from commands/{args['pass']}.md: {e}"
        ) from None
    model, note = args["model"] or env.get("SHARPEN_LLM_MODEL"), ""
    if not model and PROVIDERS[provider]["default_from_map"]:
        model, note = model_from_map(args["worktree"])
    if not model:
        raise Stop(
            2,
            f"no model: pass --model or set SHARPEN_LLM_MODEL ({provider} has no default)",
        )
    return provider, parts, model, note


def select_aspects(parts, wanted):
    """The units (name, description, prompt) `--aspects` picked, in the
    command's order; all of them without the flag."""
    known = [n for n, _, _ in parts["units"]]
    names = [n.strip() for n in wanted.split(",") if n.strip()] if wanted else known
    unknown = [n for n in names if n not in known]
    if unknown or not names:
        raise Stop(
            2, f"unknown aspect(s) {unknown or wanted!r} (known: {', '.join(known)})"
        )
    return [u for u in parts["units"] if u[0] in names]


def _timed_chat(client, model, messages, timeout, label):
    started = time.monotonic()
    reply = client.chat(model, messages, timeout, label=label)
    return reply, time.monotonic() - started


def run_passes(client, model, passes, jobs, timeout, labels=()):
    """{aspect: PassResult}, in parallel; `labels` are the CONTEXT line labels
    the parser keeps (imagine's). Stop(2) when the provider rejected
    the request as setup (key, model, URL); Stop(1) when every call failed.
    Each pass logs one line when it completes (duration, findings, tokens,
    request id), so a hung server and a slow model look different. On a setup
    status, or Ctrl-C, the passes still queued are cancelled rather than sent
    to a provider that already said no; a call already in flight runs to its
    end (urllib has no cancel), which is the most the floor offers."""
    results = {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs))
    try:
        futures = {
            pool.submit(_timed_chat, client, model, m, timeout, f"pass {n}"): n
            for n, m in passes.items()
        }
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                reply, seconds = fut.result()
            except ProviderError as e:
                if e.status in SETUP_STATUSES:
                    raise Stop(2, f"the provider rejected the request: {e}") from None
                results[name] = PassResult([], False, str(e), 0)
                _log(f"pass {name} failed: {e}")
                continue
            lines, errored, dropped = parse_pass_lines(reply.text, labels)
            results[name] = PassResult(lines, errored, None, dropped)
            findings = sum(1 for ln in lines if is_finding(ln))
            detail = [f"{findings} finding line(s)"]
            if len(lines) > findings:
                detail.append(f"{len(lines) - findings} context line(s)")
            if errored:
                detail.append(
                    "no finding lines: errored"
                    if reply.text.strip()
                    else "empty answer: errored"
                )
            if dropped:
                detail.append(f"{dropped} malformed line(s) dropped")
            if reply.usage is not None:
                detail.append(f"{reply.usage} tokens")
            if reply.model and reply.model != model:
                detail.append(f"served by {reply.model}")
            if reply.request_id:
                detail.append(f"request {reply.request_id}")
            _log(f"pass {name}: done in {seconds:.0f}s, {', '.join(detail)}")
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    if all(r.failure for r in results.values()):
        raise Stop(1, "every finding pass failed: no report, no receipt")
    return {n: results[n] for n in passes}


def artifacts_dir(wt, branch):
    """`.sharpen/grumpy/<branch>/` under the worktree's root, created and
    self-ignored (the same folder `receipt.write_receipt` uses)."""
    root = _git(wt, "rev-parse", "--show-toplevel").strip()
    path = os.path.join(root, *receipt.GRUMPY_ROOT, branch)
    os.makedirs(path, exist_ok=True)
    receipt._ensure_self_ignored(os.path.join(root, *receipt.GRUMPY_ROOT))
    return path


def _write_text(path, text):
    """Write whole or not at all: a `--verify` sweep hashing the report
    mid-write, or a process killed there, must not see a truncated file."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_passes(artifacts, pass_name, head, model, results):
    """The finding passes' outcome, one JSON line per aspect, next to the
    report and before the aggregation call: what every pass returned stays
    on disk when the aggregation fails, and stays comparable with review.md
    when it succeeds."""
    path = os.path.join(artifacts, PASSES_FILE.format(pass_name))
    lines = [
        json.dumps(
            {
                "aspect": aspect,
                "head": head,
                "model": model,
                "lines": r.lines,
                "errored": r.errored,
                "failure": r.failure,
                "dropped": r.dropped,
            },
            sort_keys=True,
        )
        for aspect, r in results.items()
    ]
    _write_text(path, "\n".join(lines) + "\n")
    return path


def save_report(artifacts, pass_name, report):
    path = os.path.join(artifacts, receipt.REPORT_FOR[pass_name])
    _write_text(path, report)
    return path


def run(args, env):
    wt, pass_name = args["worktree"], args["pass"]
    provider, parts, model, note = configure(args, env)
    aspects = select_aspects(parts, args["aspects"])
    jobs = args["jobs"] if args["jobs"] is not None else DEFAULT_JOBS[provider]
    try:
        # Before any paid call: the receipt would refuse these anyway.
        branch = receipt.named_branch(wt)
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        raise Stop(2, f"no {pass_name}: {e}") from None
    try:
        base, head, diff, names, why = resolve_diff(
            wt, require_clean=not args["dry_run"]
        )
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        raise Stop(1, f"no {pass_name}: {e}") from None
    if not diff.strip():
        raise Stop(1, f"nothing to {pass_name} against {base}")
    files_text = changed_files_text(wt, names, head)
    aspect_names = ", ".join(n for n, _, _ in aspects)
    model_line = (
        f"_Model: {model} via {provider} (one-shot, no agent; aspects: {aspect_names})"
        f"{'; ' + note if note else ''}_"
    )
    if pass_name == "imagine":
        base_line = f"_Analyzed: {base}, from {why} — HEAD at {head[:7]}_"
    else:
        base_line = f"_Base: {base}, from {why}_"
    passes = {n: pass_messages(parts, t, base, diff, files_text) for n, _, t in aspects}
    if args["dry_run"]:
        for name, messages in passes.items():
            sys.stdout.write(f"===== pass: {name}\n{messages[1]['content']}\n")
        sys.stdout.write(
            f"===== {len(passes)} pass call(s) + 1 aggregation call to {provider} {model}\n"
        )
        return
    try:
        client = ChatClient(provider, env)
    except ValueError as e:
        raise Stop(2, str(e)) from None
    _log(
        f"{len(passes)} {pass_name} pass(es) on {provider} {model}, {jobs} at a time, "
        f"base {base} ({why}); the diff and changed files are sent to the provider"
    )
    results = run_passes(
        client, model, passes, jobs, args["timeout"], parts["context_labels"]
    )
    try:
        artifacts = artifacts_dir(wt, branch)
        passes_path = save_passes(artifacts, pass_name, head, model, results)
        settled = settled_entries(wt, branch)
    except (OSError, subprocess.CalledProcessError) as e:
        raise Stop(1, f"pass results not saved: {e}. No report, no receipt") from None
    incomplete = sorted(n for n, r in results.items() if r.errored or r.failure)
    stamps = [base_line]
    if incomplete:
        stamps.append(
            f"_Incomplete: {', '.join(incomplete)} (failed or no finding lines); "
            "this report earned no receipt_"
        )
    messages = aggregate_messages(parts, results, settled, model_line)
    try:
        reply, _ = _timed_chat(client, model, messages, args["timeout"], "aggregation")
        report = finish_report(reply.text, model_line, *stamps, title=parts["title"])
    except (ProviderError, ValueError) as e:
        raise Stop(
            1,
            f"aggregation failed: {e}. No report, no receipt; the finding passes' "
            f"lines are saved at {passes_path}",
        ) from None
    try:
        _log(f"report saved: {save_report(artifacts, pass_name, report)}")
    except OSError as e:
        raise Stop(1, f"report not saved: {e}") from None
    if incomplete:
        # A pass with an aspect missing isn't the pass gate 3 or 5 asks for.
        raise Stop(
            1,
            f"aspect(s) {', '.join(incomplete)} failed or answered without finding "
            "lines: the report is saved for reading, but no receipt. Re-run to earn "
            "the gate",
        )
    executor = {
        "host": EXECUTOR_HOST,
        "provider": provider,
        "model": model,
        "aspects": ",".join(n for n, _, _ in aspects),
    }
    try:
        # `head=` makes the receipt name the commit that was diffed, or refuse.
        rpath, _ = receipt.write_receipt(pass_name, wt, executor=executor, head=head)
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        raise Stop(1, f"receipt not written: {e}") from None
    _log(
        f"receipt written: {rpath} (record it with: record-gate.py --verify {pass_name})"
    )


def main(argv, env=None):
    env = os.environ if env is None else env
    try:
        args = _parse_args(argv)
    except ValueError as e:
        _log(f"{e}\n" + __doc__.split("\n\n")[1])
        return 2
    try:
        run(args, env)
    except Stop as e:
        _log(str(e))
        return e.code
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
