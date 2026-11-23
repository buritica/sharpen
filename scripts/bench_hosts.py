#!/usr/bin/env python3
"""Host profiles for scripts/bench-grumpy.py: how each host runs a pass.

Claude Code, Codex and pi run grumpy's pass as an agent; `oneshot` runs
grumpy's own scripts/oneshot.py against a chat API with no agent. Building
the command line, hashing the prompt files and naming what ran live here;
running and scoring stay in bench-grumpy.py. Pure stdlib.
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
# How a pass runs its finding passes. `auto` leaves it to the host: headless
# Claude Code (`claude -p`) usually runs every pass itself in one session,
# while an interactive session usually fans out to one subagent per pass, so
# a bench that never pins the mode measures the cheaper path only. `inline`
# and `fanout` pin it with a line appended to Claude Code's system prompt.
# Codex, pi and oneshot have one way to run, so only `auto` applies there.
MODES = ("auto", "inline", "fanout")
MODE_HINTS = {
    "inline": (
        "Run every review or simulation pass yourself, one after another, in "
        "this session. Do not use the Agent tool for them."
    ),
    "fanout": (
        "This harness supports subagents: the Agent tool is available. Take "
        "the command's subagent path: dispatch each pass as its own Agent "
        "call, in parallel, with the model the command specifies."
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
    `mode` (inline or fanout) appends its hint to Claude Code's system prompt
    and is refused on a host with one way to run."""
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
            if isinstance(data, dict) and "usage" in data:
                return data
    return None


_ONESHOT_TOKENS = re.compile(r"^\[grumpy oneshot\] .*?\b(\d+) tokens\b", re.M)


def parse_usage(host, stdout, stderr=""):
    """What one run spent, as far as the host reports it, or None.

    {"cost_usd", "tokens", "input_tokens", "cache_read_tokens",
     "cache_write_tokens", "output_tokens", "turns", "subagents", "models"}
    with None for what a host doesn't say. Claude Code reports all of it in
    its JSON result (`models` maps each model to its tokens and cost, so the
    session and its subagents show apart). oneshot logs each call's total
    tokens on stderr and no cost. Codex and pi print no usage in the mode the
    bench runs them, so they record None."""
    if host == "claude":
        data = _claude_json(stdout)
        if data is None:
            return None
        models = {}
        totals = dict.fromkeys(
            (
                "input_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "output_tokens",
            ),
            0,
        )
        for name, u in (data.get("modelUsage") or {}).items():
            row = {
                "input_tokens": u.get("inputTokens", 0),
                "cache_read_tokens": u.get("cacheReadInputTokens", 0),
                "cache_write_tokens": u.get("cacheCreationInputTokens", 0),
                "output_tokens": u.get("outputTokens", 0),
            }
            for k, v in row.items():
                totals[k] += v
            row["tokens"] = sum(row.values())
            row["cost_usd"] = u.get("costUSD")
            models[name] = row
        stats = data.get("subagent_stats") or {}
        return {
            "cost_usd": data.get("total_cost_usd"),
            "tokens": sum(totals.values()),
            **totals,
            "turns": data.get("num_turns"),
            "subagents": stats.get("spawned"),
            "models": models,
        }
    if host == "oneshot":
        counts = [int(n) for n in _ONESHOT_TOKENS.findall(stderr)]
        if not counts:
            return None
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
        }
    return None


def observed_mode(host, usage):
    """How the pass actually ran: `fanout` when Claude Code spawned
    subagents, `inline` when it spawned none, None where the host can't say."""
    if host == "oneshot":
        return "oneshot"
    if not usage or usage.get("subagents") is None:
        return None
    return "fanout" if usage["subagents"] else "inline"


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
