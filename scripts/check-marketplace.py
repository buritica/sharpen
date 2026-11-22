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

  - every plugin path in commands/*.md and skills/*/SKILL.md is spelled
    `${<PLUGIN>_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}` with the plugin's own
    prefix: never a bare or unbraced `$CLAUDE_PLUGIN_ROOT` (empty in Claude
    Code's agent shell) and never another plugin's variable (see
    docs/hosts.md, "Plugin roots in skill text")
  - docs/hosts.md's support matrix matches plugins/sdlc/scripts/hosts.py

Also flags plugin directories present on disk but absent from the
marketplace listing (easy to forget when adding a plugin).

Exit 0 if clean, 1 if any error. Warnings alone do not fail the run.
"""

import ast
import importlib.util
import json
import os
import re
import sys
import sysconfig

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKETPLACE = os.path.join(ROOT, ".claude-plugin", "marketplace.json")

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import frontmatter  # noqa: E402 — needs sys.path set first (see above)

# generate-skill.py's filename has a dash, so it can't be `import`ed
# normally — load it the same way plugins/sdlc/tests/test_gate.py already
# loads dash-named scripts like record-gate.py, rather than shelling out to
# a subprocess and re-parsing its printed output as an API contract.
_generate_skill_spec = importlib.util.spec_from_file_location(
    "generate_skill", os.path.join(ROOT, "scripts", "generate-skill.py")
)
generate_skill = importlib.util.module_from_spec(_generate_skill_spec)
_generate_skill_spec.loader.exec_module(generate_skill)

# sys.stdlib_module_names needs Python 3.10+. The hooks promise to work on
# "any box with python3", and macOS's system python3 is 3.9, so older
# interpreters fall back to _is_stdlib_by_location() instead of skipping
# the check.
STDLIB_MODULE_NAMES = getattr(sys, "stdlib_module_names", None)
_SYS_PATHS = sysconfig.get_paths()
_STDLIB_DIRS = tuple(
    {os.path.realpath(_SYS_PATHS[k]) for k in ("stdlib", "platstdlib")}
)


def _is_stdlib_by_location(mod):
    """Pre-3.10 fallback: a top-level module is stdlib when it is built in,
    frozen, or resolves to a file under the interpreter's stdlib directory
    (and not its site-packages). find_spec on a top-level name locates the
    module without importing it."""
    if mod in sys.builtin_module_names:
        return True
    try:
        spec = importlib.util.find_spec(mod)
    except (ImportError, ValueError):
        return False
    if spec is None:
        return False
    if spec.origin in ("built-in", "frozen"):
        return True
    locations = [spec.origin] if spec.origin else []
    locations += list(spec.submodule_search_locations or [])
    for loc in locations:
        real = os.path.realpath(loc)
        if {"site-packages", "dist-packages"} & set(real.split(os.sep)):
            continue
        if any(real.startswith(d + os.sep) for d in _STDLIB_DIRS):
            return True
    return False


def _is_stdlib(mod):
    if STDLIB_MODULE_NAMES is not None:
        return mod in STDLIB_MODULE_NAMES
    return _is_stdlib_by_location(mod)


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
    """Return the description from a command's frontmatter, or None. Uses
    frontmatter.py (see generate-skill.py) rather than its own regex, so a
    folded multi-line description (most grumpy commands) or a quoted one
    (most sdlc commands) are both read the same way generate-skill.py reads
    them — a separate regex here previously only grabbed the first line of
    a folded description, which could silently diverge from what actually
    gets generated."""
    try:
        with open(md_path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    try:
        front_text, _ = frontmatter.split(text)
        description = frontmatter.parse(front_text).get("description")
    except ValueError:
        return None
    return description or None


def check_commands(plugin_dir, name):
    cmd_dir = os.path.join(plugin_dir, "commands")
    if not os.path.isdir(cmd_dir):
        return
    for fn in sorted(os.listdir(cmd_dir)):
        if not fn.endswith(".md"):
            continue
        rel = os.path.join(name, "commands", fn)
        if frontmatter_description(os.path.join(cmd_dir, fn)) is None:
            # "Missing" covers both a genuinely absent `description:` field
            # and frontmatter that failed to parse at all — check_skills
            # (below) re-parses the same file and reports the precise parse
            # error separately when that's the actual cause.
            err("{}: command has no usable frontmatter description".format(rel))


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


def check_hooks(plugin_dir, name):
    hooks_json = os.path.join(plugin_dir, "hooks", "hooks.json")
    if not os.path.isfile(hooks_json):
        return
    data = load_json(hooks_json)
    if not data:
        return
    # A warning, not an error: KNOWN_HOOK_EVENTS is this checker's best
    # knowledge, not Claude Code's own source of truth, and a false error
    # here would block a legitimate new event this list hasn't caught up to.
    for event in data.get("hooks", {}):
        if event not in KNOWN_HOOK_EVENTS:
            warn(
                '{}: hooks.json has unrecognized event "{}" — typo, or a '
                "new event this checker's allowlist needs updating for?".format(
                    name, event
                )
            )
    # Collect referenced scripts and confirm they exist on disk.
    blob = json.dumps(data)
    scripts = set(re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/(\S+?\.(?:sh|py))", blob))
    for ref in scripts:
        script = os.path.join(plugin_dir, ref)
        if not os.path.isfile(script):
            err("{}: hooks.json references missing script {}".format(name, ref))
        elif ref.endswith(".py"):
            check_local_imports(script, ref, name)


def check_skills(plugin_dir, name):
    """Every commands/*.md file's derived skills/<name>/SKILL.md (see
    generate-skill.py) must be up to date. A plugin with no commands/ dir
    has nothing to check. Catches OSError alongside ValueError (a file
    vanishing or becoming unreadable between the glob and the open, e.g. a
    permissions change or a symlink race) for the same reason every other
    check_* in this file does: one bad file should collect an error, not
    crash the whole marketplace check with a raw traceback."""
    if not os.path.isdir(os.path.join(plugin_dir, "commands")):
        return
    for command_path in generate_skill.commands_in(plugin_dir):
        try:
            ok, message = generate_skill.check_one(command_path)
        except (OSError, ValueError) as e:
            err("{}: {}: {}".format(name, command_path, e))
            continue
        if not ok:
            err("{}: {}".format(name, message))


# Each plugin's own root variable, exported by its pi extension. A plugin not
# listed here gets its name upper-cased with dashes as underscores.
PLUGIN_ROOT_PREFIX = {
    "sdlc": "SDLC",
    "grumpy": "GRUMPY",
    "sdlc-guardrails": "GUARDRAILS",
}
SHARED_ROOT_REF = re.compile(r"\$\{?CLAUDE_PLUGIN_ROOT\b\}?")
# Underscores and digits allowed: a plugin named `foo-bar` has the prefix
# `FOO_BAR`, which `[A-Z]+` would never capture.
OWN_ROOT_REF = re.compile(r"\$\{?([A-Z][A-Z0-9_]*)_PLUGIN_ROOT\b")


def _root_prefix(name):
    return PLUGIN_ROOT_PREFIX.get(name, name.upper().replace("-", "_"))


def plugin_root_problems(text, name):
    """(line, message) for every plugin path in `text` not spelled with
    `name`'s own variable first. Prose naming the variable without a `$`
    (`CLAUDE_PLUGIN_ROOT`) is fine; only expansions are checked."""
    prefix = _root_prefix(name)
    want = "${%s_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}" % prefix
    lead = "${%s_PLUGIN_ROOT:-" % prefix
    problems = []
    for m in SHARED_ROOT_REF.finditer(text):
        start = m.start() - len(lead)
        if start < 0 or text[start : start + len(want)] != want:
            line = text.count("\n", 0, m.start()) + 1
            problems.append((line, "plugin path must be spelled " + want))
    for m in OWN_ROOT_REF.finditer(text):
        if m.group(1) == prefix:
            # The own variable alone (`$SDLC_PLUGIN_ROOT/...`, `${SDLC_PLUGIN_ROOT}`)
            # is empty on Claude Code and Codex: only the full fallback form works.
            if not text.startswith(want, m.start()):
                line = text.count("\n", 0, m.start()) + 1
                problems.append((line, "plugin path must be spelled " + want))
        elif m.group(1) != "CLAUDE":
            line = text.count("\n", 0, m.start()) + 1
            problems.append(
                (
                    line,
                    "uses another plugin's root variable ${}_PLUGIN_ROOT".format(
                        m.group(1)
                    ),
                )
            )
    # A malformed fallback trips both loops on the same spot; report it once.
    return sorted(set(problems))


def check_plugin_roots(plugin_dir, name):
    paths = []
    for sub_dir, pattern in (("commands", None), ("skills", "SKILL.md")):
        base = os.path.join(plugin_dir, sub_dir)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, files in os.walk(base):
            for fn in sorted(files):
                if (pattern and fn == pattern) or (not pattern and fn.endswith(".md")):
                    paths.append(os.path.join(dirpath, fn))
    for path in sorted(paths):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except (OSError, ValueError) as e:
            err("{}: {}: {}".format(name, path, e))
            continue
        for line, message in plugin_root_problems(text, name):
            err(
                "{}: {}:{}: {}".format(name, os.path.relpath(path, ROOT), line, message)
            )


def check_hosts_matrix():
    """docs/hosts.md's generated table must match hosts.HOSTS."""
    hosts_py = os.path.join(ROOT, "plugins", "sdlc", "scripts", "hosts.py")
    doc = os.path.join(ROOT, "docs", "hosts.md")
    if not os.path.isfile(hosts_py):
        return
    spec = importlib.util.spec_from_file_location("sharpen_hosts", hosts_py)
    hosts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hosts)
    try:
        with open(doc, encoding="utf-8") as f:
            text = f.read()
        fresh = hosts.splice_matrix(text)
    except (OSError, ValueError) as e:
        err("docs/hosts.md: {}".format(e))
        return
    if fresh != text:
        err(
            "docs/hosts.md: support matrix is stale; run "
            "python3 plugins/sdlc/scripts/hosts.py --write docs/hosts.md"
        )


# Extensions shell out to hook scripts by bare filename (runHook("name.py")
# / runGuard("name.py") / join(HOOKS_DIR, "name.py")). Match only `.py`
# filenames that appear as quoted string literals — actual call sites — and
# resolve them against both the `scripts/` and `hooks/` dirs below. A bare
# `.py` in prose or a backtick-wrapped comment ref is not a hook script the
# extension runs, so it must not trip the existence check.
PI_SCRIPT_REF = re.compile(r"['\"]([\w-]+\.py)['\"]")


def _glob_has_skill_dir(path):
    """True if `path` (a pi.skills entry, glob chars already handled by the
    caller) is a directory containing at least one SKILL.md under it, or a
    file ending in SKILL.md. pi loads skills by discovering SKILL.md; a
    manifest entry that points at a dir with none would silently load
    nothing, so the checker fails on it."""
    if os.path.isfile(path):
        return os.path.basename(path) == "SKILL.md"
    if not os.path.isdir(path):
        return False
    return any(
        fn == "SKILL.md" for dirpath, _dirs, files in os.walk(path) for fn in files
    )


def check_pi_manifests(plugin_dir, name, version):
    """pi support (docs/pi.md) lives in each plugin's package.json under a
    `pi` key, plus a top-level bundle package.json that is pi's analog of the
    marketplace listing. Claude users get hooks via hooks.json; pi users get
    them via a pi extension that shells out to the SAME pure-stdlib scripts,
    so the pi contract must not be allowed to rot silently:
      - every plugin has a package.json with a `pi` key
      - its `pi` version matches the plugin's own version (the pi analog of
        the two-place bump — now three)
      - every pi.skills entry resolves to a dir/file that pi will actually
        load as a skill, and every pi.extensions entry resolves to a file
      - every extension's referenced hook script (scripts/*.py for sdlc,
        hooks/_block-main-commits.py for guardrails) exists
    Errors are hard failures, matching the Claude-side checks above."""
    pj_path = os.path.join(plugin_dir, "package.json")
    pj = load_json(pj_path)
    if not pj:
        return
    if "pi" not in pj:
        err(
            "{}: package.json has no `pi` manifest (pi install won't work)".format(name)
        )
        return
    if pj.get("version") != version:
        err(
            "{}: pi package.json version drift (plugin.json='{}', "
            "package.json='{}')".format(name, version, pj.get("version"))
        )
    if "pi-package" not in pj.get("keywords", []):
        warn("{}: pi package.json is missing the 'pi-package' keyword".format(name))

    pi = pj["pi"]
    for entry in pi.get("skills", []):
        if entry.startswith("!"):
            continue  # exclusion — nothing to load, nothing to check
        target = os.path.normpath(os.path.join(plugin_dir, entry.lstrip("./")))
        if any(c in entry for c in "*?["):
            # A glob whose directory prefix must at least exist.
            prefix = os.path.dirname(target)
            if not os.path.isdir(prefix):
                err(
                    "{}: pi.skills entry '{}' glob prefix missing: {}".format(
                        name, entry, target
                    )
                )
            continue
        if not _glob_has_skill_dir(target):
            err(
                "{}: pi.skills entry '{}' resolves to {} with no SKILL.md".format(
                    name, entry, os.path.relpath(target, ROOT)
                )
            )

    for entry in pi.get("extensions", []):
        target = os.path.normpath(os.path.join(plugin_dir, entry.lstrip("./")))
        if not os.path.isfile(target):
            err(
                "{}: pi.extensions entry '{}' is not a file: {}".format(
                    name, entry, target
                )
            )
            continue
        # Every hook script the extension shells out to must still exist.
        try:
            src = open(target, encoding="utf-8").read()
        except OSError as e:
            warn("{}: could not read pi extension {} ({})".format(name, entry, e))
            continue
        for ref in PI_SCRIPT_REF.findall(src):
            if any(
                os.path.isfile(os.path.join(plugin_dir, sub, ref))
                for sub in ("scripts", "hooks")
            ):
                continue
            err(
                "{}: pi extension {} references missing hook script {}".format(
                    name, entry, ref
                )
            )


def check_top_level_pi_bundle():
    """The top-level package.json is pi's analog of marketplace.json: one
    `pi install` pulls in all three plugins. Its skills/extensions entries
    must resolve to real SKILL.md dirs / files (minus `!` exclusions), and
    its version must track the marketplace's own listing."""
    pkg = load_json(os.path.join(ROOT, "package.json"))
    if not pkg or "pi" not in pkg:
        err("top-level package.json has no `pi` manifest")
        return
    for entry in pkg["pi"].get("skills", []):
        if entry.startswith("!"):
            continue
        target = os.path.normpath(os.path.join(ROOT, entry.lstrip("./")))
        if not _glob_has_skill_dir(target):
            err(
                "top-level pi.skills entry '{}' resolves to {} with no SKILL.md".format(
                    entry, os.path.relpath(target, ROOT)
                )
            )
    for entry in pkg["pi"].get("extensions", []):
        target = os.path.normpath(os.path.join(ROOT, entry.lstrip("./")))
        if not os.path.isfile(target):
            err(
                "top-level pi.extensions entry '{}' is not a file: {}".format(
                    entry, target
                )
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
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])  # level > 0 is a relative import
    script_dir = os.path.dirname(script)
    for mod in sorted(mods):
        if mod == "__future__" or _is_stdlib(mod):
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
        check_skills(plugin_dir, name)
        check_plugin_roots(plugin_dir, name)
        check_pi_manifests(plugin_dir, name, pv)

    check_top_level_pi_bundle()
    check_hosts_matrix()

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
