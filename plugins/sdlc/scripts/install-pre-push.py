#!/usr/bin/env python3
"""
Install (or refresh) sdlc's git pre-push hook in a repo.

    install-pre-push.py [--root <dir>] [--mode warn|block|off] [--check]

Writes a small shell shim to the repo's COMMON hooks dir (`git rev-parse
--git-common-dir`/hooks), so every linked worktree gets it. The shim does not
embed this plugin's path: plugin caches are version-nested and move on every
update, and a hook pointing at a deleted path would fail every push. Instead
the path lives in `git config sharpen.sdlcScripts`, which this script refreshes
on every run (`/sdlc:gate` runs it at init), and the shim fails open with a
message when that path is gone.

It never overwrites someone else's hook. If a pre-push hook that isn't ours
exists, or `core.hooksPath` points git elsewhere (husky, lefthook, a repo's
own `.githooks/`), it prints the one line to add to that hook and exits 0.

`--mode` sets `git config sharpen.prePush` (default warn; see pre-push.py).
`--check` reports whether the shim is installed and current, changing nothing.

Pure stdlib.
"""

import argparse
import os
import stat
import subprocess
import sys

MARKER = "# sdlc pre-push shim"
SCRIPTS = os.path.dirname(os.path.abspath(__file__))

SHIM = f"""#!/bin/sh
{MARKER} (installed by install-pre-push.py; safe to delete)
dir=$(git config --get sharpen.sdlcScripts)
if [ -n "$dir" ] && [ -f "$dir/pre-push.py" ]; then
  exec python3 "$dir/pre-push.py" "$@"
fi
echo "[sdlc] pre-push: sdlc scripts not found (sharpen.sdlcScripts=$dir); not checked. Run /sdlc:gate to refresh." >&2
exit 0
"""


def git(root, *args, check=True):
    r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)
    if check and r.returncode:
        raise SystemExit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def chain_line():
    """What to add to a pre-push hook someone else owns. Path-free for the same
    reason as the shim: the scripts dir moves on every plugin update. A hook
    that also reads stdin must capture it once and feed this line a copy."""
    return (
        'dir=$(git config --get sharpen.sdlcScripts); [ -n "$dir" ] && '
        '[ -f "$dir/pre-push.py" ] && { python3 "$dir/pre-push.py" "$@" || exit $?; }'
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=".")
    ap.add_argument("--mode", choices=("warn", "block", "off"))
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)

    common = git(a.root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    hooks_path = git(a.root, "config", "--get", "core.hooksPath", check=False)
    hook = os.path.join(common, "hooks", "pre-push")
    ours = os.path.exists(hook) and MARKER in open(hook, errors="replace").read()

    if a.check:
        current = git(a.root, "config", "--get", "sharpen.sdlcScripts", check=False)
        if hooks_path or not ours:
            print("pre-push: not installed")
            return 1
        print(
            f"pre-push: installed ({'current' if current == SCRIPTS else 'stale path'})"
        )
        return 0 if current == SCRIPTS else 1

    git(a.root, "config", "sharpen.sdlcScripts", SCRIPTS)
    if a.mode:
        git(a.root, "config", "sharpen.prePush", a.mode)

    if hooks_path or (os.path.exists(hook) and not ours):
        where = hooks_path or hook
        print(
            f"pre-push: not installed, because git hooks are managed elsewhere ({where}).\n"
            f"Add this line to that pre-push hook to chain sdlc's check:\n  {chain_line()}"
        )
        return 0
    if not ours:
        os.makedirs(os.path.dirname(hook), exist_ok=True)
        with open(hook, "w") as f:
            f.write(SHIM)
        os.chmod(
            hook, os.stat(hook).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        print(f"pre-push: installed at {hook}")
    else:
        print("pre-push: already installed; scripts path refreshed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
