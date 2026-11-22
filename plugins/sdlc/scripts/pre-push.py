#!/usr/bin/env python3
"""
git pre-push hook: the host-agnostic floor under the PR gate.

The PreToolUse hooks see a PR being opened only when an agent host runs it
through a tool they can read (pr_paths.py). They never see `make pr`, a script,
a user's gh alias, `curl`, or a person who pushes and then opens the PR in the
browser, and instructions-only hosts (Gemini, Cursor, Copilot) have no hooks at
all. A push is the one step every one of those paths shares, so this checks the
same thing the PR hook checks, for each branch being pushed: every gate the
cycle requires is recorded, and the review gates are not stale for the commit
being pushed (gate_receipts.stale_skill_gates, the same rule enforce uses).

It warns by default and blocks only when asked, because a cycle arms on a
branch's first commit and the gates finish at the end: a blocking default would
stop nearly every mid-work push. Mode, from `git config sharpen.prePush`:

    warn   (default) print what is missing, let the push through
    block  refuse the push until the gates are done
    off    say nothing

`SDLC_ALLOW_PUSH=1 git push` lets one push through in block mode. That is the
escape hatch, not `--no-verify`, which /sdlc:ship forbids.

Opt-in like the PR gate: a branch with no cycle is not checked. Failures in
this script fail open with a message, so a broken hook never wedges a push.

Installed by install-pre-push.py as a shim in the repo's common hooks dir.
Pure stdlib. @fires-on git pre-push, @blocking (block mode only)
"""

import os
import subprocess
import sys

import gate_receipts as rc
import gate_store as gs

MODES = ("warn", "block", "off")


def mode(cwd=None):
    try:
        value = gs._git("config", "--get", "sharpen.prePush", cwd=cwd).lower()
    except (OSError, subprocess.CalledProcessError):
        return "warn"
    return value if value in MODES else "warn"


def pushed_branches(lines):
    """(branch, local sha) for each branch update on stdin. Deletes (a zero
    local sha) and non-branch refs (tags) are not PRs."""
    out = []
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha = parts[0], parts[1]
        if local_sha.strip("0") == "" or not local_ref.startswith("refs/heads/"):
            continue
        out.append((local_ref[len("refs/heads/") :], local_sha))
    return out


def problems(data, branch, sha):
    """Why `branch` at `sha` isn't ready for a PR, or None. Same checks, same
    order, as enforce-sdlc-gates: missing gates first, then stale ones."""
    bd = data.get(branch)
    if not bd:
        return None
    missing = gs.missing_gates(bd)
    if missing:
        return (
            f'gates incomplete for "{branch}" (tier "{bd.get("tier")}"): '
            f"missing {', '.join(missing)}."
        )
    stale = rc.stale_skill_gates(bd, sha)
    if stale:
        return rc.stale_message(bd, branch, stale)
    return None


def check(lines, cwd=None):
    """-> (exit code, message or None)."""
    m = mode(cwd)
    if m == "off":
        return 0, None
    branches = pushed_branches(lines)
    if not branches:
        return 0, None
    data = gs.load_store(gs.default_store_path(cwd=cwd))
    found = [p for b, sha in branches if (p := problems(data, b, sha))]
    if not found:
        return 0, None
    body = "\n".join(f"  - {p}" for p in found)
    if m == "block" and os.environ.get("SDLC_ALLOW_PUSH") != "1":
        return 1, (
            "[sdlc] pre-push: refusing the push.\n"
            f"{body}\n"
            "Run /sdlc:gate to finish the chain. To push anyway (a backup, "
            "not a PR): SDLC_ALLOW_PUSH=1 git push. To warn instead of "
            "block: git config sharpen.prePush warn"
        )
    return 0, (
        "[sdlc] pre-push: pushing, but this branch is not ready for a PR.\n"
        f"{body}\n"
        "Run /sdlc:gate before opening one. (git config sharpen.prePush block "
        "makes this refuse the push; off silences it.)"
    )


def main():
    try:
        code, message = check(sys.stdin.read().splitlines())
    except gs.StoreCorruptError as e:
        code, message = 0, f"[sdlc] pre-push: gate store unreadable ({e}); not checked."
    except Exception as e:  # never wedge a push on our own bug
        code, message = 0, f"[sdlc] pre-push: unexpected error, not checked: {e}"
    if message:
        sys.stderr.write(message + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
