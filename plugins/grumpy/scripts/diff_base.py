#!/usr/bin/env python3
"""Print the base a grumpy pass diffs the branch against.

Usage:
  diff_base.py [--worktree <path>] [--why]

On a stacked branch (main <- A <- B <- this one), diffing against main
reviews every layer below: the passes re-review A and B, and a fix pass edits
their code. The base is owned by sdlc, which resolves it for the whole gate
chain and writes `.claude/sdlc/<branch>/base.json`; this reads that file
first. Without sdlc (or before its first `--init`), it applies the same
order itself:

  1. `.claude/sdlc/<branch>/base.json` (sdlc's resolved base)
  2. `git config branch.<branch>.sdlcBase`
  3. the open PR's base branch, via `gh pr view`, when `gh` is installed
  4. the branch's upstream, when it is another branch
  5. the default branch (origin/HEAD, origin/main, origin/master, main, master)

Prints the ref (pass it to `git diff "$BASE"...HEAD`), and with `--why` a
second line saying where it came from. Exits 1 with nothing on stdout when no
base resolves. Pure stdlib.
"""

import json
import os
import shutil
import subprocess
import sys


def _git(wt, *args):
    try:
        return subprocess.run(
            ["git", "-C", wt, *args], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _exists(wt, ref):
    return bool(ref) and bool(
        _git(wt, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
    )


def _from_sdlc(wt, root, branch):
    path = os.path.join(root, ".claude", "sdlc", branch, "base.json")
    try:
        with open(path) as f:
            base = json.load(f)
    except (OSError, ValueError):
        return None
    ref = base.get("ref") if isinstance(base, dict) else None
    return ref if _exists(wt, ref) else None


def _from_pr(wt, branch):
    if not shutil.which("gh"):
        return None
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", branch, "--json", "baseRefName,state"],
            cwd=wt,
            capture_output=True,
            text=True,
            timeout=10,
        )
        info = json.loads(proc.stdout) if proc.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    name = info.get("baseRefName") if info.get("state") == "OPEN" else None
    for ref in (f"origin/{name}", name) if name else ():
        if _exists(wt, ref):
            return ref
    return None


def _from_upstream(wt, branch):
    up = _git(wt, "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}")
    if not up or up == branch or up.split("/", 1)[-1] == branch:
        return None
    return up if _exists(wt, up) else None


def _default(wt):
    symbolic = _git(
        wt, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"
    )
    for ref in (symbolic, "origin/main", "origin/master", "main", "master"):
        if _exists(wt, ref):
            return ref
    return None


def resolve(wt="."):
    """(ref, why) or (None, why)."""
    branch = _git(wt, "rev-parse", "--abbrev-ref", "HEAD")
    root = _git(wt, "rev-parse", "--show-toplevel")
    if not branch or branch == "HEAD":
        return None, "not on a named branch"
    steps = (
        (
            "sdlc's resolved base (.claude/sdlc/<branch>/base.json)",
            lambda: _from_sdlc(wt, root, branch),
        ),
        (
            "git config branch.<branch>.sdlcBase",
            lambda: _git(wt, "config", "--get", f"branch.{branch}.sdlcBase") or None,
        ),
        ("the open PR's base branch", lambda: _from_pr(wt, branch)),
        ("the branch's upstream", lambda: _from_upstream(wt, branch)),
        ("the default branch", lambda: _default(wt)),
    )
    for why, find in steps:
        ref = find()
        if ref and _exists(wt, ref):
            return ref, why
    return None, "no base resolves"


def main(argv):
    wt = "."
    if "--worktree" in argv:
        i = argv.index("--worktree")
        if i + 1 >= len(argv):
            print("--worktree needs a path", file=sys.stderr)
            return 2
        wt = argv[i + 1]
    ref, why = resolve(wt)
    if not ref:
        print(f"[grumpy] {why}", file=sys.stderr)
        return 1
    print(ref)
    if "--why" in argv:
        print(why)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
