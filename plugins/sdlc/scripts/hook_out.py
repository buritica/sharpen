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

Cross-host note: this stdout envelope (`hookSpecificOutput`/`systemMessage`)
is Claude Code's documented shape. An earlier revision made `deny()`'s
envelope opt-out via an `SDLC_HOOK_HOST` env var, on the theory that handing
an unrecognized host a JSON shape it never asked for might trip the same
"unknown keys don't degrade" failure warned about above. Live-tested against
a real installed Codex CLI session and removed: Codex reads the same
`hooks/hooks.json` natively (`${CLAUDE_PLUGIN_ROOT}` resolves, `PreToolUse`
denies, `PostToolUse` fires) and tolerated this exact envelope with no
schema-validation fallout — the theoretical risk didn't hold up, so the
extra host-detection machinery it justified was removed rather than kept
as unexercised insurance. `warn`/`deny`/`emit` always run unconditionally.

Pure stdlib. Imported by the sdlc hooks that share this convention.
"""

import json
import sys


def emit(payload):
    """Write one hook JSON object to stdout."""
    sys.stdout.write(json.dumps(payload))


def warn(prefix, *messages):
    """User-facing caveat that accompanies an exit-0 return — an allow, or a
    hook declining to decide at all. See the module docstring for why this
    audience, and not the agent."""
    return {"systemMessage": "\n".join(f"[gate] {prefix}: {m}" for m in messages)}


def deny(reason):
    """The documented PreToolUse denial payload.

    Exit 2 is what actually does the blocking, and callers pair it with this:
    exit 2 routes stderr to the model, so the prose there is what gets read,
    and the block survives even if this payload is ever rejected by schema
    validation. A gate has to fail closed — hence belt and braces, in that
    order."""
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
