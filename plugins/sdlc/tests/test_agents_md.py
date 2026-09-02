#!/usr/bin/env python3
"""
Stdlib tests for agents_md.py — /sdlc:init's AGENTS.md / CLAUDE.md wiring.

Run: python3 plugins/sdlc/tests/test_agents_md.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
SCRIPT = os.path.join(SCRIPTS, "agents_md.py")
sys.path.insert(0, SCRIPTS)

import agents_md as am  # noqa: E402
import gate_store as gs  # noqa: E402

FACTS = {
    "test_cmd": "bun test",
    "lint_cmd": "bunx biome check .",
    "format_cmd": "bunx biome format --write .",
    "typecheck_cmd": "tsc --noEmit",
    "default_branch": "main",
    "grumpy": True,
    "deploy": None,
}

OLD_SECTION = """## Run gates before every PR

```
/sdlc:gate
```

This is mandatory for any change with executable code. The `sdlc` hook blocks
`gh pr create` until all gates pass.

For docs-only or trivial changes, run `/sdlc:gate --init tiny` before your
first commit.
"""


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class RenderTests(unittest.TestCase):
    def test_all_placeholders_filled(self):
        block = am.render(FACTS)
        self.assertTrue(block.startswith(am.BEGIN + "\n"))
        self.assertTrue(block.endswith("\n" + am.END))
        self.assertNotIn("{", block)
        self.assertNotIn("}", block)
        for cmd in ("`bun test`", "`bunx biome check .`", "`tsc --noEmit`"):
            self.assertIn(cmd, block)
        self.assertIn("origin/main", block)
        self.assertIn("/grumpy:review", block)
        self.assertNotIn("- deploy:", block)

    def test_missing_command_says_not_configured(self):
        block = am.render(dict(FACTS, typecheck_cmd=None, lint_cmd="  "))
        self.assertEqual(block.count(am.NOT_CONFIGURED), 2)
        self.assertIn("- typecheck: " + am.NOT_CONFIGURED, block)

    def test_grumpy_off_and_deploy_line(self):
        block = am.render(
            dict(
                FACTS,
                grumpy=False,
                deploy="CI → halfmoon via bm2",
                default_branch="master",
            )
        )
        self.assertIn("not installed", block)
        self.assertNotIn("/grumpy:review", block)
        self.assertIn(
            "- deploy: CI → halfmoon via bm2\n- CI runs the same commands", block
        )
        self.assertIn("origin/master", block)
        self.assertIn("required check on `master`", block)


class EnforcerConsistencyTests(unittest.TestCase):
    """The block must describe the tiers the store accepts and the chain gate.md
    runs — drift here is authoritative for non-Claude hosts."""

    def test_tier_names_come_from_gate_store(self):
        block = am.render(FACTS)
        for tier in gs.TIERS:
            self.assertIn("**%s**" % tier, block)
        self.assertNotIn("**docs-only**", block)
        self.assertIn("uses the `tiny` cycle", block)
        self.assertEqual(set(am.TIER_NOTES), set(gs.TIERS))

    def test_grumpy_chain_comes_from_gate_store(self):
        line = am.grumpy_on_line()
        for skill in set(gs.SKILL_FOR_GATE.values()):
            self.assertIn("`%s`" % skill, line)
        self.assertEqual(
            am.skill_chain(),
            [
                "/grumpy:simplify",
                "/grumpy:review",
                "/grumpy:fix",
                "/grumpy:imagine",
                "/grumpy:fix",
            ],
        )
        self.assertTrue(line.startswith("Gates 2–6 are `/grumpy:simplify`, then"))


class UpsertTests(unittest.TestCase):
    def test_append_when_absent(self):
        out = am.upsert_block("# repo\n\nhand-written\n", "B")
        self.assertEqual(out, "# repo\n\nhand-written\n\nB\n")
        self.assertEqual(am.upsert_block("", "B"), "B\n")
        self.assertEqual(am.upsert_block("no newline", "B"), "no newline\n\nB\n")

    def test_replace_only_inside_markers(self):
        text = "before\n%s\nold\n%s\nafter\n" % (am.BEGIN, am.END)
        block = "%s\nnew\n%s" % (am.BEGIN, am.END)
        self.assertEqual(
            am.upsert_block(text, block),
            "before\n%s\nnew\n%s\nafter\n" % (am.BEGIN, am.END),
        )

    def test_half_marked_is_error(self):
        with self.assertRaises(am.WireError):
            am.upsert_block("x\n%s\ny\n" % am.BEGIN, "B")
        with self.assertRaises(am.WireError):
            am.upsert_block("x\n%s\ny\n" % am.END, "B")
        with self.assertRaises(am.WireError):
            am.upsert_block("%s\n%s\n" % (am.END, am.BEGIN), "B")


class RemoveOldSectionTests(unittest.TestCase):
    def test_removes_matching_section_between_others(self):
        text = "# repo\n\nintro\n\n" + OLD_SECTION + "\n## Tests\n\nrun them\n"
        out, removed = am.remove_old_section(text)
        self.assertTrue(removed)
        self.assertEqual(out, "# repo\n\nintro\n\n## Tests\n\nrun them\n")

    def test_removes_trailing_section(self):
        out, removed = am.remove_old_section("# repo\n\nintro\n\n" + OLD_SECTION)
        self.assertEqual((out, removed), ("# repo\n\nintro\n", True))

    def test_leaves_same_heading_with_different_body(self):
        text = "## Run gates before every PR\n\nwe do it differently\n"
        self.assertEqual(am.remove_old_section(text), (text, False))

    def test_leaves_unrelated_text(self):
        text = "# repo\n\n## Gates\n\n/sdlc:gate\n"
        self.assertEqual(am.remove_old_section(text), (text, False))


class WireTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="agents-md-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def p(self, name):
        return os.path.join(self.root, name)

    def test_fresh_repo(self):
        results = am.wire(self.root, FACTS)
        self.assertEqual(
            [(r[0], r[1]) for r in results],
            [("AGENTS.md", "created"), ("CLAUDE.md", "created")],
        )
        agents = read(self.p("AGENTS.md"))
        self.assertTrue(
            agents.startswith("# %s\n\n%s\n" % (os.path.basename(self.root), am.BEGIN))
        )
        self.assertTrue(agents.endswith(am.END + "\n"))
        self.assertEqual(read(self.p("CLAUDE.md")), "@AGENTS.md\n")

    def test_existing_agents_content_preserved(self):
        write(self.p("AGENTS.md"), "# ca\n\n## Commands\n- `bun run start`\n")
        am.wire(self.root, FACTS)
        agents = read(self.p("AGENTS.md"))
        self.assertTrue(
            agents.startswith("# ca\n\n## Commands\n- `bun run start`\n\n" + am.BEGIN)
        )

    def test_second_run_is_unchanged_and_changed_fact_updates_only_block(self):
        am.wire(self.root, FACTS)
        first = read(self.p("AGENTS.md"))
        self.assertEqual(
            [r[1] for r in am.wire(self.root, FACTS)], ["unchanged", "unchanged"]
        )
        results = am.wire(self.root, dict(FACTS, test_cmd="bun test --coverage"))
        self.assertEqual([r[1] for r in results], ["updated", "unchanged"])
        second = read(self.p("AGENTS.md"))
        self.assertEqual(
            first[: first.index(am.BEGIN)], second[: second.index(am.BEGIN)]
        )
        self.assertEqual(first.count(am.BEGIN), 1)
        self.assertEqual(second.count(am.BEGIN), 1)
        self.assertIn("`bun test --coverage`", second)
        self.assertNotIn("`bun test`", second)

    def test_claude_without_include_gets_it_prepended(self):
        write(self.p("CLAUDE.md"), "# repo\n\nhand rules\n")
        results = am.wire(self.root, FACTS)
        self.assertEqual(results[1][1], "updated")
        self.assertIn("prepended @AGENTS.md", results[1][2])
        self.assertEqual(
            read(self.p("CLAUDE.md")), "@AGENTS.md\n\n# repo\n\nhand rules\n"
        )

    def test_claude_old_reminder_is_migrated(self):
        write(self.p("CLAUDE.md"), "# repo\n\nrules\n\n" + OLD_SECTION)
        results = am.wire(self.root, FACTS)
        self.assertIn("removed the old", results[1][2])
        self.assertIn("prepended @AGENTS.md", results[1][2])
        self.assertEqual(read(self.p("CLAUDE.md")), "@AGENTS.md\n\n# repo\n\nrules\n")
        self.assertIn("/sdlc:gate --init tiny", read(self.p("AGENTS.md")))

    def test_leftover_gate_mention_is_reported_not_removed(self):
        write(self.p("CLAUDE.md"), "# repo\n\n## Gates\n\nrun /sdlc:gate first\n")
        results = am.wire(self.root, FACTS)
        self.assertIn("still mentions /sdlc:gate at line 7", results[1][2])
        self.assertEqual(
            read(self.p("CLAUDE.md")),
            "@AGENTS.md\n\n# repo\n\n## Gates\n\nrun /sdlc:gate first\n",
        )

    def test_claude_already_including_is_unchanged(self):
        write(self.p("CLAUDE.md"), "@AGENTS.md\n")
        self.assertEqual(am.wire(self.root, FACTS)[1][1], "unchanged")
        write(self.p("CLAUDE.md"), "# x\n\n@AGENTS.md\n\nmore\n")
        self.assertEqual(am.wire(self.root, FACTS)[1][1], "unchanged")

    def test_check_does_not_write(self):
        results = am.wire(self.root, FACTS, check=True)
        self.assertEqual([r[1] for r in results], ["created", "created"])
        self.assertFalse(os.path.exists(self.p("AGENTS.md")))
        self.assertFalse(os.path.exists(self.p("CLAUDE.md")))

    def test_bad_root_is_error(self):
        with self.assertRaises(am.WireError):
            am.wire(os.path.join(self.root, "nope"), FACTS)

    def test_half_marked_agents_is_error_and_untouched(self):
        write(self.p("AGENTS.md"), "# repo\n%s\n" % am.BEGIN)
        with self.assertRaises(am.WireError):
            am.wire(self.root, FACTS)
        self.assertEqual(read(self.p("AGENTS.md")), "# repo\n%s\n" % am.BEGIN)
        self.assertFalse(os.path.exists(self.p("CLAUDE.md")))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="agents-md-cli-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, SCRIPT, "--root", self.root] + list(args),
            capture_output=True,
            text=True,
        )

    def test_writes_and_prints_table(self):
        p = self.run_cli(
            "--test-cmd", "pytest", "--lint-cmd", "ruff check .", "--grumpy"
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "AGENTS.md | created\nCLAUDE.md | created\n")
        self.assertIn("`pytest`", read(os.path.join(self.root, "AGENTS.md")))

    def test_check_exit_codes(self):
        p = self.run_cli("--check")
        self.assertEqual(p.returncode, 1)
        self.assertIn("(would change)", p.stdout)
        self.run_cli()
        p = self.run_cli("--check")
        self.assertEqual(
            (p.returncode, p.stdout),
            (0, "AGENTS.md | unchanged\nCLAUDE.md | unchanged\n"),
        )
        p = self.run_cli("--check", "--test-cmd", "pytest")
        self.assertEqual(p.returncode, 1)

    def test_bad_root_exits_2(self):
        p = subprocess.run(
            [sys.executable, SCRIPT, "--root", os.path.join(self.root, "nope")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(p.returncode, 2)
        self.assertIn("not a directory", p.stderr)


if __name__ == "__main__":
    unittest.main()
