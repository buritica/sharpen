#!/usr/bin/env python3
"""Which host is running sharpen, and what each host does for it.

Two jobs, both deliberately small:

1. `detect_host()` names the host from the environment. An adapter that knows
   its host says so explicitly with `SHARPEN_HOST` (the pi extension exports
   `SHARPEN_HOST=pi`). Claude Code is recognized by the `CLAUDECODE=1` it
   exports to every process it spawns. Anything else is `unknown`: a guessed
   name is worse than an honest blank, because the capability manifest and the
   auto-init message are built from it. Codex reads the same `hooks/hooks.json`
   as Claude Code but has no confirmed environment signal yet, so it reports
   `unknown` until one is verified live (see docs/hosts.md).

2. `HOSTS` is the support matrix: one row per host, the single source that
   `docs/hosts.md`'s table is rendered from. `check-marketplace.py` fails when
   the committed table differs from `render_matrix()`, so the doc can't drift
   from what the adapters declare. Regenerate it with:

       python3 plugins/sdlc/scripts/hosts.py --write docs/hosts.md

Host-neutral and stdlib only. This module reads the environment; the portable
`capabilities.py` deliberately does not.
"""

import os
import re
import sys

HOST_ENV = "SHARPEN_HOST"
UNKNOWN = "unknown"

# How skill gates (2-6) get recorded once a pass has left its receipt.
# "automatic": a host event (Claude Code's Stop hook, pi's agent_end) sweeps
# receipts at the end of every turn. "manual": the agent runs
# `record-gate.py --verify <pass>` itself after each pass. Every host that can
# run a shell can record; this only says whether it happens on its own.
RECORDING_AUTOMATIC = "automatic"
RECORDING_MANUAL = "manual"

_HOST_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")

# Column order of the rendered matrix, with header labels.
MATRIX_COLUMNS = (
    ("install", "Install"),
    ("pr_block", "Blocks ungated `gh pr create`"),
    ("auto_arm", "Arms a cycle on first commit"),
    ("recording", "Records gates 2-6"),
    ("plugin_root", "Plugin root in skills"),
)

UNVERIFIED = "unverified"

# One row per host. `recording` is what `recording_mode()` reports for hosts
# that have an adapter; instructions-only hosts have no adapter at all.
# Keep the cell text short: it is rendered into a Markdown table.
HOSTS = {
    "claude-code": {
        "label": "Claude Code",
        "tier": "native",
        "recording": RECORDING_AUTOMATIC,
        "install": "Plugin marketplace",
        "pr_block": "PreToolUse hook",
        "auto_arm": "PostToolUse hook",
        "recording_cell": "Automatic: `Stop` hook sweeps receipts",
        "plugin_root": "`${CLAUDE_PLUGIN_ROOT}` filled in at load (braced only)",
    },
    "codex": {
        "label": "Codex CLI",
        "tier": "native",
        "recording": RECORDING_MANUAL,
        "install": "Reads the same `hooks/hooks.json`",
        "pr_block": "PreToolUse hook, verified live",
        "auto_arm": "PostToolUse hook, verified live",
        "recording_cell": "`--verify` after each pass; `Stop` hook " + UNVERIFIED,
        "plugin_root": "Substitution in skill text " + UNVERIFIED,
    },
    "pi": {
        "label": "pi",
        "tier": "native",
        "recording": RECORDING_AUTOMATIC,
        "install": "`pi install` (TS extensions)",
        "pr_block": "`tool_call`; a TUI human can override, headless fails closed",
        "auto_arm": "`tool_result`",
        "recording_cell": "Automatic: `agent_end` sweeps receipts",
        "plugin_root": "Each extension exports `<PLUGIN>_PLUGIN_ROOT`",
    },
    "instructions-only": {
        "label": "Gemini CLI, Cursor, Copilot, `npx skills add`",
        "tier": "instructions-only",
        "recording": None,
        "install": "Generated SKILL.md only",
        "pr_block": "None",
        "auto_arm": "None",
        "recording_cell": "None (no scripts ship)",
        "plugin_root": "Unset; skills that run scripts can't",
    },
}


def detect_host(env=None):
    """Name the running host: explicit `SHARPEN_HOST`, else Claude Code's
    `CLAUDECODE=1`, else `unknown`. A malformed explicit value is `unknown`
    rather than trusted, because it ends up in a filename."""
    env = os.environ if env is None else env
    explicit = (env.get(HOST_ENV) or "").strip().lower()
    if explicit:
        return explicit if _HOST_NAME.match(explicit) else UNKNOWN
    if env.get("CLAUDECODE") == "1":
        return "claude-code"
    return UNKNOWN


MANIFEST_ENV = "SDLC_CAPABILITIES_PATH"
# Written by every sdlc before 4.15, on every host, whatever the host was.
LEGACY_MANIFEST_NAME = "capabilities.claude.json"


def manifest_name(host):
    """One manifest per host, so a Claude Code session and a pi or Codex
    session in the same repo stop overwriting each other's declaration."""
    return f"capabilities.{host}.json"


def recording_mode(host):
    """`automatic` only for hosts whose adapter sweeps receipts on its own;
    everything else, `unknown` included, is told to run `--verify`."""
    row = HOSTS.get(host)
    if row and row.get("recording") == RECORDING_AUTOMATIC:
        return RECORDING_AUTOMATIC
    return RECORDING_MANUAL


def render_matrix():
    """The support matrix as a Markdown table, rows in `HOSTS` order."""
    header = ["Host"] + [label for _, label in MATRIX_COLUMNS]
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    for row in HOSTS.values():
        cells = [row["label"]]
        for key, _ in MATRIX_COLUMNS:
            cells.append(row["recording_cell"] if key == "recording" else row[key])
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


MATRIX_BEGIN = (
    "<!-- hosts:begin (generated by plugins/sdlc/scripts/hosts.py; do not edit) -->"
)
MATRIX_END = "<!-- hosts:end -->"


def splice_matrix(text):
    """`text` with the region between the matrix markers replaced by a fresh
    `render_matrix()`. Raises ValueError when the markers are missing."""
    start = text.find(MATRIX_BEGIN)
    end = text.find(MATRIX_END)
    if start < 0 or end < start:
        raise ValueError("support-matrix markers not found")
    head = text[: start + len(MATRIX_BEGIN)]
    return head + "\n" + render_matrix() + text[end:]


def main(argv):
    if len(argv) == 2 and argv[0] in ("--write", "--check"):
        path = argv[1]
        with open(path, encoding="utf-8") as f:
            text = f.read()
        fresh = splice_matrix(text)
        if argv[0] == "--check":
            if fresh != text:
                sys.stderr.write(
                    f"{path}: support matrix is stale; run hosts.py --write\n"
                )
                return 1
            return 0
        if fresh != text:
            with open(path, "w", encoding="utf-8") as f:
                f.write(fresh)
        return 0
    if not argv:
        print(detect_host())
        return 0
    sys.stderr.write("usage: hosts.py [--write|--check <docs/hosts.md>]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
