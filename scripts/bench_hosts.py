#!/usr/bin/env python3
"""Host profiles for scripts/bench-grumpy.py: how each host runs a pass.

Claude Code, Codex and pi run grumpy's pass as an agent; `oneshot` runs
grumpy's own scripts/oneshot.py against a chat API with no agent. Building
the command line (including the system-prompt hint a pinned --mode adds),
hashing the prompt files, naming what ran, and reading what a run spent and
how its passes ran from the host's output live here; running and scoring
stay in bench-grumpy.py. Pure stdlib.
"""

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys

from frontmatter import split as split_frontmatter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRUMPY = os.path.join(ROOT, "plugins", "grumpy")

# ---------------------------------------------------------------------------
# Host profiles. `{model}` is dropped with its flag when no model is given.
# `{prompt}` is the argv slot for native invocation; inline invocation sends
# the prompt on stdin. The codex flags (exec, --model, --sandbox
# workspace-write, --skip-git-repo-check, `-` for stdin) match OpenAI's Codex
# CLI docs as of 2026-11-22; the pi flags match docs.pi.dev. Neither has had a
# live run yet, so their `verified` is False below and results record
# `flags_verified: false`; flip it here after a live run confirms them.
# --cmd overrides any of them without a code change, and `commands` prints
# exactly what would run. `oneshot` is no agent: it runs grumpy's own
# scripts/oneshot.py from this checkout against a chat API (`--provider`,
# OPENAI_API_KEY or SHARPEN_LLM_URL from the environment), so its command line
# is this repo's and is verified by its tests.
# ---------------------------------------------------------------------------
# What a review or imagine pass needs on Claude Code: git and python3 for the
# diff and the receipt, Write scoped to the report folder, read-only file
# tools, Agent for per-aspect passes. (File rules are spelled Edit(...); they
# cover Write too.) No unscoped Write or Edit: these passes
# report, they don't change code. (grumpy before 2.15 wrote under .claude/,
# which headless Claude Code refuses as a sensitive path; run_one then scored
# the printed report.)
CLAUDE_TOOLS = ",".join(
    [
        "Bash(git:*)",
        "Bash(python3:*)",
        "Bash(mkdir:*)",
        "Bash(ls:*)",
        "Bash(head:*)",
        "Read",
        "Edit(.sharpen/grumpy/**)",
        "Glob",
        "Grep",
        "Agent",
        "TaskCreate",
        "TaskUpdate",
    ]
)

HOSTS = {
    "claude": {
        "binary": "claude",
        "default_invoke": "native",
        "native": [
            "claude",
            "-p",
            "--plugin-dir",
            "{grumpy}",
            "--permission-mode",
            "acceptEdits",
            "--allowedTools={claude_tools}",
            "--output-format",
            "json",
            "--model",
            "{model}",
            "{prompt}",
        ],
        "inline": [
            "claude",
            "-p",
            "--permission-mode",
            "acceptEdits",
            "--allowedTools={claude_tools}",
            "--output-format",
            "json",
            "--model",
            "{model}",
        ],
        "verified": True,
    },
    "codex": {
        "binary": "codex",
        "default_invoke": "inline",
        "inline": [
            "codex",
            "exec",
            "--model",
            "{model}",
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "-",
        ],
        "verified": False,
    },
    "pi": {
        "binary": "pi",
        "default_invoke": "inline",
        "inline": ["pi", "--print", "--no-session", "--model", "{model}"],
        "verified": False,
    },
    "oneshot": {
        "binary": sys.executable,
        "default_invoke": "native",
        "native": [
            "{python}",
            "{grumpy}/scripts/oneshot.py",
            "{pass}",
            "--provider",
            "{provider}",
            "--model",
            "{model}",
        ],
        "verified": True,
    },
}
# How a pass runs its finding passes. `auto` leaves it to the host and the
# command (headless Claude Code has often run the passes itself; with
# subagents, review picks single or fanout by diff size), so an unpinned bench
# may not measure the path you care about. `inline`, `single` and `fanout`
# pin it with a line appended to Claude Code's system prompt. Codex, pi and
# oneshot have one way to run, so only `auto` applies there.
MODES = ("auto", "inline", "single", "fanout")
_HAS_AGENTS = "This harness supports subagents: the Agent tool is available. "
MODE_HINTS = {
    "inline": (
        "Run every review or simulation pass yourself, one after another, in "
        "this session. Do not use the Agent tool for them."
    ),
    "single": (
        _HAS_AGENTS + "Take the command's single path whatever the diff's "
        "size, as if `--single` were passed: one Agent call, on the model the "
        "command specifies, runs every pass in turn."
    ),
    "fanout": (
        _HAS_AGENTS + "Take the command's fanout subagent path whatever the "
        "diff's size, as if `--fanout` were passed: dispatch each pass as its "
        "own Agent call, in parallel, with the model the command specifies."
    ),
}
MODE_HOSTS = {"claude"}


