#!/usr/bin/env python3
"""Print the base a grumpy pass diffs the branch against.

Usage:
  diff_base.py [--worktree <path>] [--why]

On a stacked branch (main <- A <- B <- this one), diffing against main
reviews every layer below: the passes re-review A and B, and a fix pass edits
their code. The base is owned by sdlc, which resolves it for the whole gate
chain and writes `.sharpen/sdlc/<branch>/base.json` (`.claude/sdlc/` before
sdlc 4.21); this reads that file
first. Without sdlc (or before its first `--init`), it applies the same
order itself:

  1. `.sharpen/sdlc/<branch>/base.json`, or the legacy `.claude/sdlc/` one
     (sdlc's resolved base)
  2. `git config branch.<branch>.sdlcBase`
  3. the open PR's base branch, via `gh pr view`, when `gh` is installed
  4. the branch's upstream, when it is a local branch
  5. the default branch (origin/HEAD, origin/main, origin/master, main, master)

Prints the ref (pass it to `git diff "$BASE"...HEAD`), and with `--why` a
second line saying where it came from. A source that is set but doesn't
resolve (base.json or sdlcBase naming a renamed or deleted branch, a `gh`
call that failed rather than finding no PR) prints a `[grumpy]` line on
stderr before the next source is tried. Exits 1 with nothing on stdout when no
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


def _note(msg):
    print(f"[grumpy] {msg}", file=sys.stderr)


# Where sdlc writes its per-branch files. grumpy can't import sdlc, so this is
# a copy of `artifact_dirs.SDLC_ROOT`; a test in sdlc pins the two together.
# The legacy root is where sdlc before 4.21 wrote them, read only when the new
# one has no base.json.
SDLC_ROOT = (".sharpen", "sdlc")
LEGACY_SDLC_ROOT = (".claude", "sdlc")


def _from_sdlc(wt, root, branch):
    for sdlc_root in (SDLC_ROOT, LEGACY_SDLC_ROOT):
        path = os.path.join(root, *sdlc_root, branch, "base.json")
        try:
            with open(path) as f:
                base = json.load(f)
            break
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as e:
            _note(f"sdlc's {path} is unreadable ({e}); trying the next source")
            return None
    else:
        return None
    ref = base.get("ref") if isinstance(base, dict) else None
    if _exists(wt, ref):
        return ref
    _note(
        f"sdlc's base.json names {ref!r}, which doesn't resolve (renamed or "
        "deleted?); trying the next source. Fix it with "
        "`record-gate.py --set-base <parent>` (or `auto`)."
    )
    return None


def _from_config(wt, branch):
    ref = _git(wt, "config", "--get", f"branch.{branch}.sdlcBase")
    if not ref or _exists(wt, ref):
        return ref or None
    _note(
        f"branch.{branch}.sdlcBase is {ref!r}, which doesn't resolve; trying the next source"
    )
    return None


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
        if proc.returncode != 0:
            if "no pull requests found" not in proc.stderr.lower():
                err = (proc.stderr.strip().splitlines() or ["no output"])[-1]
                _note(f"`gh pr view` failed ({err}); skipping the PR base")
            return None
        info = json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        _note(f"`gh pr view` failed ({type(e).__name__}); skipping the PR base")
        return None
    name = info.get("baseRefName") if info.get("state") == "OPEN" else None
    for ref in (f"origin/{name}", name) if name else ():
        if _exists(wt, ref):
            return ref
    return None


def _from_upstream(wt, branch):
    up = _git(wt, "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}")
    if not up or up == branch:
        return None
    if _git(wt, "config", "--get", f"branch.{branch}.remote") != ".":
        # Only a local upstream is a parent. A remote-tracking one may be the
        # branch's own pushed copy (after a rename, or `push -u origin
        # HEAD:other`), which would scope review to the unpushed commits.
        # Same rule as sdlc's gate_base._upstream.
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
            "sdlc's resolved base (.sharpen/sdlc/<branch>/base.json)",
            lambda: _from_sdlc(wt, root, branch),
        ),
        (
            "git config branch.<branch>.sdlcBase",
            lambda: _from_config(wt, branch),
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
