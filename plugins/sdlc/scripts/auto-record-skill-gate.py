#!/usr/bin/env python3
"""Claude Code PostToolUse adapter for automatic SDLC skill-gate recording.

This file parses only the Claude `Skill` hook payload. Host-neutral completion,
routing, ordering, invalidation, and store writes live in
`skill_gate_completion.py`; future host adapters must call that interface only
from a trusted post-skill lifecycle callback. Hosts without one retain the
visible, reason-required `record-gate.py --attest` fallback.
"""

import json
import os
import sys

import gate_store as gs
import hook_out as ho
import skill_gate_completion as completion

# Backward-compatible module exports for direct unit tests and downstream
# harnesses that imported the original Claude-only implementation.
active_worktree_branches = completion.active_worktree_branches
find_active_cycle = completion.find_active_cycle
handle_skill_completion = completion.handle_skill_completion


def main():
    try:
        data_in = json.load(sys.stdin)
    except Exception as e:
        ho.emit(ho.warn("auto-record", f"could not parse hook stdin ({e}), skipping"))
        return 0
    if data_in.get("tool_name") != "Skill":
        return 0
    skill = data_in.get("tool_input", {}).get("skill")
    if skill in gs.RENAMED_SKILLS:
        new_name = gs.RENAMED_SKILLS[skill]
        return ho.notify(
            "auto-record",
            f'"{skill}" was replaced by "{new_name}" — that gate did not '
            f"record. Run {new_name} instead.",
            surface=True,
        )
    if not skill or skill not in gs.SKILL_TO_GATE:
        return 0

    response = data_in.get("tool_response")
    succeeded = not (isinstance(response, dict) and response.get("is_error"))
    result = completion.record_skill_completion(
        skill=skill,
        cwd=data_in.get("cwd") or os.getcwd(),
        succeeded=succeeded,
        host="claude-code",
        host_version=data_in.get("claude_version"),
        invocation_id=data_in.get("tool_use_id"),
    )
    if result.get("error"):
        return ho.notify("auto-record", f"skipped {skill}: {result['error']}")
    if result.get("recorded"):
        # A stamp outside the invoking worktree, or a code-mutating gate that
        # cleared prior bash-gate evidence, needs a visible notification.
        branch = gs.detect_branch(data_in.get("cwd") or os.getcwd())
        cross_worktree = result.get("target") != branch
        where = f" on {result['target']}" if cross_worktree else ""
        invalidated = result.get("invalidated") or []
        invalidated_note = (
            f" — this gate can fix code inline, so {', '.join(invalidated)} "
            "no longer verify the current state and must run again"
            if invalidated
            else ""
        )
        return ho.notify(
            "auto-record",
            f'recorded "{result["gate"]}"{where} after {skill}{invalidated_note}',
            surface=cross_worktree or bool(invalidated),
        )
    if result.get("reason"):
        return ho.notify(
            "auto-record",
            f"skipped {skill}: {result['reason']}",
            surface=result.get("surprising", False),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
