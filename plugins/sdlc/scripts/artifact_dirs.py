#!/usr/bin/env python3
"""Artifact folders that keep themselves out of git.

A `.gitignore` holding `*` inside a folder (the trick `.pytest_cache` uses)
keeps everything in it out of git, in a repo whose own `.gitignore` never
learned about it, on every host and without `/sdlc:init`. sharpen's command
files do the same for `.claude/sdlc/`, `.claude/grumpy/` and
`.claude/worktrees/` with one shell line; this module covers the folders the
stdlib scripts create. Pure stdlib.
"""

import os

SELF_IGNORE_TEXT = (
    "# Written by sharpen: everything in this folder is local state.\n*\n"
)


def ensure_self_ignored(directory):
    """Make the neutral `.sharpen/data/` folder ignore itself.

    Only `.sharpen/data/` gets one: never `.sharpen/` itself, which holds the
    committed `simplify.json`, and never an `$SDLC_GATES_PATH` override or the
    legacy `.claude/data/`, whose folders may hold things that aren't ours.
    An existing file is left alone. Best effort: a read-only folder fails
    later, at the real write, with a clearer error.
    """
    directory = os.path.abspath(directory)
    if os.path.basename(directory) != "data":
        return
    if os.path.basename(os.path.dirname(directory)) != ".sharpen":
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
