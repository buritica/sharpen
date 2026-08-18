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


def make_marketplace(plugins):
    """A tempdir laid out like this repo: .claude-plugin/marketplace.json,
    plugins/<name>/.claude-plugin/plugin.json, and a copy of the real
    checker under scripts/. `plugins` is {name: {"version": ..., "hooks":
    {...} or None, "hook_scripts": {filename: source}}}."""
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "scripts"))
    os.makedirs(os.path.join(root, ".claude-plugin"))
    shutil.copy(CHECKER, os.path.join(root, "scripts", "check-marketplace.py"))
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
        hooks = spec.get("hooks")
        if hooks is not None:
            hdir = os.path.join(pdir, "hooks")
            os.makedirs(hdir)
            with open(os.path.join(hdir, "hooks.json"), "w") as f:
                json.dump(hooks, f)
        for fn, source in spec.get("hook_scripts", {}).items():
            sdir = os.path.join(pdir, "scripts")
            os.makedirs(sdir, exist_ok=True)
            with open(os.path.join(sdir, fn), "w") as f:
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