# What each oneshot provider needs in the environment before a run can call it
# (oneshot.py's own PROVIDERS; checked before the first run, not per run).
ONESHOT_ENV = {"openai": "OPENAI_API_KEY", "compatible": "SHARPEN_LLM_URL"}


def skill_prompt(pass_name, grumpy=GRUMPY):
    """The pass's cross-host SKILL.md as a self-contained prompt."""
    with open(
        os.path.join(grumpy, "skills", pass_name, "SKILL.md"), encoding="utf-8"
    ) as f:
        _, body = split_frontmatter(f.read())
    body = body.replace("$ARGUMENTS", "")
    return (
        f"Run grumpy's {pass_name} pass on the current branch of this repository, non-interactively. "
        "Nobody is available to answer questions: make reasonable choices and finish. The arguments are empty. "
        f"Follow the instructions below exactly, including writing the report to .sharpen/grumpy/<branch>/{pass_name}.md.\n\n"
        + body.lstrip()
    )


def prompt_hash(pass_name, grumpy=GRUMPY, host=None):
    """Hash of the files the pass's prompt comes from. On `oneshot` that
    includes oneshot.py, which adds its own preamble and aggregation text,
    and oneshot_prompts.py, which cuts the per-pass prompts out of the
    command."""
    h = hashlib.sha256()
    rels = [
        os.path.join("commands", f"{pass_name}.md"),
        os.path.join("skills", pass_name, "SKILL.md"),
    ]
    if host == "oneshot":
        rels += [
            os.path.join("scripts", "oneshot.py"),
            os.path.join("scripts", "oneshot_prompts.py"),
        ]
    for rel in rels:
        path = os.path.join(grumpy, rel)
        if os.path.exists(path):
            with open(path, "rb") as f:
                h.update(f.read())
    return h.hexdigest()[:16]


def build_command(
    host,
    pass_name,
    model=None,
    invoke=None,
    template=None,
    grumpy=GRUMPY,
    provider=None,
    mode=None,
):
    """Return (argv, stdin_text) for one run. `{model}` and `{provider}`
    each drop with the flag before them when no value is given. A pinned
    `mode` (inline, single or fanout) appends its hint to Claude Code's system
    prompt and is refused on a host with one way to run, or with a `template`;
    `single` is refused for any pass but review, the only one with that path."""
    profile = HOSTS[host]
    invoke = invoke or profile["default_invoke"]
    if template:
        argv = shlex.split(template)
    else:
        if invoke not in profile:
            raise ValueError(f"host {host} has no {invoke} invocation")
        argv = list(profile[invoke])
    values = {"{model}": model, "{provider}": provider}
    out = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if tok in values and values[tok] is None:
            i += 1
            continue
        if tok.startswith("-") and nxt in values and values[nxt] is None:
            i += 2
            continue
        out.append(tok)
        i += 1
    native_prompt = f"/grumpy:{pass_name}"
    argv = [
        t.replace("{model}", model or "")
        .replace("{provider}", provider or "")
        .replace("{grumpy}", grumpy)
        .replace("{python}", sys.executable)
        .replace("{pass}", pass_name)
        .replace("{claude_tools}", CLAUDE_TOOLS)
        .replace("{prompt}", native_prompt)
        for t in out
    ]
    if mode and mode != "auto":
        if host not in MODE_HOSTS:
            raise ValueError(
                f"host {host} has one way to run a pass; --mode {mode} is for claude"
            )
        if template:
            # Splicing at argv[1] would land inside `env X=1 claude ...` or a
            # wrapper script's arguments; the template owns its own flags.
            raise ValueError(
                f"--mode {mode} can't be added to a --cmd template; put"
                " --append-system-prompt in the template instead"
            )
        if mode == "single" and pass_name != "review":
            raise ValueError(
                f"--mode single is for review only; {pass_name} has no single"
                " path, so use --pass review"
            )
        argv[1:1] = ["--append-system-prompt", MODE_HINTS[mode]]
    stdin_text = None if invoke == "native" else skill_prompt(pass_name, grumpy)
    return argv, stdin_text


