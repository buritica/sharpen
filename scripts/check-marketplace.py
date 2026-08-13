#!/usr/bin/env python3
"""
Validate the sharpen marketplace for internal consistency.

Pure stdlib. Run from anywhere:
    python3 scripts/check-marketplace.py

Checks, per plugin listed in .claude-plugin/marketplace.json:
  - the plugin source directory exists
  - it has .claude-plugin/plugin.json and the JSON parses
  - the version in marketplace.json matches the plugin's own plugin.json
    (this is the drift that has to be bumped by hand in two places)
  - the name matches the directory's plugin.json name
  - every commands/*.md file has YAML-ish frontmatter with a description
  - any hooks/hooks.json parses and referenced hook scripts exist

Also flags plugin directories present on disk but absent from the
marketplace listing (easy to forget when adding a plugin).

Exit 0 if clean, 1 if any error. Warnings alone do not fail the run.
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKETPLACE = os.path.join(ROOT, ".claude-plugin", "marketplace.json")

errors = []
warnings = []


def err(msg):
    errors.append(msg)


def warn(msg):
    warnings.append(msg)


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        err("missing file: {}".format(os.path.relpath(path, ROOT)))
    except ValueError as e:
        err("invalid JSON in {}: {}".format(os.path.relpath(path, ROOT), e))
    return None


def frontmatter_description(md_path):
    """Return the description from a command's frontmatter, or None."""
    try:
        with open(md_path) as f:
            text = f.read()
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    block = text[3:end]
    m = re.search(r"^\s*description\s*:\s*(.+\S)", block, re.MULTILINE)
    return m.group(1).strip() if m else None


def check_commands(plugin_dir, name):
    cmd_dir = os.path.join(plugin_dir, "commands")
    if not os.path.isdir(cmd_dir):
        return
    for fn in sorted(os.listdir(cmd_dir)):
        if not fn.endswith(".md"):
            continue
        rel = os.path.join(name, "commands", fn)
        if frontmatter_description(os.path.join(cmd_dir, fn)) is None:
            err("{}: command missing frontmatter description".format(rel))


def check_hooks(plugin_dir, name):
    hooks_json = os.path.join(plugin_dir, "hooks", "hooks.json")
    if not os.path.isfile(hooks_json):
        return
    data = load_json(hooks_json)
    if not data:
        return
    # Collect referenced scripts and confirm they exist on disk.
    blob = json.dumps(data)
    for ref in re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/(\S+?\.(?:sh|py))", blob):
        script = os.path.join(plugin_dir, ref)
        if not os.path.isfile(script):
            err("{}: hooks.json references missing script {}".format(name, ref))


def main():
    market = load_json(MARKETPLACE)
    if not market:
        return _finish()

    listed = {}
    for entry in market.get("plugins", []):
        name = entry.get("name", "<unnamed>")
        listed[name] = entry
        source = entry.get("source", "")
        plugin_dir = os.path.normpath(os.path.join(ROOT, source))

        if not os.path.isdir(plugin_dir):
            err("{}: source dir does not exist: {}".format(name, source))
            continue

        pj = load_json(os.path.join(plugin_dir, ".claude-plugin", "plugin.json"))
        if not pj:
            continue

        if pj.get("name") != name:
            err(
                "{}: name mismatch (marketplace='{}', plugin.json='{}')".format(
                    name, name, pj.get("name")
                )
            )

        mv, pv = entry.get("version"), pj.get("version")
        if mv != pv:
            err(
                "{}: version drift (marketplace='{}', plugin.json='{}')".format(
                    name, mv, pv
                )
            )

        check_commands(plugin_dir, name)
        check_hooks(plugin_dir, name)

    # Plugin dirs on disk but not in the marketplace listing.
    plugins_root = os.path.join(ROOT, "plugins")
    if os.path.isdir(plugins_root):
        for d in sorted(os.listdir(plugins_root)):
            pj_path = os.path.join(plugins_root, d, ".claude-plugin", "plugin.json")
            if os.path.isfile(pj_path):
                pj = load_json(pj_path)
                if pj and pj.get("name") not in listed:
                    warn(
                        "plugins/{} exists on disk but is not listed in "
                        "marketplace.json".format(d)
                    )

    return _finish()


def _finish():
    for w in warnings:
        print("WARN: {}".format(w))
    for e in errors:
        print("ERROR: {}".format(e))
    if errors:
        print("\nmarketplace check FAILED ({} error(s))".format(len(errors)))
        return 1
    print("marketplace check OK ({} warning(s))".format(len(warnings)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
