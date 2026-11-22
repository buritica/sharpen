#!/usr/bin/env python3
"""Run grumpy's review pass with no coding agent installed: one model, over HTTP.

Usage:
  oneshot.py review [--provider openai|compatible] [--model ID]
                    [--worktree PATH] [--aspects code,errors,...] [--jobs N]
                    [--timeout SECONDS] [--dry-run]

The no-agent path. A coding agent (Claude Code, Codex, pi) runs
`commands/review.md` itself: it spawns one finding pass per review aspect,
reads files as it goes, aggregates, saves the report and writes the receipt.
This script does the same steps for a model that has no tools, so "reviewed"
means one thing on every path:

  1. Resolve the diff the way the command does (`diff_base.py`, the branch's
     own layer on a stack), capped at 200000 characters like the command.
  2. Build every prompt from `commands/review.md` at run time (see
     `extract_review`), so this path changes when the command changes. A
     command edit that breaks the extraction fails loudly here and in
     `tests/test_oneshot.py`; it never falls back to a prompt of its own.
  3. Send one finding call per aspect (in parallel), each with the command's
     own pass template, then one aggregation call with the command's Audit
     discipline and Step 4 text. The model never runs git or writes a file.
  4. Save the report to `.sharpen/grumpy/<branch>/review.md` with a `_Model:_`
     line this script writes itself, then write the same receipt any host
     writes (`receipt.py`), plus an `executor` naming this path, the provider
     and the model. `record-gate.py --verify review` records gate 3 from it,
     and `--status` shows that the gate came from this path.

What it can't do that an agent host does: open files beyond the changed
files handed to it, follow call sites, append the review note to the sdlc
plan (Step 6), or run simplify and fix at all. The diff and the changed
files' text are sent to the provider: with `openai` the code leaves this
machine; `compatible` pointed at a local server (Ollama, llama.cpp, vLLM, LM
Studio) keeps it here.

Providers (both speak OpenAI's chat-completions format; `PROVIDERS` is where
another one goes):
  openai      https://api.openai.com/v1/chat/completions, key in OPENAI_API_KEY.
              Default model: the `analysis` role's `openai` column in
              models.yaml (UNVERIFIED until the benchmark has measured it).
  compatible  SHARPEN_LLM_URL (a base URL like http://localhost:11434/v1, or
              the full .../chat/completions URL), optional SHARPEN_LLM_API_KEY.
              No default model: pass --model or set SHARPEN_LLM_MODEL.

--provider defaults to SHARPEN_LLM_PROVIDER, else openai. --model beats
SHARPEN_LLM_MODEL beats the map. Keys are read from the environment only and
never written to the report, the receipt or any log line; provider error text
is redacted before it is printed.

Exit codes: 0 report saved and receipt written. 1 the pass earned nothing:
a dirty tree, an empty diff, a git failure, any finding pass that failed or
answered in prose (the report is saved so you can read it, but no receipt),
an unusable aggregation, HEAD moving during the run, or a refused receipt.
2 usage or configuration: unknown flag, provider or aspect, no key, no model,
a detached HEAD or the default branch, a provider that rejects the request
as unauthorized or unknown (400/401/403/404), or an extraction error.
Pure stdlib.
"""

import concurrent.futures
import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import diff_base  # noqa: E402
import receipt  # noqa: E402

PLUGIN_ROOT = os.path.dirname(HERE)
PASSES = ("review",)  # imagine follows in the next release
DIFF_CAP = 200000  # the command's own cap (`| head -c 200000`)
FILES_CAP = 200000  # total text of changed files sent as context
FILE_CAP = 40000  # per file
EXECUTOR_HOST = "oneshot"

# grumpy's pass severities and the report section each one lands in. The
# command defines them ("SEVERITY is one of CRIT ..., WARN ..., NOTE ...");
# `extract_review` reads them back and a test pins the two together.
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
    "format the instructions ask for, and nothing else."
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
    return {
        "persona": persona,
        "aspects": [(n, " ".join(d.split())) for n, d in aspects],
        "pass_template": template,
        "severities": severities,
        "audit": section(body, "Audit discipline"),
        "step4": step4,
        "personality": section(body, "Personality Guidelines"),
    }


