#!/usr/bin/env python3
"""
Tests for scripts/check-marketplace.py.

check-marketplace.py resolves its own ROOT from `__file__`'s location, not
cwd — so these tests build a throwaway marketplace under a tempdir and copy
the real script into it, rather than trying to monkeypatch a module-level
constant. That also means each test runs the checker exactly the way it's
meant to run: as a standalone script sitting in its own repo.

Run: python3 scripts/tests/test_check_marketplace.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKER = os.path.join(HERE, "..", "check-marketplace.py")
GENERATE_SKILL = os.path.join(HERE, "..", "generate-skill.py")
FRONTMATTER = os.path.join(HERE, "..", "frontmatter.py")


def make_marketplace(plugins):
    """A tempdir laid out like this repo: .claude-plugin/marketplace.json,
    plugins/<name>/.claude-plugin/plugin.json, and a copy of the real
    checker under scripts/ (plus generate-skill.py/frontmatter.py, since
    check_skills imports the former via importlib — its filename has a dash
    so it can't be a normal `import`). `plugins` is {name: {"version": ...,
    "hooks": {...} or None, "hook_scripts": {filename: source}, "commands":
    {filename: source}}}."""
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "scripts"))
    os.makedirs(os.path.join(root, ".claude-plugin"))
    shutil.copy(CHECKER, os.path.join(root, "scripts", "check-marketplace.py"))
    shutil.copy(GENERATE_SKILL, os.path.join(root, "scripts", "generate-skill.py"))
    shutil.copy(FRONTMATTER, os.path.join(root, "scripts", "frontmatter.py"))
    entries = []
    for name, spec in plugins.items():
        pdir = os.path.join(root, "plugins", name)
        os.makedirs(os.path.join(pdir, ".claude-plugin"))
        version = spec.get("version", "1.0.0")
        with open(os.path.join(pdir, ".claude-plugin", "plugin.json"), "w") as f:
            json.dump({"name": name, "version": version}, f)
        entries.append(
            {"name": name, "source": f"./plugins/{name}", "version": version}
        )
        for filename, content in (
            ("hooks.json", spec.get("hooks")),
            ("codex-hooks.json", spec.get("codex_hooks")),
        ):
            if content is None:
                continue
            hdir = os.path.join(pdir, "hooks")
            os.makedirs(hdir, exist_ok=True)
            with open(os.path.join(hdir, filename), "w") as f:
                json.dump(content, f)
        for fn, source in spec.get("hook_scripts", {}).items():
            sdir = os.path.join(pdir, "scripts")
            os.makedirs(sdir, exist_ok=True)
            with open(os.path.join(sdir, fn), "w") as f:
                f.write(source)
        for fn, source in spec.get("commands", {}).items():
            cdir = os.path.join(pdir, "commands")
            os.makedirs(cdir, exist_ok=True)
            with open(os.path.join(cdir, fn), "w") as f:
                f.write(source)
    with open(os.path.join(root, ".claude-plugin", "marketplace.json"), "w") as f:
        json.dump({"plugins": entries}, f)
    return root


def run(root):
    return subprocess.run(
        [sys.executable, os.path.join(root, "scripts", "check-marketplace.py")],
        cwd=root,
        capture_output=True,
    )


def run_without_stdlib_module_names(root):
    """Simulate Python <3.10 (no sys.stdlib_module_names) by deleting the
    attribute before the checker's module-level code runs, then executing it
    via runpy so `__file__`/ROOT resolution stays identical to a real run."""
    driver = (
        "import runpy, sys\n"
        "if hasattr(sys, 'stdlib_module_names'):\n"
        "    del sys.stdlib_module_names\n"
        f"runpy.run_path({os.path.join(root, 'scripts', 'check-marketplace.py')!r}, "
        "run_name='__main__')\n"
    )
    return subprocess.run([sys.executable, "-c", driver], cwd=root, capture_output=True)


class CleanMarketplaceTest(unittest.TestCase):
    def test_minimal_plugin_passes(self):
        root = make_marketplace({"foo": {}})
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        r = run(root)
        self.assertEqual(r.returncode, 0, r.stdout.decode())


class HookEventNameTest(unittest.TestCase):
    def test_unknown_event_name_warns_but_does_not_fail(self):
        root = make_marketplace(
            {
                "foo": {
                    "hooks": {
                        "hooks": {"PreToolUser": [{"matcher": "Bash", "hooks": []}]}
                    },
                }
            }
        )
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        r = run(root)
        out = r.stdout.decode()
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("WARN", out)
        self.assertIn("PreToolUser", out)

    def test_known_event_names_are_quiet(self):
        root = make_marketplace(
            {
                "foo": {
                    "hooks": {
                        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}
                    },
                }
            }
        )
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        r = run(root)
        out = r.stdout.decode()
        self.assertEqual(r.returncode, 0, out)
        self.assertNotIn("WARN", out)


class CodexHooksTest(unittest.TestCase):
    """codex-hooks.json (see plugins/sdlc/hooks/codex-hooks.json) resolves
    scripts against ${SDLC_SCRIPTS_ROOT} instead of ${CLAUDE_PLUGIN_ROOT}, and
    that root is the plugin's scripts/ dir directly rather than the plugin
    root — both need their own check, separate from hooks.json's."""

    def _codex_hooks(self, script_ref):
        return {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "^Bash$",
                        "hooks": [
                            {
                                "type": "command",
                                "command": (
                                    'SDLC_HOOK_HOST=codex python3 "${SDLC_SCRIPTS_ROOT}'
                                    f'/{script_ref}"'
                                ),
                            }
                        ],
                    }
                ]
            }
        }

    def test_existing_script_is_fine(self):
        root = make_marketplace(
            {
                "foo": {
                    "codex_hooks": self._codex_hooks("hook.py"),
                    "hook_scripts": {"hook.py": "import json\n"},
                }
            }
        )
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        r = run(root)
        self.assertEqual(r.returncode, 0, r.stdout.decode())

    def test_missing_script_is_an_error(self):
        root = make_marketplace({"foo": {"codex_hooks": self._codex_hooks("nope.py")}})
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        r = run(root)
        out = r.stdout.decode()
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("nope.py", out)
        self.assertIn("codex-hooks.json", out)

    def test_claude_plugin_root_reference_is_not_checked_against_scripts_root(self):
        # A hooks.json-style ${CLAUDE_PLUGIN_ROOT} reference inside
        # codex-hooks.json (e.g. accidentally copy-pasted) must not be
        # silently checked against the wrong root and pass by coincidence —
        # it's simply not a pattern this file's checker looks for at all.
        codex_hooks = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "^Bash$",
                        "hooks": [
                            {
                                "type": "command",
                                "command": (
                                    'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hook.py"'
                                ),
                            }
                        ],
                    }
                ]
            }
        }
        root = make_marketplace({"foo": {"codex_hooks": codex_hooks}})
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        r = run(root)
        self.assertEqual(r.returncode, 0, r.stdout.decode())


