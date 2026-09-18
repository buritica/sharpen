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
        # Every plugin is also a pi package now; the checker validates the
        # `pi` manifest, so a fixture needs a valid (if empty) one by default.
        # `spec["pi"]` lets a test add skills/extensions entries that must
        # resolve to real files/dirs on disk.
        pi_manifest = {
            "name": name,
            "version": version,
            "keywords": ["pi-package"],
            # `spec["pi"]` replaces the whole nested manifest (skills +
            # extensions), so a test can supply entries that must resolve.
            "pi": spec.get("pi", {"skills": [], "extensions": []}),
        }
        with open(os.path.join(pdir, "package.json"), "w") as f:
            json.dump(pi_manifest, f)
        entries.append(
            {"name": name, "source": f"./plugins/{name}", "version": version}
        )
    # Top-level pi bundle (pi's analog of the marketplace listing).
    with open(os.path.join(root, "package.json"), "w") as f:
        json.dump(
            {
                "name": "test-marketplace",
                "version": "1.0.0",
                "keywords": ["pi-package"],
                "pi": {"skills": [], "extensions": []},
            },
            f,
        )
        if spec.get("hooks") is not None:
            hdir = os.path.join(pdir, "hooks")
            os.makedirs(hdir, exist_ok=True)
            with open(os.path.join(hdir, "hooks.json"), "w") as f:
                json.dump(spec["hooks"], f)
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


class PiManifestTest(unittest.TestCase):
    """The checker now validates each plugin's `pi` package.json manifest and
    the top-level pi bundle (docs/pi.md)."""

    def test_missing_package_json_is_an_error(self):
        root = make_marketplace({"foo": {}})
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        os.remove(os.path.join(root, "plugins", "foo", "package.json"))
        r = run(root)
        out = r.stdout.decode()
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("package.json", out)

    def test_package_json_without_pi_key_is_an_error(self):
        root = make_marketplace({"foo": {}})
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        with open(os.path.join(root, "plugins", "foo", "package.json"), "w") as f:
            json.dump({"name": "foo", "version": "1.0.0"}, f)
        r = run(root)
        out = r.stdout.decode()
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("no `pi` manifest", out)

    def test_pi_version_drift_is_an_error(self):
        root = make_marketplace({"foo": {}})
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        with open(os.path.join(root, "plugins", "foo", "package.json"), "w") as f:
            json.dump(
                {
                    "name": "foo",
                    "version": "9.9.9",
                    "keywords": ["pi-package"],
                    "pi": {"skills": [], "extensions": []},
                },
                f,
            )
        r = run(root)
        out = r.stdout.decode()
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("version drift", out)

    def test_valid_pi_manifest_passes(self):
        # A real skills dir + extension file (which references a real hook
        # script) must pass cleanly.
        spec = {
            "pi": {
                "skills": ["./skills"],
                "extensions": ["./extensions/hooks.ts"],
            }
        }
        root = make_marketplace({"foo": spec})
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        pdir = os.path.join(root, "plugins", "foo")
        os.makedirs(os.path.join(pdir, "skills", "bar"))
        with open(os.path.join(pdir, "skills", "bar", "SKILL.md"), "w") as f:
            f.write("---\nname: bar\ndescription: x\n---\n\nBody.\n")
        os.makedirs(os.path.join(pdir, "extensions"))
        with open(os.path.join(pdir, "extensions", "hooks.ts"), "w") as f:
            f.write('runHook("scripts/hook.py")\n')
        os.makedirs(os.path.join(pdir, "scripts"))
        with open(os.path.join(pdir, "scripts", "hook.py"), "w") as f:
            f.write("import json\n")
        r = run(root)
        self.assertEqual(r.returncode, 0, r.stdout.decode())

    def test_extension_referencing_missing_script_is_an_error(self):
        # The real extensions pass bare filenames to runHook/runGuard (not
        # `scripts/...` paths), so the checker must catch a bare reference to
        # a script that doesn't exist anywhere.
        spec = {"pi": {"extensions": ["./extensions/hooks.ts"]}}
        root = make_marketplace({"foo": spec})
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        pdir = os.path.join(root, "plugins", "foo")
        os.makedirs(os.path.join(pdir, "extensions"))
        with open(os.path.join(pdir, "extensions", "hooks.ts"), "w") as f:
            f.write('runHook("ghost.py")\n')
        r = run(root)
        out = r.stdout.decode()
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("ghost.py", out)

    def test_extension_referencing_hooks_script_is_fine(self):
        # guardrails' script lives under hooks/, not scripts/ — a bare ref to
        # it must resolve there, not false-error.
        spec = {"pi": {"extensions": ["./extensions/hooks.ts"]}}
        root = make_marketplace({"foo": spec})
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        pdir = os.path.join(root, "plugins", "foo")
        os.makedirs(os.path.join(pdir, "extensions"))
        os.makedirs(os.path.join(pdir, "hooks"))
        with open(os.path.join(pdir, "extensions", "hooks.ts"), "w") as f:
            f.write('runGuard("_block-main-commits.py")\n')
        with open(os.path.join(pdir, "hooks", "_block-main-commits.py"), "w") as f:
            f.write("import json\n")
        r = run(root)
        self.assertEqual(r.returncode, 0, r.stdout.decode())


if __name__ == "__main__":
    unittest.main()
