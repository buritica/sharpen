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
            dc.parse(text),
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
            dc.parse(text)["test"], ["api: go test ./...", "web: bun test"]
        )

    def test_no_block_is_none(self):
        self.assertIsNone(dc.parse("# Just prose\n\n- test: `make test`\n"))

    def test_command_lists_outside_the_section_are_ignored(self):
        text = rendered(test_cmd=["make test"]).replace(
            "### Pull requests", "### Pull requests\n- lint: `not-a-real-lint`"
        )
        self.assertIsNone(dc.parse(text)["lint"])

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
    def test_gate_reads_declared_commands_before_the_table(self):
        with open(GATE_MD) as f:
            text = f.read()
        section = text.split("## Auto-detection", 1)[1]
        self.assertIn("declared_commands.py", section)
        self.assertLess(
            section.index("declared_commands.py"), section.index("**Test runner:**")
        )
        self.assertIn("AGENTS.md wins", section)


if __name__ == "__main__":
    unittest.main()
