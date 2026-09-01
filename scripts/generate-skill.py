#!/usr/bin/env python3
"""
Generate a cross-host SKILL.md from a Claude Code plugin command file.

Claude Code plugin commands live at `commands/<name>.md` with Claude-specific
frontmatter (`allowed-tools`, `argument-hint`) that other coding-agent hosts
(Codex CLI, Gemini CLI, Cursor, Copilot) don't read. Those hosts share a
`SKILL.md` format instead — `name` + `description` frontmatter, then
markdown instructions. Rather than hand-duplicate every command into a
second file (drift risk — see plugins/sdlc/README.md, "Codex CLI support"),
this derives `skills/<name>/SKILL.md` mechanically from `commands/<name>.md`:
parse the existing frontmatter with frontmatter.py, re-emit it in SKILL.md's
shape, and keep the body verbatim (already agent-neutral as of #24/#25).

Known limitation, not fixed here: the body still references `$ARGUMENTS`
Claude-command-style — no cross-host argument-passing convention exists yet,
so SKILL.md consumers get the same body text a Claude Code user would.

Usage:
  generate-skill.py <commands/name.md>              write skills/name/SKILL.md
  generate-skill.py <commands/name.md> --check       exit nonzero if stale/missing
  generate-skill.py --write-all-in <plugin-dir>      regenerate every command
  generate-skill.py --write-all-in <plugin-dir> --check   check every command

Pure stdlib.
"""

import argparse
import glob
import json
import os
import sys

import frontmatter as fm


def render(source_text, name_fallback):
    """Render a commands/<name>.md file's text into a SKILL.md file's text.
    `name_fallback` is used when the source has no explicit `name:` field —
    normally the command's filename stem. Raises ValueError if the source
    has no `description` (SKILL.md requires one) or if `description` isn't a
    plain string — every real command file's description is either a quoted
    single-line scalar or a folded multi-line one, both of which
    frontmatter.parse() already collapses to a string; any other shape means
    a command file frontmatter.py wasn't built to handle, and guessing at it
    here would silently produce a wrong SKILL.md rather than failing loudly."""
    front_text, body = fm.split(source_text)
    data = fm.parse(front_text)
    description = data.get("description")
    if not description:
        raise ValueError("source frontmatter has no `description`")
    if not isinstance(description, str):
        raise ValueError(f"`description` is not a plain string: {description!r}")
    name = data.get("name") or name_fallback
    if not isinstance(name, str):
        raise ValueError(f"`name` is not a plain string: {name!r}")
    # ensure_ascii=False: several real descriptions use em dashes — the
    # default would escape them to —, degrading readability for the
    # exact non-Claude hosts (Cursor, Copilot, etc.) this file is for.
    out_front = "name: {}\ndescription: {}\n".format(
        name, json.dumps(description, ensure_ascii=False)
    )
    return "---\n" + out_front + "---\n" + body


def skill_path(command_path):
    """commands/<name>.md -> skills/<name>/SKILL.md, siblings of `commands/`."""
    commands_dir = os.path.dirname(command_path)
    plugin_dir = os.path.dirname(commands_dir)
    name = os.path.splitext(os.path.basename(command_path))[0]
    return os.path.join(plugin_dir, "skills", name, "SKILL.md")


def check_one(command_path):
    """Returns (ok, message). `message` is None when `ok` is True. Raises
    ValueError on a source error (bad frontmatter, no/malformed
    description) — that's a problem with the command file itself, not a
    pass/fail the caller should silently fold into "stale"."""
    name = os.path.splitext(os.path.basename(command_path))[0]
    with open(command_path, encoding="utf-8") as f:
        rendered = render(f.read(), name)
    target = skill_path(command_path)
    if not os.path.isfile(target):
        return False, f"missing: {target} (run generate-skill.py {command_path})"
    with open(target, encoding="utf-8") as f:
        current = f.read()
    if current != rendered:
        return False, (
            f"stale: {target} does not match {command_path} "
            f"(run generate-skill.py {command_path} to regenerate)"
        )
    return True, None


def generate_one(command_path, check=False):
    """Returns True if up to date / successfully written, False if `check`
    found staleness or the target was missing (message printed to stdout in
    that case). Raises on a source error (bad frontmatter, no description)
    regardless of `check`."""
    if check:
        ok, message = check_one(command_path)
        if not ok:
            print(message)
        return ok
    name = os.path.splitext(os.path.basename(command_path))[0]
    with open(command_path, encoding="utf-8") as f:
        rendered = render(f.read(), name)
    target = skill_path(command_path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(rendered)
    return True


def commands_in(plugin_dir):
    return sorted(glob.glob(os.path.join(plugin_dir, "commands", "*.md")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command_md", nargs="?", help="path to a commands/<name>.md file")
    parser.add_argument(
        "--write-all-in", metavar="PLUGIN_DIR", help="regenerate every command in a plugin dir"
    )
    parser.add_argument(
        "--check", action="store_true", help="exit nonzero if generated output is stale/missing"
    )
    args = parser.parse_args()

    if args.write_all_in:
        paths = commands_in(args.write_all_in)
    elif args.command_md:
        paths = [args.command_md]
    else:
        parser.error("pass a commands/<name>.md path or --write-all-in <plugin-dir>")

    ok = True
    for path in paths:
        try:
            if not generate_one(path, check=args.check):
                ok = False
        except ValueError as e:
            print(f"error: {path}: {e}", file=sys.stderr)
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