class SkillsTest(unittest.TestCase):
    """check_skills calls generate_skill.check_one() in-process (see
    check-marketplace.py) — a commands/*.md file with no matching, up-to-date
    skills/<name>/SKILL.md must fail the overall check."""

    COMMAND_SOURCE = '---\ndescription: "Does foo."\n---\n\nBody.\n'

    def test_plugin_with_no_commands_dir_is_unaffected(self):
        root = make_marketplace({"foo": {}})
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        r = run(root)
        self.assertEqual(r.returncode, 0, r.stdout.decode())

    def test_missing_skill_md_is_an_error(self):
        root = make_marketplace({"foo": {"commands": {"bar.md": self.COMMAND_SOURCE}}})
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        r = run(root)
        out = r.stdout.decode()
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("missing", out)
        self.assertIn("bar", out)

    def test_up_to_date_skill_md_passes(self):
        root = make_marketplace({"foo": {"commands": {"bar.md": self.COMMAND_SOURCE}}})
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        subprocess.run(
            [
                sys.executable,
                os.path.join(root, "scripts", "generate-skill.py"),
                "--write-all-in",
                os.path.join(root, "plugins", "foo"),
            ],
            check=True,
            capture_output=True,
        )
        r = run(root)
        self.assertEqual(r.returncode, 0, r.stdout.decode())

    def test_stale_skill_md_is_an_error(self):
        root = make_marketplace({"foo": {"commands": {"bar.md": self.COMMAND_SOURCE}}})
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        skill_dir = os.path.join(root, "plugins", "foo", "skills", "bar")
        os.makedirs(skill_dir)
        with open(os.path.join(skill_dir, "SKILL.md"), "w") as f:
            f.write('---\nname: bar\ndescription: "stale"\n---\n\nOld body.\n')
        r = run(root)
        out = r.stdout.decode()
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("stale", out)

    def test_malformed_command_frontmatter_is_an_error_not_a_crash(self):
        # check_skills's `except (OSError, ValueError)` branch — a command
        # file generate_skill.check_one() can't even parse must surface as
        # a normal collected error, not an uncaught traceback that aborts
        # the whole marketplace check.
        root = make_marketplace(
            {
                "foo": {
                    "commands": {
                        "broken.md": '---\nargument-hint: "[--x]"\n---\n\nBody.\n'
                    }
                }
            }
        )
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        r = run(root)
        out = r.stdout.decode()
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("no `description`", out)


