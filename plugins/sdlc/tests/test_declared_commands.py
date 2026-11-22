#!/usr/bin/env python3
"""Tests for scripts/declared_commands.py: `/sdlc:gate` reads the commands
`/sdlc:init` wrote into AGENTS.md, so what init renders must read back.

Run: python3 plugins/sdlc/tests/test_declared_commands.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
SCRIPT = os.path.join(SCRIPTS, "declared_commands.py")
GATE_MD = os.path.join(HERE, "..", "commands", "gate.md")
sys.path.insert(0, SCRIPTS)

import agents_md as am  # noqa: E402
import declared_commands as dc  # noqa: E402


def rendered(**facts):
    return "# Repo rules\n\n" + am.render(facts) + "\n"


class ParseTests(unittest.TestCase):
    def test_round_trip_from_what_init_renders(self):
        text = rendered(
            test_cmd=["python3 scripts/run-tests.py"],
            lint_cmd=["ruff format --check . && ruff check ."],
            format_cmd=["ruff format ."],
        )
        self.assertEqual(
            dc.parse(text)[0],
            {
                "test": ["python3 scripts/run-tests.py"],
                "lint": ["ruff format --check . && ruff check ."],
                "format": ["ruff format ."],
                "typecheck": None,  # rendered as "not configured — …"
            },
        )

    def test_monorepo_commands_read_back_as_a_list(self):
        text = rendered(test_cmd=["api: go test ./...", "web: bun test"])
        self.assertEqual(
            dc.parse(text)[0]["test"], ["api: go test ./...", "web: bun test"]
        )

    def test_no_block_is_none(self):
        self.assertIsNone(dc.parse("# Just prose\n\n- test: `make test`\n"))

    def test_command_lists_outside_the_section_are_ignored(self):
        text = rendered(test_cmd=["make test"]).replace(
            "### Pull requests", "### Pull requests\n- lint: `not-a-real-lint`"
        )
        self.assertIsNone(dc.parse(text)[0]["lint"])

    def test_a_deploy_line_ends_a_nested_list(self):
        text = rendered(test_cmd=["web: bun test", "api: pytest"], deploy="fly deploy")
        commands, unreadable = dc.parse(text)
        self.assertEqual(commands["test"], ["web: bun test", "api: pytest"])
        self.assertEqual(unreadable, [])

    def test_crlf_reads_the_same(self):
        text = rendered(test_cmd=["make test"]).replace("\n", "\r\n")
        self.assertEqual(dc.parse(text)[0]["test"], ["make test"])

    def test_an_annotated_line_is_reported_not_silently_dropped(self):
        text = rendered(test_cmd=["make test"]).replace(
            "- test: `make test`", "- test: `make test` (needs docker)"
        )
        commands, unreadable = dc.parse(text)
        self.assertIsNone(commands["test"])
        self.assertEqual(unreadable, ["test"])

    def _edit(self, old, new):
        text = rendered(test_cmd=["web: bun test", "api: pytest"])
        self.assertIn(old, text)
        return dc.parse(text.replace(old, new))

    def test_an_unbackticked_list_item_flags_the_whole_entry(self):
        commands, unreadable = self._edit("  - `web: bun test`", "  - web: bun test")
        self.assertIsNone(commands["test"])  # not just the half after it
        self.assertEqual(unreadable, ["test"])

    def test_a_differently_shaped_entry_is_reported(self):
        for loose in ("- Test:", "* test:"):
            commands, unreadable = self._edit("- test:", loose)
            self.assertIsNone(commands["test"], loose)
            self.assertEqual(unreadable, ["test"], loose)

    def test_a_duplicate_entry_is_reported(self):
        commands, unreadable = self._edit("- lint:", "- test: `make test`\n- lint:")
        self.assertIsNone(commands["test"])
        self.assertEqual(unreadable, ["test"])

    def test_not_configured_is_null_and_readable(self):
        commands, unreadable = dc.parse(rendered(test_cmd=["make test"]))
        self.assertIsNone(commands["typecheck"])
        self.assertEqual(unreadable, [])

    def test_this_repos_own_block(self):
        root = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
        path = os.path.join(root, "AGENTS.md")
        if not os.path.exists(path):
            self.skipTest("no AGENTS.md next to the plugin")
        result = dc.read(root)
        self.assertEqual(result["source"], "AGENTS.md")
        self.assertEqual(result["commands"]["test"], ["python3 scripts/run-tests.py"])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def run_cli(self):
        return subprocess.run(
            [sys.executable, SCRIPT, "--root", self.root],
            capture_output=True,
            text=True,
        )

    def test_no_agents_md_reports_no_source(self):
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertIsNone(out["source"])
        self.assertEqual(out["commands"], dict.fromkeys(dc.NAMES))
        self.assertEqual(out["unreadable"], [])

    def test_current_block_is_not_stale(self):
        with open(os.path.join(self.root, "AGENTS.md"), "w") as f:
            f.write(rendered(test_cmd=["make test"]))
        out = json.loads(self.run_cli().stdout)
        self.assertEqual(out["source"], "AGENTS.md")
        self.assertFalse(out["stale"])
        self.assertEqual(out["commands"]["test"], ["make test"])

    def test_old_block_is_stale_but_still_read(self):
        text = rendered(test_cmd=["make test"]).replace(am.stamp(), am.stamp("0.0.1"))
        with open(os.path.join(self.root, "AGENTS.md"), "w") as f:
            f.write(text)
        out = json.loads(self.run_cli().stdout)
        self.assertTrue(out["stale"])
        self.assertEqual(out["commands"]["test"], ["make test"])

    def test_broken_markers_exit_2(self):
        with open(os.path.join(self.root, "AGENTS.md"), "w") as f:
            f.write(am.BEGIN + "\nno end marker\n")
        r = self.run_cli()
        self.assertEqual(r.returncode, 2)
        self.assertIn("declared_commands:", r.stderr)


class GateUsesItTests(unittest.TestCase):
    def setUp(self):
        with open(GATE_MD) as f:
            text = " ".join(f.read().split("## Auto-detection", 1)[1].split())
        self.section = text

    def test_gate_reads_declared_commands_before_the_table(self):
        self.assertIn("declared_commands.py", self.section)
        self.assertLess(
            self.section.index("declared_commands.py"),
            self.section.index("**Test runner:**"),
        )
        self.assertIn("AGENTS.md wins", self.section)

    def test_root_is_the_repo_toplevel_not_the_cwd(self):
        self.assertIn(
            '--root "$(git -C "${WT:-.}" rev-parse --show-toplevel)"', self.section
        )

    def test_format_is_never_run_and_monorepo_areas_are_split(self):
        self.assertIn("never run `format`", self.section)
        self.assertIn("one entry is a plain command", self.section)
        self.assertIn("before its first colon", self.section)
        self.assertIn("no `area:` prefix run in turn from the root", self.section)
        self.assertIn("`unreadable`", self.section)

    def test_only_a_missing_binary_falls_back_to_detection(self):
        # A script's own 127 (a tool it calls is missing) must fail the gate,
        # never swap in another runner that could pass.
        self.assertIn("`command -v <first word>` fails", self.section)
        self.assertIn("fails the gate, whatever its exit code", self.section)
        self.assertNotIn("not found (exit 127)", self.section)

    def test_commands_run_from_the_root_and_skip_env_assignments(self):
        self.assertIn("from the repo root", self.section)
        self.assertIn("never the session's cwd", self.section)
        self.assertIn("skipping leading `NAME=value` words", self.section)
        self.assertIn("single space-free token", self.section)

    def test_update_notes_carry_the_install_check_rule(self):
        with open(os.path.join(os.path.dirname(GATE_MD), "..", "UPDATE.md")) as f:
            notes = " ".join(f.read().split())
        self.assertNotIn("(exit 127)", notes)
        self.assertIn("fails the gate, whatever its exit code", notes)

    def test_package_json_lint_never_overrides_a_declared_lint(self):
        self.assertIn(
            "When AGENTS.md declares no `lint` and package.json", self.section
        )

    def test_shellcheck_stays_additive_to_a_declared_lint(self):
        self.assertIn("a declared `lint` that doesn't already run it", self.section)


class IsolatedModeTest(unittest.TestCase):
    def test_runs_under_python_isolated_mode(self):
        r = subprocess.run(
            [sys.executable, "-I", SCRIPT, "--root", tempfile.gettempdir()],
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
