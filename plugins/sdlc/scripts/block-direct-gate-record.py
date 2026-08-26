#!/usr/bin/env python3
"""
PreToolUse hook: block direct `record-gate.py --record` for skill-gated gates.

Skill-gated gates (simplify, grumpy-*) can ONLY be recorded by the auto-record
hook after the actual skill runs, or via `record-gate.py --attest <gate>
--reason <text>` (gate_store.attest_gate) — a deliberately separate, loud,
reason-required path for the reported case (sharpen#11) where the skill
genuinely ran but a re-invoked Skill tool call returned a cached "already
loaded" response and PostToolUse failed to fire for it. `--attest` is not
blocked here: it is the sanctioned exception, not the bypass this hook exists
to catch, and it marks its own stamp as unverified in `--status`/`--oneline`
rather than looking like a hook-verified one.
Bash-verifiable gates (tests/lint/typecheck) stay manually recordable.

Pure stdlib. @fires-on Bash tool, @blocking
"""

import json
import os
import sys

import hook_out as ho  # tiny: json + sys, both already imported here
import shell_parse as sp

# gate_store is imported lazily inside check_direct_record. It drags in
# subprocess/tempfile/datetime/fcntl/glob (~15ms) to read one small map and
# build two refusal strings, and this hook runs on every Bash call — the
# pre-filter short-circuits ~97% of them before any of it is needed.

# The recorder is invoked under several names — bare, or with any extension
# (a future `.sh`/`.mjs` shim must not silently lose enforcement). shell_parse
# strips the interpreter prefix (`python3 …/record-gate.py`) but leaves argv[0]
# verbatim, so the path is stripped here.
_RECORDER_STEM = "record-gate"
# The auto-record hook is the one authorized recorder, and it is also just a
# script that reads a payload on stdin: hand-feeding it a forged one stamps a
# gate whose skill never ran, without ever typing `authorized=True`. Same
# intent as a manual --record, so it gets the same answer.
#
# This is a speed bump for the obvious spelling, not a barrier — `cp` it
# elsewhere and run that, and argv[0] no longer matches. It can't be more than
# that: anyone who can run arbitrary python can call record_gate(
# authorized=True) directly. The point is that every remaining route has to be
# deliberate and legible in the transcript, which is the whole threat model
# (see gate_store.record_gate).
_AUTO_RECORD_STEM = "auto-record-skill-gate"


def _stem_matches(argv, stem):
    base = os.path.basename(argv[0])
    return base == stem or base.startswith(stem + ".")


def recorded_gates(command):
    """What this command would record: (gates, drives_auto_record).

    `gates` is every gate a real `record-gate --record <gate>` invocation would
    stamp; `drives_auto_record` is True if the command runs the auto-record
    hook itself.

    argv-based (see shell_parse), so wrappers like `bash -c "record-gate.py
    --record simplify"` are seen while a string literal — `echo "record-gate.py
    --record simplify"` — is not, matching enforce's discipline.

    Every invocation is collected, not just the first: `record-gate.py --record
    lint && record-gate.py --record simplify` would otherwise let a skill-gated
    stamp ride along behind an allowed one."""
    if not isinstance(command, str) or not any(
        stem in command for stem in (_RECORDER_STEM, _AUTO_RECORD_STEM)
    ):
        # Cheap pre-filter; this hook sees every Bash call. Both stems are
        # needed: `auto-record-skill-gate` does NOT contain `record-gate`.
        # The isinstance check is not paranoia: `in` raises TypeError on a
        # non-str, and this hook had no exception handler to catch it.
        return [], False
    gates, drives_auto_record = [], False
    for argv in sp.command_segments(command):
        if _stem_matches(argv, _AUTO_RECORD_STEM):
            drives_auto_record = True
        elif _stem_matches(argv, _RECORDER_STEM):
            gate = sp.flag_value(argv, "--record")
            if gate:
                gates.append(gate)
    return gates, drives_auto_record


def check_direct_record(tool, tool_input):
    if tool != "Bash":
        return True, None
    recorded, drives_auto_record = recorded_gates(tool_input.get("command", ""))
    if not recorded and not drives_auto_record:
        return True, None

    import gate_store as gs  # deferred: see the note at the imports

    if drives_auto_record:
        return False, (
            "BLOCKED: auto-record-skill-gate.py is the hook the harness runs "
            "after a skill completes — driving it by hand records a gate whose "
            "skill never ran.\n\n"
            "Run the skill itself; the gate is recorded when it finishes.\n\n"
            + gs.gate_lists_hint()
        )
    blocked = [g for g in recorded if g in gs.SKILL_FOR_GATE]
    if not blocked:
        return True, None
    message = gs.skill_gated_message(blocked[0])
    if len(blocked) > 1:
        # name them all, or the agent fixes one and gets refused again
        message += f"\n\nThis command also records: {', '.join(blocked[1:])}"
    return False, message


def main():
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        # A hook that silently no-ops on every call because the payload shape
        # changed is indistinguishable from one that's working. Exit-0 stderr
        # reaches nobody (see hook_out), so this goes to the user instead.
        ho.emit(ho.warn("block-direct", f"could not parse hook stdin ({e})"))
        return 0
    try:
        allowed, error = check_direct_record(
            data.get("tool_name"), data.get("tool_input", {})
        )
    except Exception as e:
        # Matches enforce's posture: don't wedge every Bash call over a bug
        # here, but don't vanish either. Without this the hook exits 1 with a
        # raw traceback in the user's face.
        ho.emit(
            ho.warn("block-direct", f"unexpected error, not blocking this call: {e}")
        )
        return 0
    if not allowed:
        # Same shape as enforce: documented payload plus exit 2, which blocks on
        # its own if the payload is ever rejected. The old form put a raw JSON
        # blob on stderr, which is what the agent then read as the reason.
        ho.emit(ho.deny(error))
        sys.stderr.write(error + "\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