def load_command(pass_name, plugin_root=PLUGIN_ROOT):
    path = os.path.join(plugin_root, "commands", f"{pass_name}.md")
    with open(path, encoding="utf-8") as f:
        parts = extract_review(f.read())
    if parts["severities"] != list(SEVERITY_SECTIONS):
        raise ExtractionError(
            f"review.md's severities are {parts['severities']}, this path knows "
            f"{list(SEVERITY_SECTIONS)}: update SEVERITY_SECTIONS"
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

    def chat(self, model, messages, timeout=300, attempts=2):
        body = json.dumps({"model": model, "messages": messages}).encode()
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        last, status = None, None
        for attempt in range(attempts):
            req = urllib.request.Request(self.url, data=body, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return _content(json.load(resp))
            except urllib.error.HTTPError as e:
                status = e.code
                last = f"HTTP {e.code}: {_snippet(e)}"
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
                status, last = None, f"{type(e).__name__}: {e}"
            if attempt + 1 < attempts:
                time.sleep(2 * (attempt + 1))
        raise ProviderError(redact(f"{self.provider}: {last}", [self._key]), status)


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


# --- the diff -----------------------------------------------------------------


def _git(wt, *args):
    # errors="replace": one latin-1 file in the diff must not end the review.
    return subprocess.check_output(
        ["git", "-C", wt, *args],
        stderr=subprocess.DEVNULL,
        encoding="utf-8",
        errors="replace",
    )


def resolve_diff(wt, require_clean=True):
    """(base, head sha, diff, files). Same base as the command. The tree must
    be clean, and the diff is taken at a fixed sha: the receipt must name the
    commit that was reviewed, so `run` refuses it if HEAD moved meanwhile."""
    if require_clean:
        receipt.require_clean(wt)
    head = _git(wt, "rev-parse", "HEAD").strip()
    base, _why = diff_base.resolve(wt)
    if not base:
        base = "HEAD~1"
        _log("no branch base resolved: reviewing the last commit only (HEAD~1)")
    diff = _git(wt, "diff", f"{base}...{head}")[:DIFF_CAP]
    names = [
        n
        for n in _git(wt, "diff", "-z", "--name-only", f"{base}...{head}").split("\0")
        if n
    ]
    return base, head, diff, names


def changed_files_text(wt, names, head="HEAD"):
    out, total = [], 0
    for name in names:
        try:
            text = _git(wt, "show", f"{head}:{name}")
        except subprocess.CalledProcessError:
            continue  # deleted
        if "\0" in text:
            continue  # binary
        text = text[:FILE_CAP]
        if total + len(text) > FILES_CAP:
            out.append(f"--- {name} (omitted: context cap reached)")
            continue
        total += len(text)
        out.append(f"--- {name}\n{text}")
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


def pass_messages(parts, aspect, description, base, diff, files_text):
    prompt = (
        parts["pass_template"]
        .replace("[ASPECT]", f"{aspect} ({description})")
        .replace("[DIFF_BASE]", base)
        .replace("[DIFF_CONTENT]", diff)
    )
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


def parse_pass_lines(text):
    """(findings, errored). Outside-in, as the command's Audit discipline says:
    SEVERITY and file:line from the left, FACT and ASPECT from the right, the
    rest is the text. A response with no usable line that is more than a
    short "nothing found" is errored, never salvaged."""
    found = []
    for line in text.strip().strip("`").split("\n"):
        fields = line.strip().split("|")
        if len(fields) < 5:
            continue
        sev, loc, fact, aspect = fields[0], fields[1], fields[-2], fields[-1]
        if sev.strip() not in SEVERITY_SECTIONS or fact.strip() not in FACTS:
            continue
        if not re.search(r":\d", loc):
            continue
        middle = "|".join(fields[2:-2]).strip()
        found.append(
            "|".join([sev.strip(), loc.strip(), middle, fact.strip(), aspect.strip()])
        )
    errored = not found and len(text.strip()) > 120
    return found, errored


def aggregate_messages(parts, results, settled, model_line):
    blocks = []
    for aspect, (lines, errored, failure) in results.items():
        if failure:
            blocks.append(
                f"[{aspect}] errored: the call failed ({failure}). Say this aspect needs manual review."
            )
        elif errored:
            blocks.append(
                f"[{aspect}] errored: the response was not pipe lines. Say this aspect needs manual review."
            )
        else:
            blocks.append(f"[{aspect}]\n" + ("\n".join(lines) or "(nothing found)"))
    prompt = "\n\n".join(
        [
            "You are the aggregator. The runner already ran one finding pass per "
            "aspect and gives you their pipe lines below. Follow grumpy's "
            "aggregation rules and render the report.",
            "## Audit discipline\n\n" + parts["audit"],
            "## Step 4: Aggregate and Deliver the Verdict\n\n" + parts["step4"],
            "## Personality Guidelines\n\n" + parts["personality"],
            "Settled entries from fix.md (the runner read them for you):\n"
            + ("\n".join(settled) or "(none)"),
            "Pass outputs:\n\n" + "\n\n".join(blocks),
            f"Reply with only the report, from `# Code Review:` through "
            f"`## Verdict`. Its model line is exactly: {model_line}",
        ]
    )
    return [
        {"role": "system", "content": f"{PREAMBLE}\n\n{parts['persona']}"},
        {"role": "user", "content": prompt},
    ]


def finish_report(text, model_line):
    """The model's report with the `_Model:_` line set by this script, or
    ValueError when it isn't a report (then nothing is saved)."""
    text = text.strip()
    fence = re.match(r"^```[a-z]*\n(.*)\n```$", text, re.S)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("# Code Review") or "## Verdict" not in text:
        raise ValueError("the aggregation reply is not a review report")
    lines = [ln for ln in text.split("\n") if not ln.startswith("_Model:")]
    return "\n".join([lines[0], "", model_line, *lines[1:]]).rstrip() + "\n"


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
        "jobs": 4,
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
    args["jobs"], args["timeout"] = int(args["jobs"]), int(args["timeout"])
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
    known = [n for n, _ in parts["aspects"]]
    names = [n.strip() for n in wanted.split(",") if n.strip()] if wanted else known
    unknown = [n for n in names if n not in known]
    if unknown or not names:
        raise Stop(
            2, f"unknown aspect(s) {unknown or wanted!r} (known: {', '.join(known)})"
        )
    return [(n, d) for n, d in parts["aspects"] if n in names]


def run_passes(client, model, passes, jobs, timeout):
    """{aspect: (lines, errored, failure)}, in parallel. Stop(2) when the
    provider rejected the request as setup (key, model, URL); Stop(1) when
    every call failed."""
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = {
            pool.submit(client.chat, model, m, timeout): n for n, m in passes.items()
        }
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                results[name] = (*parse_pass_lines(fut.result()), None)
            except ProviderError as e:
                if e.status in SETUP_STATUSES:
                    raise Stop(2, f"the provider rejected the request: {e}") from None
                results[name] = ([], False, str(e))
                _log(f"pass {name} failed: {e}")
    if all(r[2] for r in results.values()):
        raise Stop(1, "every finding pass failed: no report, no receipt")
    return {n: results[n] for n in passes}


def save_report(wt, branch, pass_name, report):
    root = _git(wt, "rev-parse", "--show-toplevel").strip()
    artifacts = os.path.join(root, *receipt.GRUMPY_ROOT, branch)
    os.makedirs(artifacts, exist_ok=True)
    receipt._ensure_self_ignored(os.path.join(root, *receipt.GRUMPY_ROOT))
    path = os.path.join(artifacts, receipt.REPORT_FOR[pass_name])
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    return path


def run(args, env):
    wt, pass_name = args["worktree"], args["pass"]
    provider, parts, model, note = configure(args, env)
    aspects = select_aspects(parts, args["aspects"])
    try:
        # Before any paid call: the receipt would refuse these anyway.
        branch = receipt.named_branch(wt)
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        raise Stop(2, f"no review: {e}") from None
    try:
        base, head, diff, names = resolve_diff(wt, require_clean=not args["dry_run"])
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        raise Stop(1, f"no review: {e}") from None
    if not diff.strip():
        raise Stop(1, f"nothing to review against {base}")
    files_text = changed_files_text(wt, names, head)
    model_line = f"_Model: {model} via {provider} (one-shot, no agent){'; ' + note if note else ''}_"
    passes = {n: pass_messages(parts, n, d, base, diff, files_text) for n, d in aspects}
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
        f"{len(passes)} finding pass(es) on {provider} {model}, base {base}; "
        "the diff and changed files are sent to the provider"
    )
    results = run_passes(client, model, passes, args["jobs"], args["timeout"])
    messages = aggregate_messages(
        parts, results, settled_entries(wt, branch), model_line
    )
    try:
        report = finish_report(
            client.chat(model, messages, args["timeout"]), model_line
        )
    except (ProviderError, ValueError) as e:
        raise Stop(1, f"aggregation failed: {e}. No report, no receipt") from None
    try:
        _log(f"report saved: {save_report(wt, branch, pass_name, report)}")
    except OSError as e:
        raise Stop(1, f"report not saved: {e}") from None
    incomplete = sorted(
        n for n, (_, errored, failure) in results.items() if errored or failure
    )
    if incomplete:
        # A review with an aspect missing isn't the review gate 3 asks for.
        raise Stop(
            1,
            f"aspect(s) {', '.join(incomplete)} failed or answered in prose: the "
            "report is saved for reading, but no receipt. Re-run to earn the gate",
        )
    try:
        moved = _git(wt, "rev-parse", "HEAD").strip() != head
    except (OSError, subprocess.CalledProcessError):
        moved = True
    if moved:
        raise Stop(1, f"HEAD moved during the run (reviewed {head[:12]}): no receipt")
    executor = {"host": EXECUTOR_HOST, "provider": provider, "model": model}
    try:
        rpath, _ = receipt.write_receipt(pass_name, wt, executor=executor)
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