class HookScriptImportTest(unittest.TestCase):
    def _with_hook_script(self, source):
        return make_marketplace(
            {
                "foo": {
                    "hooks": {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "matcher": "Bash",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": (
                                                'python3 "${CLAUDE_PLUGIN_ROOT}'
                                                '/scripts/hook.py"'
                                            ),
                                        }
                                    ],
                                }
                            ]
                        }
                    },
                    "hook_scripts": {"hook.py": source},
                }
            }
        )

    def test_stdlib_import_is_fine(self):
        root = self._with_hook_script("import json\nimport os\n")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        r = run(root)
        self.assertEqual(r.returncode, 0, r.stdout.decode())

    def test_existing_sibling_module_is_fine(self):
        root = self._with_hook_script("import helper\n")
        with open(
            os.path.join(root, "plugins", "foo", "scripts", "helper.py"), "w"
        ) as f:
            f.write("X = 1\n")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        r = run(root)
        self.assertEqual(r.returncode, 0, r.stdout.decode())

    def test_missing_sibling_module_is_an_error(self):
        root = self._with_hook_script("import gate_store as gs\n")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        r = run(root)
        out = r.stdout.decode()
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("gate_store", out)

    def test_third_party_import_is_an_error(self):
        # Not just "missing sibling" — an unambiguous third-party package
        # name (never plausibly a same-directory sibling) must still be
        # flagged, matching this repo's pure-stdlib-hooks convention.
        root = self._with_hook_script("import yaml\n")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        r = run(root)
        out = r.stdout.decode()
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("yaml", out)

    def test_missing_stdlib_module_names_degrades_to_a_warning(self):
        # Python <3.10 has no sys.stdlib_module_names. This must not crash
        # the whole checker run over one attribute — CLAUDE.md promises
        # this tooling works on any box with python3.
        root = self._with_hook_script("import gate_store as gs\n")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        r = run_without_stdlib_module_names(root)
        out = r.stdout.decode()
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("WARN", out)
        self.assertIn("3.10", out)

    def test_from_import_of_missing_sibling_is_an_error(self):
        root = self._with_hook_script("from helper import thing\n")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        r = run(root)
        self.assertEqual(r.returncode, 1, r.stdout.decode())

    def test_relative_import_is_not_flagged(self):
        # level > 0 ("from . import x") isn't a same-directory sibling
        # lookup by our convention — not this checker's concern.
        root = self._with_hook_script("from . import helper\n")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        r = run(root)
        self.assertEqual(r.returncode, 0, r.stdout.decode())

    def test_docstring_prose_is_not_mistaken_for_an_import(self):
        # Regression: a line-anchored regex over raw text previously matched
        # "from committing to main by habit" inside a real docstring.
        source = (
            '"""\n'
            "This hook stops an agent from committing to main by habit.\n"
            '"""\n'
            "import json\n"
        )
        root = self._with_hook_script(source)
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        r = run(root)
        self.assertEqual(r.returncode, 0, r.stdout.decode())

    def test_syntax_error_is_not_this_checkers_job(self):
        # A real syntax error is py_compile's/the test suite's problem, not
        # something this checker should crash or falsely report on.
        root = self._with_hook_script("def broken(:\n")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        r = run(root)
        self.assertEqual(r.returncode, 0, r.stdout.decode())


if __name__ == "__main__":
    unittest.main()
