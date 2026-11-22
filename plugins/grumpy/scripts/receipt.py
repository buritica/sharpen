#!/usr/bin/env python3
"""Write the receipt a grumpy pass leaves when it finishes.

Usage:
  receipt.py <simplify|review|imagine|fix> [--worktree <path>]

Writes `<worktree>/.claude/grumpy/<branch>/receipts/<pass>.json`, next to the
report the pass already wrote, naming the commit the pass ran against and the
sha256 of that report. No report on disk (or one left from the last run), no
receipt. The
sdlc gate chain records a skill gate only from a receipt that names the
branch's current HEAD (`record-gate.py --verify <pass>`), so a pass that was
loaded but never finished earns nothing, on any host.

Run it as the pass's LAST step, after committing whatever the pass changed, so
the receipt names the commit the work landed in. A dirty tree is refused.

The format is `schemas/grumpy-receipt.v1.schema.json`. It is a record that the
work ran, not a security boundary. Pure stdlib.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

PASSES = ("simplify", "review", "imagine", "fix")
# The report each pass writes next to its receipt, before the receipt.
REPORT_FOR = {p: f"{p}.md" for p in PASSES}


def _git(wt, *args):
    return subprocess.check_output(
        ["git", "-C", wt, *args], stderr=subprocess.DEVNULL, text=True
    ).strip()


def _report_digest(report, previous_receipt):
    """sha256 of the pass's report, or ValueError when there is no fresh one.

    A receipt is evidence that the pass ran AND left its report. A pass that
    couldn't save its report (a host that won't write under .claude/, say)
    printed it instead, and must not earn a gate: there is nothing on disk
    for the gate to point at. A report no newer than this pass's previous
    receipt is a leftover from the last run, not this one."""
    try:
        with open(report, "rb") as f:
            data = f.read()
    except OSError:
        raise ValueError(f"report not written ({report}): save it first") from None
    if not data.strip():
        raise ValueError(f"report is empty ({report})")
    if os.path.exists(previous_receipt) and os.path.getmtime(
        report
    ) <= os.path.getmtime(previous_receipt):
        raise ValueError(f"report predates the last receipt ({report}): save it again")
    return hashlib.sha256(data).hexdigest()


def write_receipt(pass_name, worktree="."):
    if pass_name not in PASSES:
        raise ValueError(f"unknown pass {pass_name!r} (one of: {', '.join(PASSES)})")
    branch = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise ValueError("detached HEAD: a receipt needs a named branch")
    if branch in ("main", "master"):
        # Gates track feature branches. A pass that lands here was most likely
        # pointed at the wrong worktree (a lost --worktree).
        raise ValueError(f'"{branch}" has no gate cycle; is --worktree right?')
    if _git(worktree, "status", "--porcelain", "--untracked-files=no"):
        # The receipt names HEAD. Uncommitted edits (simplify's applied fixes,
        # fix's leftovers) would land in a later commit the pass never saw,
        # and that commit would make every gate stale at PR time.
        raise ValueError("uncommitted changes: commit the pass's edits first")
    root = _git(worktree, "rev-parse", "--show-toplevel")
    head = _git(worktree, "rev-parse", "HEAD")
    artifacts = os.path.join(root, ".claude", "grumpy", branch)
    directory = os.path.join(artifacts, "receipts")
    path = os.path.join(directory, f"{pass_name}.json")
    report = os.path.join(artifacts, REPORT_FOR[pass_name])
    digest = _report_digest(report, path)
    os.makedirs(directory, exist_ok=True)
    receipt = {
        "protocol_version": "1",
        "skill": pass_name,
        "branch": branch,
        "head": head,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "report": REPORT_FOR[pass_name],
        "report_sha256": digest,
    }
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{pass_name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(receipt, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
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
