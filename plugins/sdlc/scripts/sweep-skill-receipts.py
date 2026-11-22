#!/usr/bin/env python3
"""End-of-turn adapter: record skill gates whose receipts are on disk.

Fires on Claude Code's `Stop` hook and on pi's `agent_end` (via the pi
extension). A grumpy pass ends by writing a receipt; the gate chain then runs
`record-gate.py --verify <skill>`. This sweep is the native safety net for a
turn where that last step was skipped: it records every receipt-backed gate
whose receipt names the branch's HEAD or an ancestor of it (fix commits before
the turn ends), in chain order, and nothing else (legacy cycles are never
touched). Refused receipts stay quiet here; `--verify all` prints why.

Never blocks: a Stop hook that exits 2 would keep the agent running, so every
outcome exits 0, with a one-line notice when something recorded or the store
could not be read.

Pure stdlib. @fires-on Stop (Claude), agent_end (pi)
"""

import json
import os
import sys

import hook_out as ho
import skill_gate_completion as completion


def main():
    try:
        data_in = json.load(sys.stdin)
    except Exception:
        data_in = {}
    cwd = data_in.get("cwd") or os.getcwd()
    try:
        swept = completion.sweep_receipts(
            cwd=os.path.realpath(cwd),
            host=data_in.get("host") or "claude-code",
            host_version=data_in.get("claude_version"),
        )
    except Exception as e:  # never break the end of a turn
        ho.emit(ho.warn("receipts", f"sweep skipped ({e})"))
        return 0
    notes = []
    if swept["recorded"]:
        notes.append(
            "recorded from receipts: "
            + ", ".join(f"{r['gate']} on {r['target']}" for r in swept["recorded"])
        )
    if swept["error"]:
        notes.append(f"sweep stopped: {swept['error']}")
    if notes:
        ho.emit(ho.warn("receipts", "; ".join(notes)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
