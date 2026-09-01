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
  - any hooks/hooks.json parses, referenced hook scripts exist, their event
    names are recognized (warn only — this checker's allowlist may lag a new
    event), and every referenced .py script's imports resolve (stdlib or a
    same-directory sibling — this repo's hook scripts are pure stdlib by
    convention, so anything else means a script survived a sibling's deletion)

Also flags plugin directories present on disk but absent from the
marketplace listing (easy to forget when adding a plugin).

Exit 0 if clean, 1 if any error. Warnings alone do not fail the run.
"""

import ast
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKETPLACE = os.path.join(ROOT, ".claude-plugin", "marketplace.json")

# sys.stdlib_module_names needs Python 3.10+. This repo's own CLAUDE.md
# promises hook enforcement "works on any box with python3" — None here
# means an older interpreter, and check_local_imports degrades to a warning
# instead of crashing the whole run over one attribute.
STDLIB_MODULE_NAMES = getattr(sys, "stdlib_module_names", None)

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


# Hook events Claude Code actually dispatches. A typo'd event name (e.g.
# "PreToolUser") parses as valid JSON and just never fires — nothing else in
# this repo would catch that, since it only fails at hook-runtime, silently.
KNOWN_HOOK_EVENTS = {
    "PreToolUse",
    "PostToolUse",
    "Notification",
    "UserPromptSubmit",
    "Stop",
    "SubagentStop",
    "PreCompact",
    "SessionStart",
    "SessionEnd",
}


def _check_hooks_file(hooks_json, plugin_dir, name, script_root_var, script_root_dir):
    if not os.path.isfile(hooks_json):
        return
    data = load_json(hooks_json)
    if not data:
        return
    label = "{}/{}".format(name, os.path.basename(hooks_json))
    # A warning, not an error: KNOWN_HOOK_EVENTS is this checker's best
    # knowledge, not Claude Code's own source of truth, and a false error
    # here would block a legitimate new event this list hasn't caught up to.
    for event in data.get("hooks", {}):
        if event not in KNOWN_HOOK_EVENTS:
            warn(
                '{}: hooks.json has unrecognized event "{}" — typo, or a '
                "new event this checker's allowlist needs updating for?".format(
                    label, event
                )
            )
    # Collect referenced scripts and confirm they exist on disk. The var
    # reference may carry bash's `:?message` unset-error syntax (see
    # codex-hooks.json), not just a bare `${VAR}` — `[^}]*` skips over
    # that optional suffix without swallowing the following `/script.py`.
    blob = json.dumps(data)
    pattern = r"\$\{" + re.escape(script_root_var) + r"[^}]*\}/(\S+?\.(?:sh|py))"
    scripts = set(re.findall(pattern, blob))
    for ref in scripts:
        script = os.path.join(script_root_dir, ref)
        if not os.path.isfile(script):
            err("{}: references missing script {}".format(label, ref))
        elif ref.endswith(".py"):
            check_local_imports(script, ref, label)


# One row per host-flavored hooks manifest this checker knows about. Each
# resolves its own scripts against a different env var and root — Codex has
# no plugin-root concept, so codex-hooks.json (see
# plugins/sdlc/hooks/codex-hooks.json) points SDLC_SCRIPTS_ROOT at scripts/
# directly rather than the plugin root. A future host adds a row here, not
# another hand-copied call.
HOOK_MANIFESTS = (
    ("hooks.json", "CLAUDE_PLUGIN_ROOT", lambda plugin_dir: plugin_dir),
    (
        "codex-hooks.json",
        "SDLC_SCRIPTS_ROOT",
        lambda plugin_dir: os.path.join(plugin_dir, "scripts"),
    ),
)


def check_hooks(plugin_dir, name):
    for filename, script_root_var, script_root_dir in HOOK_MANIFESTS:
        _check_hooks_file(
            os.path.join(plugin_dir, "hooks", filename),
            plugin_dir,
            name,
            script_root_var,
            script_root_dir(plugin_dir),
        )


def check_local_imports(script, ref, name):
    """A hook script that survives a sibling module's deletion only fails at
    hook-runtime, silently (each hook's own except-and-report pattern can't
    catch an ImportError raised before its own code runs). Flag any
    `import X`/`from X import ...` whose X is neither stdlib nor an existing
    same-directory file — this repo's hook scripts are pure stdlib plus
    same-plugin siblings by convention (see root CLAUDE.md), so anything
    else is exactly the drift this check exists to catch.

    Parsed with `ast`, not a regex over the raw text: a regex anchored on
    line-start also matches "from committing to main by habit" inside a
    docstring — this doesn't, since only real Import/ImportFrom nodes count."""
    try:
        with open(script) as f:
            source = f.read()
    except OSError as e:
        # check_hooks already confirmed this file exists, so a read failure
        # past that point is unexpected, not routine — say so rather than
        # skipping quietly.
        warn("{}: could not read {} to check its imports ({})".format(name, ref, e))
        return
    try:
        tree = ast.parse(source, filename=script)
    except SyntaxError:
        # Not this checker's job — CI's lint job (`ruff check`) already has
        # to parse every file, so a real syntax error surfaces there. Not a
        # substitute for that check, just not a second one.
        return
    if STDLIB_MODULE_NAMES is None:
        warn(
            "{}: skipping import check for {} (needs Python 3.10+ for "
            "sys.stdlib_module_names)".format(name, ref)
        )
        return
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])  # level > 0 is a relative import
    script_dir = os.path.dirname(script)
    for mod in sorted(mods):
        if mod == "__future__" or mod in STDLIB_MODULE_NAMES:
            continue
        if not os.path.isfile(os.path.join(script_dir, mod + ".py")):
            err(
                '{}: {} imports "{}", which is neither stdlib nor a sibling '
                "module in the same directory".format(name, ref, mod)
            )


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
