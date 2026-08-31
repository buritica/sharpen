#!/usr/bin/env python3
"""
How a hook says something and is actually heard.

The rule this module exists to encode, because getting it wrong is silent:

  - A hook's stderr reaches the MODEL only on exit 2. On exit 0 it goes to the
    debug log, which nobody reads. So an exit-0 `sys.stderr.write` is not a
    warning; it is a comment addressed to no one.
  - In PostToolUse, exit 2 does not block anything — the tool already ran — so
    it is simply the visible channel. `notify` uses it.
  - In PreToolUse, exit 2 DENIES. That makes it unusable for anything attached
    to an allow. `permissionDecisionReason` is model-visible but only travels
    attached to a decision, and deciding "allow" would auto-approve the command
    past the user's permission prompt — a worse trade than any breadcrumb is
    worth. So `warn` falls back to `systemMessage`, which reaches the USER and
    not the model: use it for things a human should notice, never for anything
    the agent is expected to act on. Anything the agent must act on has to ride
    a denial.

`additionalContext` was considered for that gap and deliberately not used: it
is model-visible and needs no decision, which would be exactly right, but the
published spec is inconsistent about whether PreToolUse accepts it. That is not
a safe thing to guess at, because unknown keys don't degrade — they fail the
harness's schema validation, which discards the WHOLE payload (taking the
`systemMessage` that does work with it) and replaces it with a hook-error
notice. Worth revisiting against a live hook; until someone does, only keys
confirmed for the event go on stdout. See code.claude.com/docs/en/hooks.

Host-aware stdout envelope, `deny()` only: `hookSpecificOutput` is Claude
Code's documented shape specifically, not a cross-host standard, and handing
an unrecognized host a JSON shape it never asked for risks the same "unknown
keys don't degrade" failure this docstring already warns about, just aimed at
a different parser. `deny()`'s envelope is opt-out via `SDLC_HOOK_HOST` (not
inferred — there's no reliable ambient signal that a hook subprocess is
running under Claude Code, since a hooks.json command string referencing
`${CLAUDE_PLUGIN_ROOT}` is substituted into the command before exec, not
guaranteed to also land in the child's environment; guessing from its absence
would be a coin flip). This is safe specifically because every call site
already writes its denial reason to stderr independently before returning
exit 2 (see enforce-sdlc-gates.py, block-direct-gate-record.py) — dropping
the envelope loses decoration, not the message, on the cross-host
exit-2-denies channel Codex's own hooks.json is documented to honor too.

`warn()` is deliberately NOT host-gated, even though it looks like the same
shape of problem. It isn't: `warn()`'s `systemMessage` is the ONLY channel
some call sites use at all (see auto-init-gate-cycle.py's stdin-parse-failure
warn, and enforce-sdlc-gates.py's allow-with-notes warn) — no independent
stderr write backs them up, because on exit 0 a bare `sys.stderr.write` is
"a comment addressed to no one" per the note above. Silently dropping
`warn()`'s envelope on a non-Claude host would turn an already-marginal
channel into no channel — worse than shipping it to a host that might not
render it, which is merely a no-op today, not a proven regression. So `warn()`
always builds and `emit()` always writes it, on every host, until a
cross-host equivalent of `systemMessage` is identified.

Pure stdlib. Imported by the sdlc hooks that share this convention.
"""

import json
import os
import sys


def _wants_claude_deny_envelope():
    host = os.environ.get("SDLC_HOOK_HOST", "claude").strip().lower()
    return host == "claude"


def emit(payload):
    """Write one hook JSON object to stdout. A `None` payload — `deny()`
    opting out of its envelope on a declared non-Claude host, see module
    docstring — writes nothing."""
    if payload is None:
        return
    sys.stdout.write(json.dumps(payload))


def warn(prefix, *messages):
    """User-facing caveat that accompanies an exit-0 return — an allow, or a
    hook declining to decide at all. See the module docstring for why this
    audience, and not the agent, and why (unlike `deny()`) this is never
    host-gated."""
    return {"systemMessage": "\n".join(f"[gate] {prefix}: {m}" for m in messages)}


def deny(reason):
    """The documented PreToolUse denial payload.

    Exit 2 is what actually does the blocking, and callers pair it with this:
    exit 2 routes stderr to the model, so the prose there is what gets read,
    and the block survives even if this payload is ever rejected by schema
    validation. A gate has to fail closed — hence belt and braces, in that
    order. Returns `None` on a declared non-Claude host (see module
    docstring); the belt (exit 2 + stderr, written independently by every
    caller) still denies without it."""
    if not _wants_claude_deny_envelope():
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def notify(prefix, message, surface=True):
    """PostToolUse breadcrumb: write it and return the exit code that decides
    whether the model actually sees it. Non-blocking either way — the tool
    already ran.

    `surface=False` writes the line but returns 0, leaving it in the debug log.
    That is the right choice for routine outcomes the caller has no reason to
    act on — otherwise every skill run in an ungated repo nags. Reserve
    surfacing for what someone would want to do something about."""
    sys.stderr.write(f"[gate] {prefix}: {message}\n")
    return 2 if surface else 0