def output_text(host, stdout):
    """What the host printed for a person to read. Claude Code runs with
    `--output-format json`, so its answer is the `result` field."""
    if host == "claude":
        data = _claude_json(stdout)
        if data is not None:
            return data.get("result") or ""
    return stdout


def _claude_json(stdout):
    """The result object `claude -p --output-format json` prints last, or
    None (a --cmd without the flag, a crash before it printed)."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            # Any of the keys a result object carries, so losing one of them
            # (say `usage`) doesn't also lose the report text.
            if isinstance(data, dict) and _RESULT_KEYS & set(data):
                return data
    return None


_RESULT_KEYS = {"result", "usage", "modelUsage", "total_cost_usd", "num_turns"}
# Only a pass's own completion line counts: Stop, `failed:` and retry lines can
# quote a provider's error body ("maximum context length is 128000 tokens").
# oneshot.py logs no usage for its aggregation call, so that call isn't counted.
_ONESHOT_TOKENS = re.compile(
    r"^\[grumpy oneshot\] pass \S+: done in [^\n]*?\b(\d+) tokens\b", re.M
)
_ONESHOT_DONE = re.compile(r"^\[grumpy oneshot\] pass \S+: done in ", re.M)
_ONESHOT_FAILED = re.compile(r"^\[grumpy oneshot\] pass \S+ failed:", re.M)
_CLAUDE_TOKEN_KEYS = (
    ("input_tokens", "inputTokens"),
    ("cache_read_tokens", "cacheReadInputTokens"),
    ("cache_write_tokens", "cacheCreationInputTokens"),
    ("output_tokens", "outputTokens"),
)
# Hosts whose runs are expected to record usage; a row from one of them with
# none is a broken measurement, not a host that can't say.
USAGE_HOSTS = {"claude", "oneshot"}
# Claude Code's per-subagent aliases: a model the analysis role can be pinned to.
CLAUDE_ALIASES = ("haiku", "sonnet", "opus")


def _count(v):
    """A token count, or None for a missing, null or non-numeric value."""
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def parse_usage(host, stdout, stderr=""):
    """What one run spent, as far as the host reports it, or None.

    {"cost_usd", "tokens", "input_tokens", "cache_read_tokens",
     "cache_write_tokens", "output_tokens", "turns", "subagents", "models"}
    with None for what a host doesn't say. Claude Code reports all of it in
    its JSON result (`models` maps each model to its tokens and cost, so the
    session and its subagents show apart). oneshot logs each call's total
    tokens on stderr and no cost. Codex and pi print no usage in the mode the
    bench runs them, so they record None.

    A count the host should have reported and didn't is None, never 0: a
    missing `modelUsage` or a renamed key must not read as a free run.
    `partial` is True when only some of the calls reported tokens."""
    if host == "claude":
        data = _claude_json(stdout)
        if data is None:
            return None
        models = {}
        totals = dict.fromkeys((k for k, _ in _CLAUDE_TOKEN_KEYS), 0)
        model_usage = data.get("modelUsage")
        complete = isinstance(model_usage, dict) and bool(model_usage)
        for name, u in (model_usage if complete else {}).items():
            u = u if isinstance(u, dict) else {}
            row = {k: _count(u.get(src)) for k, src in _CLAUDE_TOKEN_KEYS}
            if None in row.values():
                complete = False
                row["tokens"] = None
            else:
                for k, v in row.items():
                    totals[k] += v
                row["tokens"] = sum(row.values())
            row["cost_usd"] = u.get("costUSD")
            models[name] = row
        if not complete:
            totals = dict.fromkeys(totals)
        stats = data.get("subagent_stats")
        spawned = stats.get("spawned") if isinstance(stats, dict) else None
        return {
            "cost_usd": data.get("total_cost_usd"),
            "tokens": sum(totals.values()) if complete else None,
            **totals,
            "turns": data.get("num_turns"),
            "subagents": _count(spawned),
            "models": models,
            "partial": not complete,
        }
    if host == "oneshot":
        counts = [int(n) for n in _ONESHOT_TOKENS.findall(stderr)]
        if not counts:
            return None
        calls = len(_ONESHOT_DONE.findall(stderr)) + len(
            _ONESHOT_FAILED.findall(stderr)
        )
        return {
            "cost_usd": None,
            "tokens": sum(counts),
            "input_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "output_tokens": None,
            "turns": len(counts),
            "subagents": None,
            "models": {},
            # A failed pass, or a provider that omitted usage on some calls.
            "partial": calls > len(counts),
        }
    return None


def observed_mode(host, usage):
    """How the pass actually ran: `inline` when Claude Code spawned no
    subagent, `single` when it spawned one (review's single path), `fanout`
    when it spawned more, None where the host can't say.
    The count is ambiguous: a single-path review whose one agent failed or
    was rate-limited may be rerun once (review.md's Model paragraph), which
    spawns a second subagent and reads as `fanout` here.
    On Claude Code this reads `subagent_stats.spawned` from the JSON result
    and nothing else; when that key is absent the mode is None, and the
    summary says so under the table."""
    if host == "oneshot":
        return "oneshot"
    if not usage or usage.get("subagents") is None:
        return None
    # ponytail: one subagent reads as `single` for every pass; split by pass when an imagine row reports observed_mode single
    n = usage["subagents"]
    return "inline" if not n else "single" if n == 1 else "fanout"


def oneshot_version(grumpy=GRUMPY):
    """`grumpy <version> oneshot.py`: what ran on the oneshot host, where a
    `--version` of the Python interpreter would say nothing useful."""
    try:
        with open(
            os.path.join(grumpy, ".claude-plugin", "plugin.json"), encoding="utf-8"
        ) as f:
            return f"grumpy {json.load(f)['version']} oneshot.py"
    except (OSError, ValueError, KeyError):
        return None


def cli_version(binary):
    try:
        r = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=30
        )
        return (
            (r.stdout or r.stderr).strip().splitlines()[0]
            if (r.stdout or r.stderr).strip()
            else None
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def oneshot_setup(args, env=None):
    """(provider or None, why the run can't start or None), checked before
    the first run so a missing key fails once, not once per case. The
    provider is `--provider`, else SHARPEN_LLM_PROVIDER, else openai
    (oneshot.py's own order); it is None on every other host."""
    env = os.environ if env is None else env
    if args.host != "oneshot":
        return None, ("--provider is for --host oneshot" if args.provider else None)
    if getattr(args, "analysis", None):
        return None, "--analysis pins Claude Code's alias; on oneshot use --model"
    provider = args.provider or env.get("SHARPEN_LLM_PROVIDER") or "openai"
    if provider not in ONESHOT_ENV:
        return None, f"unknown provider {provider!r} (one of: {', '.join(ONESHOT_ENV)})"
    if not env.get(ONESHOT_ENV[provider]):
        return (
            None,
            f"{ONESHOT_ENV[provider]} is not set (needed by --provider {provider})",
        )
    if provider == "compatible" and not (args.model or env.get("SHARPEN_LLM_MODEL")):
        return None, "--provider compatible needs --model (or SHARPEN_LLM_MODEL)"
    return provider, None
