#!/usr/bin/env python3
"""Artifact folders that keep themselves out of git.

A `.gitignore` holding `*` inside a folder (the trick `.pytest_cache` uses)
keeps everything in it out of git, in a repo whose own `.gitignore` never
learned about it, on every host and without `/sdlc:init`. sharpen's command
files do the same for `.sharpen/sdlc/`, `.sharpen/grumpy/` and
`.claude/worktrees/` with one shell line; this module covers the folders the
stdlib scripts create (gate state in `.sharpen/data/`, `base.json` in
`.sharpen/sdlc/<branch>/`). Pure stdlib.

It also names where per-branch artifacts live. sdlc and grumpy share that
contract (grumpy reads sdlc's plan and base file, sdlc reads grumpy's
receipts) but can't import each other, so grumpy's `receipt.py` and
`diff_base.py` carry their own copies of the roots, and a test pins them
together. Artifacts moved from `.claude/` to `.sharpen/` in sdlc 4.21 /
grumpy 2.15, because Claude Code treats `.claude/` as its own config and a
headless session refuses to write there; the old folders are still read for
checkouts that predate the move.
"""

import os

# Per-branch artifact roots, relative to the worktree root.
SDLC_ROOT = (".sharpen", "sdlc")
GRUMPY_ROOT = (".sharpen", "grumpy")
# Where sdlc before 4.21 and grumpy before 2.15 wrote them. Read, never written.
LEGACY_SDLC_ROOT = (".claude", "sdlc")
LEGACY_GRUMPY_ROOT = (".claude", "grumpy")

SELF_IGNORE_TEXT = (
    "# Written by sharpen: everything in this folder is local state.\n*\n"
)


# (parent, folder) pairs sharpen owns outright. `.sharpen/` itself is never
# one (it holds the committed `simplify.json`), nor an `$SDLC_GATES_PATH`
# override or the legacy `.claude/data/`, whose folders may hold things that
# aren't ours.
OWNED = {
    (".sharpen", "data"),
    SDLC_ROOT,
    GRUMPY_ROOT,
    # Where sdlc before 4.21 and grumpy before 2.15 wrote; still sharpen's.
    (".claude", "sdlc"),
    (".claude", "grumpy"),
    (".claude", "worktrees"),
}


def ensure_self_ignored(directory):
    """Make one of sharpen's own artifact folders (`OWNED`) ignore itself.

    Any other folder is left untouched. An existing file is left alone. Best
    effort: a read-only folder fails later, at the real write, with a clearer
    error.
    """
    directory = os.path.abspath(directory)
    pair = (
        os.path.basename(os.path.dirname(directory)),
        os.path.basename(directory),
    )
    if pair not in OWNED:
        return
    path = os.path.join(directory, ".gitignore")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except OSError:
        return  # already there (FileExistsError), or not writable
    try:
        with os.fdopen(fd, "w") as f:
            f.write(SELF_IGNORE_TEXT)
    except OSError:
        # A half-written file would be "left alone" forever; remove it so the
        # next write tries again.
        try:
            os.unlink(path)
        except OSError:
            pass
