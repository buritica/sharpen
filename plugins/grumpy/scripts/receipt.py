#!/usr/bin/env python3
"""Write the receipt a grumpy pass leaves when it finishes.

Usage:
  receipt.py <simplify|review|imagine|fix> [--stage review|imagine] [--worktree <path>]

Writes `<worktree>/.sharpen/grumpy/<branch>/receipts/<pass>.json`, next to the
report the pass already wrote, naming the commit the pass ran against and the
sha256 of that report. No report on disk (or one left from the last run), no
receipt. The
sdlc gate chain records a skill gate only from a receipt that names the
branch's current HEAD (`record-gate.py --verify <pass>`), so a pass that was
loaded but never finished earns nothing, on any host.

`fix --stage review` (or `imagine`) names which review the fix run resolved
and writes `fix-review.json` (or `fix-imagine.json`), so one fix run earns one
fix gate (sdlc 4.17.0+). It refreshes `fix.json` too, the name older sdlc
reads. Without `--stage` only `fix.json` is written, and any stage files
from an earlier run are removed, so sdlc 4.17+ (which prefers a stage file)
reads the fresh `fix.json` instead of a stale stage receipt.

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

# Where grumpy's per-branch artifacts live, relative to the worktree root.
# sdlc reads receipts from here and can't import this file, so it keeps its own
# copy in `artifact_dirs.GRUMPY_ROOT`; a test in sdlc pins the two together.
# Before grumpy 2.15 this was `.claude/grumpy/`, which Claude Code treats as
# its own config and a headless session refuses to write.
GRUMPY_ROOT = (".sharpen", "grumpy")
LEGACY_GRUMPY_ROOT = (".claude", "grumpy")
FIX_STAGES = ("review", "imagine")
# The report each pass writes next to its receipt, before the receipt.
REPORT_FOR = {p: f"{p}.md" for p in PASSES}


def _git(wt, *args):
    return subprocess.check_output(
        ["git", "-C", wt, *args], stderr=subprocess.DEVNULL, text=True
    ).strip()


def _ensure_self_ignored(grumpy_root):
    """Drop a `.gitignore` holding `*` into `.sharpen/grumpy/`, so reports and
    receipts stay out of git even in a repo whose own `.gitignore` never
    listed them. Same text the grumpy commands write; an existing file is
    left alone."""
    path = os.path.join(grumpy_root, ".gitignore")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except OSError:
        return
    try:
        with os.fdopen(fd, "w") as f:
            f.write("*\n")
    except OSError:
        # A half-written file would be "left alone" forever; remove it so the
        # next write tries again.
        try:
            os.unlink(path)
        except OSError:
            pass


def _report_digest(report, previous_receipts):
    """sha256 of the pass's report, or ValueError when there is no fresh one.

    A receipt is evidence that the pass ran AND left its report. A pass that
    couldn't save its report (a host that won't write where it was told, say)
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
    previous = [
        (os.path.getmtime(p), p) for p in previous_receipts if os.path.exists(p)
    ]
    if previous:
        newest, newest_path = max(previous)
        if os.path.getmtime(report) <= newest:
            raise ValueError(
                f"report predates the last receipt "
                f"({os.path.basename(newest_path)}): save {report} again"
            )
    return hashlib.sha256(data).hexdigest()


