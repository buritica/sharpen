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
        first = gs.GATES_BY_TIER["small-medium"].index("simplify") + 1
        last = gs.GATES_BY_TIER["small-medium"].index("grumpy-fix-post-imagine") + 1
        self.assertIn("Gates %d–%d" % (first, last), line)

    def test_literal_braces_in_template_pass_through(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "t.md")
        write(path, 'cmd: {test_cmd}\njson: {"a": 1} and {} and {Not_A_Key}\n')
        try:
            block = am.render(FACTS, template_path=path)
            self.assertIn('json: {"a": 1} and {} and {Not_A_Key}', block)
            write(path, "{nope}\n")
            with self.assertRaises(am.WireError):
                am.render(FACTS, template_path=path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class StampAndCommandTests(unittest.TestCase):
    def test_block_is_stamped_with_plugin_version(self):
        block = am.render(FACTS)
        self.assertRegex(
            block.splitlines()[1], r"^<!-- rendered by sdlc \d+\.\d+\.\d+ "
        )
        self.assertEqual(am.block_version(block), am.plugin_version())
        self.assertEqual(am.block_version("no block here\n"), None)
        self.assertEqual(am.block_version("%s\nold\n%s\n" % (am.BEGIN, am.END)), "")

    def test_repeated_commands_render_nested_bullets(self):
        block = am.render(dict(FACTS, test_cmd=["web: bun test", "api: pytest"]))
        self.assertIn("- test:\n  - `web: bun test`\n  - `api: pytest`\n", block)
        self.assertEqual(am._cmd(["", "  "]), am.NOT_CONFIGURED)
        self.assertEqual(am._cmd(["x"]), "`x`")


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

    def test_markers_in_prose_are_not_markers(self):
        prose = "Managed between `%s` and `%s`.\n" % (am.BEGIN, am.END)
        block = "%s\nnew\n%s" % (am.BEGIN, am.END)
        once = am.upsert_block("# repo\n\n" + prose, block)
        self.assertEqual(once, "# repo\n\n" + prose + "\n" + block + "\n")
        twice = am.upsert_block(once, "%s\nnewer\n%s" % (am.BEGIN, am.END))
        self.assertEqual(twice.count(am.BEGIN + "\n"), 1)
        self.assertIn(prose, twice)
        self.assertIn("newer", twice)
        self.assertNotIn("\nnew\n", twice)

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

    def test_fenced_hash_lines_do_not_end_the_section(self):
        old = (
            "## Run gates before every PR\n\n```sh\n# run this before every PR\n"
            "/sdlc:gate\n```\n\nMandatory.\n"
        )
        text = "# repo\n\n" + old + "\n## Tests\n\nrun them\n"
        out, removed = am.remove_old_section(text)
        self.assertTrue(removed)
        self.assertEqual(out, "# repo\n\n## Tests\n\nrun them\n")

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

    def test_leftover_mentions_multiple_lines_and_skip_pasted_block(self):
        text = (
            "# repo\n\n/sdlc:gate here\n%s\n/sdlc:gate inside block\n%s\n"
            "and /sdlc:gate again\n" % (am.BEGIN, am.END)
        )
        self.assertEqual(am.leftover_gate_mentions(text), [3, 7])
        write(self.p("CLAUDE.md"), text)
        results = am.wire(self.root, FACTS)
        self.assertIn("at line 5, 9", results[1][2])

    def test_symlinked_claude_is_refused_and_nothing_written(self):
        write(self.p("AGENTS.md"), "# ca\n\nhand\n")
        os.symlink("AGENTS.md", self.p("CLAUDE.md"))
        with self.assertRaises(am.WireError) as ctx:
            am.wire(self.root, FACTS)
        self.assertIn("symlink", str(ctx.exception))
        self.assertEqual(read(self.p("AGENTS.md")), "# ca\n\nhand\n")

    def test_symlinked_agents_is_refused(self):
        write(self.p("CLAUDE.md"), "# repo\n")
        os.symlink("CLAUDE.md", self.p("AGENTS.md"))
        with self.assertRaises(am.WireError):
            am.wire(self.root, FACTS)
        self.assertEqual(read(self.p("CLAUDE.md")), "# repo\n")

    def test_dangling_symlink_is_refused(self):
        os.symlink("nowhere.md", self.p("CLAUDE.md"))
        with self.assertRaises(am.WireError):
            am.wire(self.root, FACTS)
        self.assertFalse(os.path.exists(self.p("AGENTS.md")))

    def test_non_utf8_is_wire_error(self):
        with open(self.p("CLAUDE.md"), "wb") as fh:
            fh.write(b"# caf\xe9\n")
        with self.assertRaises(am.WireError) as ctx:
            am.wire(self.root, FACTS)
        self.assertIn("not UTF-8", str(ctx.exception))

    def test_crlf_is_preserved_per_file(self):
        with open(self.p("CLAUDE.md"), "wb") as fh:
            fh.write(b"# repo\r\n\r\nrules\r\n")
        am.wire(self.root, FACTS)
        with open(self.p("CLAUDE.md"), "rb") as fh:
            raw = fh.read()
        self.assertEqual(raw, b"@AGENTS.md\r\n\r\n# repo\r\n\r\nrules\r\n")
        with open(self.p("AGENTS.md"), "rb") as fh:
            self.assertNotIn(b"\r\n", fh.read())
        self.assertEqual(
            [r[1] for r in am.wire(self.root, FACTS)], ["unchanged", "unchanged"]
        )

    def test_no_temp_file_left_behind(self):
        am.wire(self.root, FACTS)
        self.assertEqual(sorted(os.listdir(self.root)), ["AGENTS.md", "CLAUDE.md"])

    def test_failed_second_write_names_the_first(self):
        write(self.p("CLAUDE.md"), "# repo\n")
        os.chmod(self.root, 0o555)
        try:
            with self.assertRaises(am.WireError) as ctx:
                am.wire(self.root, FACTS)
            self.assertIn("could not write AGENTS.md", str(ctx.exception))
        finally:
            os.chmod(self.root, 0o755)

    def test_agents_old_reminder_is_removed_too(self):
        write(self.p("AGENTS.md"), "# repo\n\n" + OLD_SECTION + "\n## Rules\n\nkeep\n")
        results = am.wire(self.root, FACTS)
        self.assertIn("removed the old", results[0][2])
        agents = read(self.p("AGENTS.md"))
        self.assertNotIn(am.OLD_HEADING, agents)
        self.assertIn("## Rules\n\nkeep\n", agents)
        self.assertEqual(agents.count(am.BEGIN + "\n"), 1)

    def test_no_claude_manages_agents_only(self):
        results = am.wire(self.root, FACTS, claude=False)
        self.assertEqual([r[0] for r in results], ["AGENTS.md"])
        self.assertFalse(os.path.exists(self.p("CLAUDE.md")))

    def test_read_only_target_is_refused(self):
        write(self.p("AGENTS.md"), "# repo\n")
        os.chmod(self.p("AGENTS.md"), 0o444)
        try:
            with self.assertRaises(am.WireError) as ctx:
                am.wire(self.root, FACTS)
            self.assertIn("read-only", str(ctx.exception))
        finally:
            os.chmod(self.p("AGENTS.md"), 0o644)

    def test_failed_rename_leaves_no_temp_file(self):
        real = am.os.replace

        def boom(src, dst):
            raise OSError("cross-device")

        am.os.replace = boom
        try:
            with self.assertRaises(am.WireError):
                am.wire(self.root, FACTS)
        finally:
            am.os.replace = real
        self.assertEqual([f for f in os.listdir(self.root) if "sdlc-tmp" in f], [])

    def test_repo_name_uses_main_checkout_from_a_worktree(self):
        self.assertEqual(am.repo_name(self.root), os.path.basename(self.root))

        def git(*a):
            return subprocess.run(
                ["git", "-C", self.root] + list(a),
                check=True,
                capture_output=True,
                text=True,
            )

        git("init", "-q")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        git("config", "commit.gpgsign", "false")
        write(self.p("f"), "x\n")
        git("add", "f")
        git("commit", "-q", "-m", "base")
        wt = os.path.join(self.root, ".claude", "worktrees", "feature-x")
        git("worktree", "add", "-q", "-b", "feature-x", wt)
        self.assertEqual(am.repo_name(wt), os.path.basename(self.root))
        am.wire(wt, FACTS)
        self.assertTrue(
            read(os.path.join(wt, "AGENTS.md")).startswith(
                "# %s\n" % os.path.basename(self.root)
            )
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
        p = self.run_cli("--check", "--test-cmd", "pytest")
        self.assertEqual(p.returncode, 1)
        self.assertIn("(would change)", p.stdout)
        self.run_cli("--test-cmd", "pytest")
        p = self.run_cli("--check", "--test-cmd", "pytest")
        self.assertEqual(
            (p.returncode, p.stdout),
            (0, "AGENTS.md | unchanged\nCLAUDE.md | unchanged\n"),
        )
        p = self.run_cli("--check", "--test-cmd", "bun test")
        self.assertEqual(p.returncode, 1)

    def test_deploy_and_default_branch_flags(self):
        p = self.run_cli("--deploy", "CI → prod via bm2", "--default-branch", "master")
        self.assertEqual(p.returncode, 0, p.stderr)
        agents = read(os.path.join(self.root, "AGENTS.md"))
        self.assertIn("- deploy: CI → prod via bm2", agents)
        self.assertIn("origin/master", agents)

    def test_non_utf8_exits_2_not_1(self):
        with open(os.path.join(self.root, "CLAUDE.md"), "wb") as fh:
            fh.write(b"\xff\xfe")
        p = self.run_cli("--check")
        self.assertEqual(p.returncode, 2)
        self.assertNotIn("Traceback", p.stderr)

    def test_check_without_facts_compares_the_stamp(self):
        p = self.run_cli("--check")
        self.assertEqual(p.returncode, 1)
        self.assertIn("AGENTS.md | missing", p.stdout)
        self.run_cli("--test-cmd", "pytest")
        p = self.run_cli("--check")
        self.assertEqual(
            (p.returncode, p.stdout.splitlines()[0]),
            (0, "AGENTS.md | current | rendered by sdlc %s" % am.plugin_version()),
        )
        agents = os.path.join(self.root, "AGENTS.md")
        write(
            agents,
            read(agents).replace(
                "rendered by sdlc %s " % am.plugin_version(), "rendered by sdlc 0.0.1 "
            ),
        )
        p = self.run_cli("--check")
        self.assertEqual(p.returncode, 1)
        self.assertIn("AGENTS.md | stale | rendered by sdlc 0.0.1, installed", p.stdout)
        os.remove(os.path.join(self.root, "CLAUDE.md"))
        p = self.run_cli("--check", "--no-claude")
        self.assertNotIn("CLAUDE.md", p.stdout)

    def test_no_flags_warns_on_stderr(self):
        p = self.run_cli()
        self.assertEqual(p.returncode, 0)
        self.assertIn("no --*-cmd given", p.stderr)
        p = self.run_cli("--test-cmd", "pytest")
        self.assertEqual(p.stderr, "")

    def test_repeated_flags_via_cli(self):
        p = self.run_cli("--test-cmd", "web: bun test", "--test-cmd", "api: pytest")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("  - `api: pytest`", read(os.path.join(self.root, "AGENTS.md")))

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
