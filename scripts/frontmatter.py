#!/usr/bin/env python3
"""
A stdlib-only parser for the frontmatter shapes actually used by this repo's
plugin command files (plugins/*/commands/*.md) — deliberately NOT a general
YAML parser. Every shape it handles was found by dumping every command
file's real frontmatter block before writing this module; it errors on
anything else rather than silently guessing at a shape it wasn't built for.

Shapes handled:
  - `key: "quoted string"` — single-line quoted scalar.
  - `key: bareword` — single-line unquoted scalar (e.g. guard.md's
    `name: guard`, `model: haiku`, and `allowed-tools: Bash(python3:*)` —
    a bare Claude Code tool-matcher pattern, not an array).
  - `key:` followed by indented continuation lines, joined with spaces — a
    YAML "folded" multi-line scalar (most grumpy commands' `description:`).
  - `key: ["a", "b"]` — a single-line JSON-style array.
  - `key:` followed by an indented `[`, one item per line, closing `]`,
    with a trailing comma before the close — a multi-line JSON-style array
    (cleanup.md/fix.md's `allowed-tools:`). The trailing comma makes it
    invalid JSON as written, so it's stripped before parsing.

Used by generate-skill.py to translate Claude Code command frontmatter into
the cross-host SKILL.md shape (name + description) without hand-duplicating
23 files and letting them drift.
"""

import json
import re

_KEY_RE = re.compile(r"^([A-Za-z_][\w-]*):\s*(.*)$")
_TRAILING_COMMA_RE = re.compile(r",\s*\]$")


def split(text):
    """Return (frontmatter_text, body_text). Raises ValueError if `text`
    doesn't start with a `---`-delimited frontmatter block, or if that block
    never closes. Only the FIRST closing `---` ends the block — a `---`
    later in the body (e.g. a markdown horizontal rule) doesn't confuse it."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError("no frontmatter block (must start with '---')")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1 :])
    raise ValueError("frontmatter block never closes with '---'")


def parse(frontmatter_text):
    """Parse a frontmatter block (the text BETWEEN the `---` delimiters,
    e.g. `split()`'s first return value) into a dict. Values are `str` or
    `list[str]`. Raises ValueError on a line that isn't a recognized `key:
    value` shape or a continuation of one."""
    lines = frontmatter_text.split("\n")
    result = {}
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = _KEY_RE.match(line)
        if not m:
            raise ValueError(f"unrecognized frontmatter line: {line!r}")
        key, rest = m.group(1), m.group(2).strip()
        i += 1
        if rest:
            result[key] = _scalar_or_array(rest)
            continue
        cont = []
        while i < n and lines[i].strip() and lines[i][0] in " \t":
            cont.append(lines[i].strip())
            i += 1
        result[key] = _scalar_or_array(" ".join(cont))
    return result


def _scalar_or_array(rest):
    if rest.startswith("["):
        return _array(rest)
    if rest.startswith('"'):
        return json.loads(rest)
    return rest


def _array(joined):
    return json.loads(_TRAILING_COMMA_RE.sub("]", joined))
