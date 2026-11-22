#!/usr/bin/env python3
"""Read the commands a repo declares in its AGENTS.md managed SDLC block.

`/sdlc:init` (step 10, via `agents_md.py`) writes the repo's test, lint,
format and typecheck commands into the `### Commands` section of the managed
block. `/sdlc:gate` runs the same gates, so it reads them from here first and
falls back to its curated auto-detection table only for a command the block
doesn't declare. One source of truth: when the two disagree, the block wins,
because it is what the repo says it runs.

Usage:
  declared_commands.py [--root DIR]

Prints one JSON object:

  {"source": "AGENTS.md" | null,      # null: no file, or no managed block
   "version": "4.19.0" | "" | null,   # sdlc version stamped in the block
   "stale": true | false,             # stamped by a different sdlc than this
   "commands": {"test": [...] | null, "lint": ..., "format": ..., "typecheck": ...},
   "unreadable": ["test", ...]}       # lines a human edited past parsing

A command is a list (a monorepo declares one per area, the area named inside
the value, e.g. "web: bun test") or null when the block says "not
configured" or omits it. A line that is neither a backticked command nor
"not configured" (an annotation added inside the markers) reads as null and
is listed in `unreadable`, so the gate can say so instead of silently
falling back. Exit 0, or 2 when AGENTS.md can't be read
or its markers are broken. Pure stdlib.
"""

import argparse
import json
import os
import re
import sys

# Same-directory sibling, the way agents_md.py imports gate_store.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agents_md as am  # noqa: E402

NAMES = ("test", "lint", "format", "typecheck")
_ENTRY = re.compile(r"^- (%s):(.*)$" % "|".join(NAMES))
# Anything that looks like an entry for one of NAMES but isn't init's exact
# shape (`- Test:`, `* test:`): a hand edit, reported rather than skipped.
_LOOSE_ENTRY = re.compile(r"^\s*[-*]\s*(%s)\s*:" % "|".join(NAMES), re.I)
_NESTED = re.compile(r"^\s+- `(.+)`\s*$")


def _unquote(value):
    """(command | None, readable). Readable is False for a value that is
    neither one backticked command nor init's "not configured" line."""
    value = value.strip()
    if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
        inner = value[1:-1].strip()
        return (inner or None), True
    return None, (not value or value == am.NOT_CONFIGURED)


def parse(text):
    """({name: [commands] | None}, [unreadable names]) from AGENTS.md text,
    or None when the text has no managed block. Only the `### Commands` section inside the markers
    counts; prose elsewhere that looks like a command list is ignored."""
    lines = text.splitlines()
    span = am.marker_span(lines)
    if not span:
        return None
    commands = dict.fromkeys(NAMES)
    unreadable = []
    in_section = False
    current = None
    seen = set()

    def flag(name):
        if name not in unreadable:
            unreadable.append(name)

    for line in lines[span[0] + 1 : span[1]]:
        if line.startswith("### "):
            in_section = line.strip() == "### Commands"
            current = None
            continue
        if not in_section or not line.strip():
            continue
        entry = _ENTRY.match(line)
        if entry:
            current, rest = entry.group(1), entry.group(2).strip()
            if current in seen:
                flag(current)  # two `- test:` lines: which one is meant?
            seen.add(current)
            value, readable = _unquote(rest)
            commands[current] = [value] if value else None
            if not readable:
                flag(current)
            continue
        loose = _LOOSE_ENTRY.match(line)
        if loose:
            current = None
            flag(loose.group(1).lower())
            continue
        nested = _NESTED.match(line)
        if nested and current:
            commands[current] = (commands[current] or []) + [nested.group(1)]
            continue
        if current and line[:1].isspace():
            flag(current)  # an indented item that isn't a backticked command
            continue
        current = None  # init's own prose lines (`- deploy:`, `- CI runs …`)
    for name in unreadable:
        commands[name] = None  # never run half of what a hand edit meant
    return commands, unreadable


def read(root):
    path = os.path.join(root, "AGENTS.md")
    if not os.path.exists(path):
        text = None
    else:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    parsed = parse(text) if text is not None else None
    if parsed is None:
        return {
            "source": None,
            "version": None,
            "stale": False,
            "commands": dict.fromkeys(NAMES),
            "unreadable": [],
        }
    commands, unreadable = parsed
    version = am.block_version(text)
    return {
        "source": "AGENTS.md",
        "version": version,
        "stale": version != am.plugin_version(),
        "commands": commands,
        "unreadable": unreadable,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="declared_commands.py",
        description="Print the commands AGENTS.md's managed SDLC block declares.",
    )
    parser.add_argument("--root", default=".", help="repo root (default: cwd)")
    args = parser.parse_args(argv)
    try:
        result = read(args.root)
    except (am.WireError, OSError, UnicodeDecodeError) as exc:
        print("declared_commands: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