def write_receipt(pass_name, worktree=".", stage=None, executor=None):
    if pass_name not in PASSES:
        raise ValueError(f"unknown pass {pass_name!r} (one of: {', '.join(PASSES)})")
    if stage is not None and (pass_name != "fix" or stage not in FIX_STAGES):
        raise ValueError("--stage is review or imagine, and only for fix")
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
    artifacts = os.path.join(root, *GRUMPY_ROOT, branch)
    directory = os.path.join(artifacts, "receipts")
    name = f"fix-{stage}" if stage else pass_name
    path = os.path.join(directory, f"{name}.json")
    report = os.path.join(artifacts, REPORT_FOR[pass_name])
    # Every fix receipt counts as "the last receipt": the second fix run must
    # leave a newer report than the first one's receipt, whatever its name.
    # The legacy folder's receipts count too: the first fix run after an
    # upgrade copies the old fix.md over, and that copy must not pass for a
    # report this run saved.
    legacy = os.path.join(root, *LEGACY_GRUMPY_ROOT, branch, "receipts")
    names = ("fix", "fix-review", "fix-imagine") if pass_name == "fix" else (name,)
    siblings = [
        os.path.join(d, f"{n}.json") for d in (directory, legacy) for n in names
    ]
    digest = _report_digest(report, siblings)
    os.makedirs(directory, exist_ok=True)
    _ensure_self_ignored(os.path.join(root, *GRUMPY_ROOT))
    receipt = {
        "protocol_version": "1",
        "skill": pass_name,
        "branch": branch,
        "head": head,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "report": REPORT_FOR[pass_name],
        "report_sha256": digest,
    }
    if stage:
        receipt["stage"] = stage
    if executor:
        # Which path ran the pass, when it wasn't an agent host following the
        # command (oneshot.py: {"host": "oneshot", "provider", "model"}).
        # sdlc shows it next to the gate in --status.
        receipt["executor"] = dict(executor)
    # A stage receipt also refreshes fix.json, the one name sdlc before 4.17
    # reads, so a newer grumpy with an older installed sdlc still records the
    # fix gates. sdlc 4.17+ prefers the stage file.
    # The stage file first: if the fix.json refresh then fails, sdlc 4.17+
    # still has the receipt it reads, and the error names what is missing.
    _write_json(directory, path, receipt)
    if stage:
        try:
            _write_json(directory, os.path.join(directory, "fix.json"), receipt)
        except OSError as e:
            raise PartialReceipt(path, e) from e
    elif pass_name == "fix":
        # A stageless run supersedes earlier stage receipts; left in place,
        # sdlc 4.17+ would read them instead of this fix.json.
        for n in FIX_STAGES:
            stale = os.path.join(directory, f"fix-{n}.json")
            if os.path.exists(stale):
                os.unlink(stale)
    return path, receipt


def legacy_receipts_note(worktree="."):
    """A hint for stderr when an older grumpy left receipts for this branch
    under `.claude/grumpy/`: a session there may still run sdlc 4.20 or older,
    which reads receipts only from that folder and never sees this one. None
    when there is no such folder."""
    root = _git(worktree, "rev-parse", "--show-toplevel")
    branch = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    if not os.path.isdir(os.path.join(root, *LEGACY_GRUMPY_ROOT, branch, "receipts")):
        return None
    return (
        f"[grumpy] note: receipts from an older grumpy are under "
        f"{os.path.join(*LEGACY_GRUMPY_ROOT, branch)}/. sdlc 4.21+ reads this "
        f"receipt; sdlc 4.20 or older reads only that folder and will say "
        f"'no receipt yet'. If it does, upgrade sdlc to 4.21+.\n"
    )


class PartialReceipt(OSError):
    """The stage receipt was written; only the fix.json copy failed."""

    def __init__(self, path, cause):
        super().__init__(f"{path} written, fix.json not refreshed: {cause}")
        self.path = path
        self.cause = cause


def _write_json(directory, path, data):
    name = os.path.splitext(os.path.basename(path))[0]
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


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
    stage = None
    if "--stage" in args:
        i = args.index("--stage")
        if i + 1 >= len(args):
            sys.stderr.write("--stage needs review or imagine\n")
            return 1
        stage = args[i + 1]
        del args[i : i + 2]
    if len(args) != 1:
        sys.stderr.write(__doc__.split("\n\n")[1] + "\n")
        return 1
    try:
        path, receipt = write_receipt(args[0], worktree, stage)
    except PartialReceipt as e:
        # sdlc 4.17+ reads the stage file, which is in place; don't retry
        # (the report is now older than it). Only an older sdlc misses out.
        sys.stderr.write(
            f"[grumpy] fix receipt written to {e.path}; sdlc 4.17+ records "
            f"from it. Copying it to fix.json for older sdlc failed: {e.cause}\n"
        )
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        sys.stderr.write(f"[grumpy] receipt not written: {e}\n")
        return 1
    sys.stdout.write(
        f"[grumpy] {receipt['skill']} receipt for {receipt['branch']} "
        f"@ {receipt['head'][:12]} -> {path}\n"
    )
    try:
        note = legacy_receipts_note(worktree)
    except (OSError, subprocess.CalledProcessError):
        note = None
    if note:
        sys.stderr.write(note)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
