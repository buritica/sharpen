#!/usr/bin/env python3
"""`record-gate.py --carry` (sharpen#37): which commits may carry the review
gates, and which may not. Split from test_convergence.py for the runner's
per-file timeout.
"""

import os
import unittest

from test_receipts import BRANCH, ReceiptTestBase, git, ignore_artifacts

import gate_convergence as gc


class DocsSafeListTest(unittest.TestCase):
    """The carry's docs class must never cover a file an agent executes."""

    SAFE = [
        "README.md",
        "plugins/sdlc/README.md",
        "CHANGELOG.md",
        "plugins/grumpy/CHANGELOG.md",
        "LICENSE",
        "CONTRIBUTING.rst",
        "docs/pi.md",
        "docs/guide/setup.txt",
        "assets/logo.png",
    ]
    UNSAFE = [
        "plugins/sdlc/commands/gate.md",
        "plugins/grumpy/skills/review/SKILL.md",
        "docs/SKILL.md",
        "AGENTS.md",
        "CLAUDE.md",
        "plugins/sdlc/templates/agents-sdlc.md",
        "plugins/x/agents/reviewer.md",
        "plugins/x/hooks/hooks.json",
        "docs/commands/readme.md",
        ".claude/skills/sdlc/README.md",
        ".github/copilot-instructions.md",
        "plugins/sdlc/UPDATE.md",
        "notes.md",
        "docs/diagram.svg",
        "src/app.py",
        "README.py",
    ]

    def test_safe_list(self):
        for path in self.SAFE:
            self.assertTrue(gc.is_docs_safe(path), path)

    def test_agent_executed_and_unknown_files_are_not_safe(self):
        for path in self.UNSAFE:
            self.assertFalse(gc.is_docs_safe(path), path)


