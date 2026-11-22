#!/usr/bin/env python3
"""Write the receipt a grumpy pass leaves when it finishes.

Usage:
  receipt.py <simplify|review|imagine|fix> [--worktree <path>]

Writes `<worktree>/.claude/grumpy/<branch>/receipts/<pass>.json`, next to the
report the pass already wrote, naming the commit the pass ran against. The
sdlc gate chain records a skill gate only from a receipt that names the
branch's current HEAD (`record-gate.py --verify <pass>`), so a pass that was
loaded but never finished earns nothing, on any host.

Run it as the pass's LAST step. For fix, that means after its commit, so the
receipt names the commit the fixes landed in.

The format is `schemas/grumpy-receipt.v1.schema.json`. It is a record that the
work ran, not a security boundary. Pure stdlib.
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

PASSES = ("simplify", "review", "imagine", "fix")


def _git(wt, *args):
    return subprocess.check_output(
        ["git", "-C", wt, *args], stderr=subprocess.DEVNULL, text=True
    ).strip()


def write_receipt(pass_name, worktree="."):
    if pass_name not in PASSES:
        raise ValueError(f"unknown pass {pass_name!r} (one of: {', '.join(PASSES)})")
    branch = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise ValueError("detached HEAD: a receipt needs a named branch")
    root = _git(worktree, "rev-parse", "--show-toplevel")
    head = _git(worktree, "rev-parse", "HEAD")
    directory = os.path.join(root, ".claude", "grumpy", branch, "receipts")
    os.makedirs(directory, exist_ok=True)
    receipt = {
        "protocol_version": "1",
        "skill": pass_name,
        "branch": branch,
        "head": head,
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    path = os.path.join(directory, f"{pass_name}.json")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{pass_name}.", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(receipt, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return path, receipt


def main(argv):
    worktree = "."
    args = list(argv)
    if "--worktree" in args:
        i = args.index("--worktree")
        if i + 1 >= len(args):
            sys.stderr.write("--worktree needs a path\n")
            return 1
        worktree = args[i + 1]
        del args[i : i + 2]
    if len(args) != 1:
        sys.stderr.write(__doc__.split("\n\n")[1] + "\n")
        return 1
    try:
        path, receipt = write_receipt(args[0], worktree)
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        sys.stderr.write(f"[grumpy] receipt not written: {e}\n")
        return 1
    sys.stdout.write(
        f"[grumpy] {receipt['skill']} receipt for {receipt['branch']} "
        f"@ {receipt['head'][:12]} -> {path}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
