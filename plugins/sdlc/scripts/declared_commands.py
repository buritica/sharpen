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
   "commands": {"test": [...] | null, "lint": ..., "format": ..., "typecheck": ...}}

A command is a list (a monorepo declares one per area) or null when the block
says "not configured" or omits it. Exit 0, or 2 when AGENTS.md can't be read
or its markers are broken. Pure stdlib.
"""

import argparse
import json
import os
import re
import sys

import agents_md as am

NAMES = ("test", "lint", "format", "typecheck")
_ENTRY = re.compile(r"^- (%s):(.*)$" % "|".join(NAMES))
_NESTED = re.compile(r"^\s+- `(.+)`\s*$")


def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
        return value[1:-1]
    return None  # "not configured — …", or anything that isn't a command


def parse(text):
    """{name: [commands] | None} from AGENTS.md text, or None when the text
    has no managed block. Only the `### Commands` section inside the markers
    counts; prose elsewhere that looks like a command list is ignored."""
    lines = text.splitlines()
    span = am.marker_span(lines)
    if not span:
        return None
    commands = dict.fromkeys(NAMES)
    in_section = False
    current = None
    for line in lines[span[0] + 1 : span[1]]:
        if line.startswith("### "):
            in_section = line.strip() == "### Commands"
            current = None
            continue
        if not in_section:
            continue
        entry = _ENTRY.match(line)
        if entry:
            current, rest = entry.group(1), entry.group(2).strip()
            value = _unquote(rest) if rest else None
            commands[current] = [value] if value else None
            continue
        nested = _NESTED.match(line)
        if nested and current:
            commands[current] = (commands[current] or []) + [nested.group(1)]
            continue
        current = None
    return commands


def read(root):
    path = os.path.join(root, "AGENTS.md")
    if not os.path.exists(path):
        text = None
    else:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    commands = parse(text) if text is not None else None
    if commands is None:
        return {
            "source": None,
            "version": None,
            "stale": False,
            "commands": dict.fromkeys(NAMES),
        }
    version = am.block_version(text)
    return {
        "source": "AGENTS.md",
        "version": version,
        "stale": version != am.plugin_version(),
        "commands": commands,
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