class CarryTest(ReceiptTestBase):
    """`--carry` keeps the review gates over commits that can't change what
    the review saw, and nothing else (sharpen#37)."""

    def setUp(self):
        super().setUp()
        ignore_artifacts(self.repo)
        self.write("src.py", "def f():\n    return 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "code")
        self.run_chain()

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def commit_file(self, rel, text, msg="post-chain"):
        self.write(rel, text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", msg)

    def carry(self, *args):
        return self.cli("--carry", *args)

    def assert_carried(self, r, klass):
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"({klass}", r.stderr)
        self.assertEqual(self.bd()["carries"][-1]["class"], klass)

    def finish_checks(self):
        for gate in ("tests", "lint", "typecheck"):
            self.assertEqual(self.cli("--record", gate).returncode, 0)

    def test_whitespace_change_carries_and_reopens_the_checks(self):
        self.commit_file("src.py", "def f():\n\n    return 1   \n")
        self.assertEqual(self.pr_create().returncode, 2)
        r = self.carry()
        self.assert_carried(r, "format")
        self.assertIn("re-run tests, lint, typecheck", r.stderr)
        self.assertNotIn("tests", self.gates())
        self.assertEqual(self.pr_create().returncode, 2)
        self.finish_checks()
        self.assertEqual(self.pr_create().returncode, 0, self.pr_create().stderr)

    def test_safe_docs_carry(self):
        self.commit_file("README.md", "# x\n")
        self.commit_file("docs/use.md", "how\n")
        self.assert_carried(self.carry(), "docs")
        self.finish_checks()
        self.assertEqual(self.pr_create().returncode, 0)

    def test_agent_executed_markdown_never_carries_as_docs(self):
        self.commit_file("plugins/x/commands/do.md", "do the thing\n")
        r = self.carry()
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot carry", r.stderr)
        self.assertIn("plugins/x/commands/do.md", r.stderr)
        self.assertNotIn("carries", self.bd())
        self.assertEqual(self.pr_create().returncode, 2)

    def test_skill_file_under_docs_never_carries_as_docs(self):
        self.commit_file("docs/SKILL.md", "be an agent\n")
        self.assertEqual(self.carry().returncode, 1)

    def test_code_change_is_refused_without_a_reason(self):
        self.commit_file("src.py", "def f():\n    return 2\n")
        r = self.carry()
        self.assertEqual(r.returncode, 1)
        self.assertIn("--reason", r.stderr)

    def test_small_comment_fix_carries_with_a_reason(self):
        self.commit_file("src.py", "def f():\n    # one\n    return 1\n")
        r = self.carry("--reason", "comment only")
        self.assert_carried(r, "manual")
        self.assertEqual(self.bd()["carries"][-1]["reason"], "comment only")
        self.assertIn("comment only", self.cli("--status").stdout)

    def test_blank_reason_is_refused(self):
        self.commit_file("src.py", "def f():\n    # one\n    return 1\n")
        self.assertEqual(self.carry("--reason", "  ").returncode, 1)

    def test_manual_carry_is_capped(self):
        body = "".join(f"# line {i}\n" for i in range(gc.MANUAL_CARRY_MAX_LINES + 1))
        self.commit_file("src.py", body + "def f():\n    return 1\n")
        r = self.carry("--reason", "comments")
        self.assertEqual(r.returncode, 1)
        self.assertIn("capped", r.stderr)

    def test_manual_carry_cannot_add_files(self):
        self.commit_file("new.py", "x = 1\n")
        r = self.carry("--reason", "tiny")
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot add files", r.stderr)

    def test_clean_rebase_carries(self):
        git(self.repo, "checkout", "-q", "main")
        self.commit_file("other.txt", "upstream\n", msg="upstream")
        git(self.repo, "checkout", "-q", BRANCH)
        git(self.repo, "rebase", "-q", "main")
        self.assertEqual(self.pr_create().returncode, 2)
        self.assert_carried(self.carry(), "rebase")
        self.finish_checks()
        self.assertEqual(self.pr_create().returncode, 0)

    def test_rewrite_that_changes_the_patch_is_refused(self):
        self.write("src.py", "def f():\n    return 3\n")
        git(self.repo, "commit", "-q", "-a", "--amend", "--no-edit")
        r = self.carry()
        self.assertEqual(r.returncode, 1)
        self.assertIn("patch changed", r.stderr)

    def test_carries_chain(self):
        self.commit_file("src.py", "def f():\n    return 1  \n")
        self.assert_carried(self.carry(), "format")
        self.commit_file("README.md", "# x\n")
        self.assert_carried(self.carry(), "docs")
        self.finish_checks()
        self.assertEqual(self.pr_create().returncode, 0)
        status = self.cli("--status").stdout
        self.assertIn("Carried (format)", status)
        self.assertIn("Carried (docs)", status)

    def test_nothing_to_carry_on_head(self):
        r = self.carry()
        self.assertEqual(r.returncode, 0)
        self.assertIn("nothing to carry", r.stderr)
        self.assertNotIn("carries", self.bd())

    def test_stale_block_points_at_carry(self):
        self.commit_file("README.md", "# x\n")
        self.assertIn("--carry", self.pr_create().stderr)

    def test_init_clears_carries(self):
        self.commit_file("README.md", "# x\n")
        self.assert_carried(self.carry(), "docs")
        self.cli("--init", "small-medium")
        self.assertNotIn("carries", self.bd())

    def test_reason_flag_is_refused_elsewhere(self):
        r = self.cli("--status", "--reason", "x")
        self.assertEqual(r.returncode, 1)

    def test_reindent_is_not_format(self):
        self.commit_file("src.py", "def f():\n        return 1\n")
        r = self.carry()
        self.assertEqual(r.returncode, 1)
        self.assertIn("--reason", r.stderr)

    def test_whitespace_inside_a_string_is_not_format(self):
        self.commit_file("msg.py", 'M = "a b"\n', msg="add")
        self.run_chain_again()
        self.commit_file("msg.py", 'M = "a  b"\n')
        self.assertEqual(self.carry().returncode, 1)

    def run_chain_again(self):
        self.cli("--init", "small-medium")
        self.run_chain()

    def test_rename_is_not_format(self):
        git(self.repo, "mv", "src.py", "lib.py")
        git(self.repo, "commit", "-q", "-m", "move")
        r = self.carry()
        self.assertEqual(r.returncode, 1)

    def test_moving_a_command_into_docs_is_not_docs(self):
        self.commit_file("plugins/x/commands/do.md", "do it\n", msg="cmd")
        self.run_chain_again()
        os.makedirs(os.path.join(self.repo, "docs"), exist_ok=True)
        git(self.repo, "mv", "plugins/x/commands/do.md", "docs/do.md")
        git(self.repo, "commit", "-q", "-m", "move")
        r = self.carry()
        self.assertEqual(r.returncode, 1)
        self.assertIn("plugins/x/commands/do.md", r.stderr)

    def test_rebase_that_reindents_is_refused(self):
        git(self.repo, "checkout", "-q", "main")
        self.commit_file("other.txt", "upstream\n", msg="upstream")
        git(self.repo, "checkout", "-q", BRANCH)
        git(self.repo, "rebase", "-q", "main")
        self.write("src.py", "def f():\n        return 1\n")
        git(self.repo, "commit", "-q", "-a", "--amend", "--no-edit")
        r = self.carry()
        self.assertEqual(r.returncode, 1)
        self.assertIn("patch changed", r.stderr)

    def test_manual_carry_covers_a_small_agent_file_fix(self):
        # The honest path for a wording fix to a prompt after review: a carry
        # with a reason on the record, never a docs carry and never a review.
        self.commit_file("plugins/x/commands/do.md", "do the thing\n", msg="cmd")
        self.run_chain_again()
        self.commit_file("plugins/x/commands/do.md", "do the one thing\n")
        self.assertEqual(self.carry().returncode, 1)
        self.assert_carried(self.carry("--reason", "wording"), "manual")

    def test_reset_to_the_reviewed_commit_is_not_stale(self):
        reviewed = git(self.repo, "rev-parse", "HEAD").strip()
        self.commit_file("src.py", "def f():\n    return 2\n")
        self.assertEqual(self.pr_create().returncode, 2)
        git(self.repo, "reset", "-q", "--hard", reviewed)
        self.assertEqual(self.pr_create().returncode, 0, self.pr_create().stderr)


class CarryEdgeTest(CarryTest):
    """Edges the imagine pass found: settings and inputs that must not widen
    or narrow a class."""

    def test_noprefix_config_does_not_open_bookkeeping(self):
        git(self.repo, "config", "diff.noprefix", "true")
        self.commit_file("src.py", "def f():\n    return 2\n")
        self.assertEqual(self.carry().returncode, 1)

    def test_crlf_is_not_format(self):
        self.commit_file("src.py", "def f():\r\n    return 1\r\n")
        self.assertEqual(self.carry().returncode, 1)

    def test_mode_change_is_not_format(self):
        os.chmod(os.path.join(self.repo, "src.py"), 0o755)
        git(self.repo, "commit", "-q", "-am", "chmod")
        self.assertEqual(self.carry().returncode, 1)

    def test_manual_cap_covers_all_manual_carries(self):
        self.commit_file("src.py", "# a\n" * 20 + "def f():\n    return 1\n")
        self.assert_carried(self.carry("--reason", "comments"), "manual")
        self.assertEqual(self.bd()["carries"][-1]["lines"], 20)
        self.commit_file("src.py", "# a\n" * 32 + "def f():\n    return 1\n")
        r = self.carry("--reason", "more comments")
        self.assertEqual(r.returncode, 1)
        self.assertIn("plus 20 from earlier", r.stderr)

    def test_manual_carry_refuses_binary(self):
        self.commit_file("blob.bin", "x", msg="bin")
        self.cli("--init", "small-medium")
        self.run_chain()
        with open(os.path.join(self.repo, "blob.bin"), "wb") as f:
            f.write(b"\x00\x01" * 50)
        git(self.repo, "commit", "-q", "-am", "binary")
        r = self.carry("--reason", "tiny")
        self.assertEqual(r.returncode, 1)
        self.assertIn("binary", r.stderr)

    def test_rebase_past_an_upstream_function_line_carries(self):
        # Upstream adds a top-level line above the branch's hunk: the hunk's
        # function context changes, the branch's own change does not.
        git(self.repo, "checkout", "-q", "main")
        self.commit_file("lib.py", "A = 1\n" + "x = 0\n" * 10, msg="lib")
        git(self.repo, "checkout", "-q", BRANCH)
        git(self.repo, "rebase", "-q", "main")
        self.commit_file("lib.py", "A = 1\n" + "x = 0\n" * 10 + "y = 1\n", msg="y")
        self.cli("--init", "small-medium")
        self.run_chain()
        git(self.repo, "checkout", "-q", "main")
        self.commit_file("lib.py", "Z = 9\nA = 1\n" + "x = 0\n" * 10, msg="z")
        git(self.repo, "checkout", "-q", BRANCH)
        git(self.repo, "rebase", "-q", "main")
        self.assert_carried(self.carry(), "rebase")

    def test_clean_merge_of_main_is_a_rebase(self):
        git(self.repo, "checkout", "-q", "main")
        self.commit_file("other.txt", "upstream\n", msg="upstream")
        git(self.repo, "checkout", "-q", BRANCH)
        git(self.repo, "merge", "-q", "--no-edit", "main")
        self.assert_carried(self.carry(), "rebase")

    def test_reset_past_a_carry_then_docs_carries(self):
        reviewed = git(self.repo, "rev-parse", "HEAD").strip()
        self.commit_file("README.md", "# x\n")
        self.assert_carried(self.carry(), "docs")
        git(self.repo, "reset", "-q", "--hard", reviewed)
        self.commit_file("docs/use.md", "how\n")
        self.assert_carried(self.carry(), "docs")

    def test_reason_is_kept_on_a_mechanical_carry(self):
        self.commit_file("README.md", "# x\n")
        self.assert_carried(self.carry("--reason", "typo"), "docs")
        self.assertEqual(self.bd()["carries"][-1]["reason"], "typo")

    def test_git_failure_does_not_send_you_to_reset(self):
        self.commit_file("src.py", "def f():\n    return 2\n")

        def zero(data):
            heads = data[BRANCH].get("evidence_heads", {})
            data[BRANCH]["evidence_heads"] = {g: "0" * 40 for g in heads}

        self.edit_store(zero)
        r = self.carry()
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a reason to reset", r.stderr)
        self.assertNotIn("Re-run the chain", r.stderr)


class BookkeepingCarryTest(ReceiptTestBase):
    """Release bookkeeping (version fields, render stamps, release notes)
    carries; anything else in those files does not."""

    PLUGIN = "plugins/x/.claude-plugin/plugin.json"
    LOG = "plugins/x/CHANGELOG.md"

    def plugin_json(self, version, description="does x"):
        return (
            "{\n"
            '  "name": "x",\n'
            f'  "description": "{description}",\n'
            f'  "version": "{version}"\n'
            "}\n"
        )

    def setUp(self):
        super().setUp()
        ignore_artifacts(self.repo)
        git(self.repo, "checkout", "-q", "main")
        self.write(self.PLUGIN, self.plugin_json("1.0.0"))
        self.write(self.LOG, "# log\n")
        self.commit_all("base")
        git(self.repo, "checkout", "-q", BRANCH)
        git(self.repo, "merge", "-q", "--ff-only", "main")
        self.write("src.py", "def f():\n    return 1\n")
        self.write(self.PLUGIN, self.plugin_json("1.1.0", "does x and y"))
        self.write(self.LOG, "# log\n\n## 1.1.0 - mine\n")
        self.commit_all("code")
        self.run_chain()

    write = CarryTest.write
    carry = CarryTest.carry
    assert_carried = CarryTest.assert_carried
    finish_checks = CarryTest.finish_checks

    def commit_all(self, msg):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", msg)

    def restack(self, version, description="does x and y", extra=None):
        """Rebuild the branch on a main that took the version it had."""
        git(self.repo, "checkout", "-q", "main")
        self.write(self.PLUGIN, self.plugin_json("1.1.0"))
        self.write(self.LOG, "# log\n\n## 1.1.0 - theirs\n")
        self.commit_all("upstream release")
        git(self.repo, "checkout", "-q", "-B", BRANCH, "main")
        self.write("src.py", "def f():\n    return 1\n")
        self.write(self.PLUGIN, self.plugin_json(version, description))
        self.write(self.LOG, f"# log\n\n## {version} - mine\n\n## 1.1.0 - theirs\n")
        for rel, text in (extra or {}).items():
            self.write(rel, text)
        self.commit_all("code")

    def test_version_bump_on_the_branch_carries(self):
        self.write(self.PLUGIN, self.plugin_json("1.2.0", "does x and y"))
        self.write(self.LOG, "# log\n\n## 1.2.0 - mine\n")
        self.commit_all("renumber")
        self.assert_carried(self.carry(), "bookkeeping")
        self.assertNotIn("tests", self.gates())
        self.finish_checks()
        self.assertEqual(self.pr_create().returncode, 0)

    def test_restack_with_a_new_version_carries(self):
        self.restack("1.2.0")
        self.assertEqual(self.pr_create().returncode, 2)
        self.assert_carried(self.carry(), "bookkeeping")
        self.finish_checks()
        self.assertEqual(self.pr_create().returncode, 0)

    def test_restack_that_changes_a_version_file_for_real_is_refused(self):
        self.restack("1.2.0", description="does x, y and z")
        r = self.carry()
        self.assertEqual(r.returncode, 1)
        self.assertIn("beyond release bookkeeping", r.stderr)

    def test_restack_with_any_other_change_is_refused(self):
        self.restack("1.2.0", extra={"src.py": "def f():\n    return 2\n"})
        self.assertEqual(self.carry().returncode, 1)

    def test_restack_that_drops_an_upstream_note_is_refused(self):
        self.restack("1.2.0")
        self.write(self.LOG, "# log\n\n## 1.2.0 - mine\n")
        git(self.repo, "commit", "-q", "-a", "--amend", "--no-edit")
        self.assertEqual(self.carry().returncode, 1)

    def test_restack_that_drops_a_reviewed_note_is_refused(self):
        self.restack("1.2.0")
        self.write(self.LOG, "# log\n\n## 1.1.0 - theirs\n")
        git(self.repo, "commit", "-q", "-a", "--amend", "--no-edit")
        self.assertEqual(self.carry().returncode, 1)

    def test_new_note_text_on_a_restack_is_refused(self):
        # On the branch alone a changelog edit is a docs carry; across a
        # restack, note text that is neither reviewed nor upstream is not
        # bookkeeping.
        self.restack("1.2.0")
        self.write(self.LOG, "# log\n\n## 1.2.0 - mine\n\nAlso fixed a bug.\n")
        git(self.repo, "commit", "-q", "-a", "--amend", "--no-edit")
        self.assertEqual(self.carry().returncode, 1)

    def test_version_bump_with_a_real_change_in_the_same_file_is_refused(self):
        self.write(self.PLUGIN, self.plugin_json("1.2.0", "does other things"))
        self.commit_all("renumber and more")
        self.assertEqual(self.carry().returncode, 1)


class CarryLegacyTest(ReceiptTestBase):
    def setUp(self):
        super().setUp()
        self.env["SDLC_GATE_EVIDENCE"] = "events"
        self.cli("--init", "small-medium")

    def test_legacy_cycle_has_nothing_to_carry(self):
        r = self.cli("--carry")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no receipt-backed cycle", r.stderr)


if __name__ == "__main__":
    unittest.main()
