#!/usr/bin/env python3
"""Convergence of the review loop (sharpen#37): `--carry`, the round
counter, the review base for stacked branches, and one-fix-one-gate
receipts. Split from test_receipts.py, whose harness it reuses, to keep each
suite under the runner's per-file timeout.
"""

import json
import os
import re
import unittest

import time

from test_receipts import (
    BRANCH,
    GRUMPY_RECEIPT,
    ReceiptTestBase,
    git,
    ignore_artifacts,
)


class StackTest(ReceiptTestBase):
    """A three-layer stack: main <- layer-a <- layer-b <- BRANCH. The review
    round covers only BRANCH's own layer, and carries survive restacks."""

    def setUp(self):
        super().setUp()
        ignore_artifacts(self.repo)
        git(self.repo, "checkout", "-q", "-b", "layer-a", "main")
        self.commit_file("a.py", "A = 1\n" * 5)
        git(self.repo, "checkout", "-q", "-b", "layer-b")
        self.commit_file("b.py", "B = 1\n" * 7)
        git(self.repo, "checkout", "-q", BRANCH)
        git(self.repo, "reset", "-q", "--hard", "layer-b")
        self.commit_file("c.py", "C = 1\n" * 3)

    def write(self, rel, text):
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)

    def carry(self, *args):
        return self.cli("--carry", *args)

    def assert_carried(self, r, klass):
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.bd()["carries"][-1]["class"], klass)

    def commit_file(self, rel, text, msg=None):
        self.write(rel, text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", msg or f"add {rel}")

    def base_file(self):
        path = os.path.join(self.repo, ".sharpen", "sdlc", BRANCH, "base.json")
        with open(path) as f:
            return json.load(f)

    def start(self):
        r = self.cli("--init", "small-medium")
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_explicit_base_scopes_the_round_to_the_top_layer(self):
        self.assertEqual(self.cli("--set-base", "layer-b").returncode, 0)
        r = self.start()
        self.assertIn("Base: layer-b", r.stderr)
        layer_b = git(self.repo, "rev-parse", "layer-b").strip()
        self.assertEqual(self.bd()["base"]["commit"], layer_b)
        self.assertEqual(self.bd()["rounds"][-1]["added"], 3)
        self.assertEqual(self.base_file()["commit"], layer_b)
        self.assertEqual(self.base_file()["source"], "config")
        self.assertIn("Base: layer-b", self.cli("--status").stdout)

    def test_upstream_parent_is_the_base_without_config(self):
        git(self.repo, "branch", "--set-upstream-to=layer-b")
        self.start()
        self.assertEqual(self.bd()["base"]["source"], "upstream")
        self.assertEqual(self.bd()["rounds"][-1]["added"], 3)

    def test_default_branch_is_the_last_fallback(self):
        self.start()
        self.assertEqual(self.bd()["base"]["source"], "default")
        self.assertEqual(self.bd()["rounds"][-1]["added"], 15)

    def test_restack_onto_a_rewritten_parent_carries(self):
        self.cli("--set-base", "layer-b")
        self.start()
        self.run_chain()
        old_b = git(self.repo, "rev-parse", "layer-b").strip()
        git(self.repo, "checkout", "-q", "layer-b")
        self.commit_file("b2.py", "B2 = 1\n", msg="parent gets a fix")
        git(self.repo, "checkout", "-q", BRANCH)
        git(self.repo, "rebase", "-q", "--onto", "layer-b", old_b)
        self.assertEqual(self.pr_create().returncode, 2)
        self.assert_carried(self.carry(), "rebase")
        new_b = git(self.repo, "rev-parse", "layer-b").strip()
        self.assertEqual(self.bd()["base"]["commit"], new_b)

    def test_restack_after_the_layers_below_squash_merge_carries(self):
        self.cli("--set-base", "layer-b")
        self.start()
        self.run_chain()
        old_b = git(self.repo, "rev-parse", "layer-b").strip()
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--squash", "layer-b")
        git(self.repo, "commit", "-q", "-m", "squash a and b")
        git(self.repo, "checkout", "-q", BRANCH)
        git(self.repo, "rebase", "-q", "--onto", "main", old_b)
        self.assertEqual(self.cli("--set-base", "auto").returncode, 0)
        self.assert_carried(self.carry(), "rebase")
        self.assertEqual(self.bd()["base"]["source"], "default")

    def test_a_change_in_the_top_layer_still_needs_review(self):
        self.cli("--set-base", "layer-b")
        self.start()
        self.run_chain()
        old_b = git(self.repo, "rev-parse", "layer-b").strip()
        git(self.repo, "checkout", "-q", "layer-b")
        self.commit_file("b2.py", "B2 = 1\n", msg="parent gets a fix")
        git(self.repo, "checkout", "-q", BRANCH)
        git(self.repo, "rebase", "-q", "--onto", "layer-b", old_b)
        self.write("c.py", "C = 2\n" * 3)
        git(self.repo, "commit", "-q", "-a", "--amend", "--no-edit")
        self.assertEqual(self.carry().returncode, 1)

    def rewrite_parent_and_restack(self):
        """layer-b is rewritten after review (amended, not appended to), and
        BRANCH is restacked onto it with its own patch unchanged."""
        old_b = git(self.repo, "rev-parse", "layer-b").strip()
        git(self.repo, "checkout", "-q", "layer-b")
        self.write("b.py", "B = 2\n" * 7)
        git(self.repo, "commit", "-q", "-a", "--amend", "--no-edit")
        git(self.repo, "checkout", "-q", BRANCH)
        git(self.repo, "rebase", "-q", "--onto", "layer-b", old_b)
        return old_b

    def test_parent_moved_between_review_and_restack_carries(self):
        # sharpen#37, area 7's restack: re-running --set-base after the parent
        # moved must not make the reviewed patch be taken against the new
        # parent, which reads the parent's own change as the branch's.
        self.cli("--set-base", "layer-b")
        self.start()
        self.run_chain()
        old_b = self.rewrite_parent_and_restack()
        self.assertEqual(self.cli("--set-base", "layer-b").returncode, 0)
        self.assertEqual(self.bd()["base"]["commit"], old_b)
        self.assert_carried(self.carry(), "rebase")
        new_b = git(self.repo, "rev-parse", "layer-b").strip()
        self.assertEqual(self.bd()["base"]["commit"], new_b)

    def test_parent_moved_with_no_recorded_base_carries(self):
        # A cycle armed before 4.17 has no base: the fork point from the
        # parent's reflog stands in for it.
        self.cli("--set-base", "layer-b")
        self.start()
        self.run_chain()
        self.edit_store(lambda data: data[BRANCH].pop("base"))
        self.rewrite_parent_and_restack()
        self.assert_carried(self.carry(), "rebase")

    def test_moved_parent_refusal_names_the_branch_s_own_file(self):
        self.cli("--set-base", "layer-b")
        self.start()
        self.run_chain()
        self.rewrite_parent_and_restack()
        self.write("c.py", "C = 2\n" * 3)
        git(self.repo, "commit", "-q", "-a", "--amend", "--no-edit")
        r = self.carry()
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("b.py", r.stderr)

    def test_parent_rewording_next_to_the_branch_s_hunk_carries(self):
        # Area 3's restack: the layer below rewrote text right around this
        # layer's hunk. Only the context lines differ, so the branch's own
        # change is the same and carries; a merge keeps it on the patch path.
        git(self.repo, "checkout", "-q", "layer-b")
        self.commit_file("shared.py", "".join(f"S{i} = {i}\n" for i in range(9)))
        git(self.repo, "checkout", "-q", BRANCH)
        git(self.repo, "rebase", "-q", "layer-b")
        self.cli("--set-base", "layer-b")
        text = "".join(f"S{i} = {i}\n" for i in range(9)).replace("S4 = 4", "S4 = 40")
        self.commit_file("shared.py", text, msg="top layer edits S4")
        self.start()
        self.run_chain()
        git(self.repo, "checkout", "-q", "layer-b")
        reworded = "".join(f"S{i} = {i}\n" for i in range(9)).replace(
            "S2 = 2", "S2 = 20"
        )
        self.commit_file("shared.py", reworded, msg="parent rewords S2")
        git(self.repo, "checkout", "-q", BRANCH)
        git(self.repo, "merge", "-q", "--no-edit", "layer-b")
        self.assert_carried(self.carry(), "rebase")

    def test_a_base_at_head_never_carries_a_code_change(self):
        self.cli("--set-base", "layer-b")
        self.start()
        self.run_chain()
        head = git(self.repo, "rev-parse", "HEAD").strip()
        self.edit_store(lambda data: data[BRANCH]["base"].update(commit=head))
        self.commit_file("c.py", "import os\nos.system('true')\n", msg="code")
        r = self.carry()
        self.assertEqual(r.returncode, 1)
        self.assertIn("is not below", r.stderr)

    def test_reviewed_base_lets_a_cycle_with_no_recorded_base_carry(self):
        self.cli("--set-base", "layer-b")
        self.start()
        self.run_chain()
        self.edit_store(lambda data: data[BRANCH].pop("base"))
        old_b = self.rewrite_parent_and_restack()
        # No reflog of the old parent: the fork point can't be found.
        git(self.repo, "reflog", "expire", "--expire=now", "--all")
        self.assertEqual(self.carry().returncode, 1)
        self.assert_carried(self.carry("--reviewed-base", old_b), "rebase")

    def test_reviewed_base_below_the_recorded_base_is_refused(self):
        # Folding the lower layers into the branch, then naming main as the
        # reviewed base, would carry lines this chain never reviewed.
        self.cli("--set-base", "layer-b")
        self.start()
        self.run_chain()
        git(self.repo, "reset", "-q", "--soft", "main")
        git(self.repo, "commit", "-q", "-m", "fold the stack")
        self.cli("--set-base", "auto")
        r = self.carry("--reviewed-base", "main")
        self.assertEqual(r.returncode, 1)
        self.assertIn("below", r.stderr)
        self.assertEqual(self.carry("--reviewed-base").returncode, 1)

    def test_set_base_after_gates_keeps_base_json_on_the_earned_base(self):
        self.cli("--set-base", "layer-b")
        self.start()
        self.run_chain()
        earned = self.base_file()["commit"]
        self.assertEqual(self.cli("--set-base", "layer-a").returncode, 0)
        self.assertEqual(self.base_file()["commit"], earned)

    def test_remote_fallback_takes_branch_names_only(self):
        git(self.repo, "remote", "add", "origin", self.repo)
        head = git(self.repo, "rev-parse", "layer-b").strip()
        git(self.repo, "update-ref", "refs/remotes/origin/parent-x", head)
        for ref in ("parent-x~1", "parent-x^{commit}"):
            self.assertEqual(self.cli("--set-base", ref).returncode, 1, ref)

    def test_an_upstream_that_holds_head_is_the_branch_s_own_copy(self):
        # Pushed under another name: `push -u origin HEAD:other-name`.
        git(self.repo, "remote", "add", "origin", self.repo)
        head = git(self.repo, "rev-parse", "HEAD").strip()
        git(self.repo, "update-ref", "refs/remotes/origin/other-name", head)
        git(self.repo, "config", f"branch.{BRANCH}.remote", "origin")
        git(self.repo, "config", f"branch.{BRANCH}.merge", "refs/heads/other-name")
        self.start()
        self.assertEqual(self.bd()["base"]["source"], "default")

    def test_a_renamed_branch_s_old_remote_copy_is_not_its_parent(self):
        # Pushed as feat/old, renamed, then a new commit: the old remote tip
        # must not become the base (auto-init would pick `tiny` from it).
        git(self.repo, "remote", "add", "origin", self.repo)
        head = git(self.repo, "rev-parse", "HEAD").strip()
        git(self.repo, "update-ref", "refs/remotes/origin/old-name", head)
        git(self.repo, "config", f"branch.{BRANCH}.remote", "origin")
        git(self.repo, "config", f"branch.{BRANCH}.merge", "refs/heads/old-name")
        self.commit_file("README.md", "docs\n")
        self.start()
        self.assertEqual(self.bd()["base"]["source"], "default")

    def test_a_refusal_names_the_base_and_points_at_set_base(self):
        self.cli("--set-base", "layer-b")
        self.start()
        self.run_chain()
        self.commit_file("c.py", "C = 9\n" * 3, msg="code change")
        r = self.carry()
        self.assertEqual(r.returncode, 1)
        self.assertIn("measured from layer-b", r.stderr)
        self.assertIn("--set-base <parent>", r.stderr)

    def test_an_unwritable_base_file_is_reported(self):
        # A file where the directory should be blocks the write, even as root.
        sdlc = os.path.join(self.repo, ".sharpen", "sdlc")
        os.makedirs(sdlc, exist_ok=True)
        blocker = os.path.join(sdlc, BRANCH.split("/")[0])
        if os.path.isdir(blocker):
            import shutil

            shutil.rmtree(blocker)
        with open(blocker, "w") as f:
            f.write("not a directory\n")
        r = self.cli("--init", "small-medium")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("warning: could not write", r.stderr)

    def test_reviewed_base_is_only_for_carry(self):
        r = self.cli("--status", "--reviewed-base", "layer-b")
        self.assertEqual(r.returncode, 1)

    def test_set_base_accepts_a_remote_only_parent(self):
        head = git(self.repo, "rev-parse", "layer-b").strip()
        git(self.repo, "remote", "add", "origin", self.repo)
        git(self.repo, "update-ref", "refs/remotes/origin/parent-x", head)
        r = self.cli("--set-base", "parent-x")
        self.assertEqual(r.returncode, 0, r.stderr)
        cfg = git(self.repo, "config", f"branch.{BRANCH}.sdlcBase").strip()
        self.assertEqual(cfg, "origin/parent-x")

    def test_set_base_refuses_refs_that_move_with_head(self):
        for ref in ("HEAD", "HEAD~1", BRANCH):
            r = self.cli("--set-base", ref)
            self.assertEqual(r.returncode, 1, ref)
        self.assertEqual(self.cli("--set-base", "layer-b").returncode, 0)

    def test_bare_set_base_is_an_error_not_an_unset(self):
        self.cli("--set-base", "layer-b")
        self.assertEqual(self.cli("--set-base").returncode, 1)
        self.assertEqual(self.cli("--set-base", "--branch", BRANCH).returncode, 1)
        cfg = git(self.repo, "config", f"branch.{BRANCH}.sdlcBase").strip()
        self.assertEqual(cfg, "layer-b")

    def test_set_base_does_not_combine_with_init(self):
        r = self.cli("--init", "tiny", "--set-base", "layer-b")
        self.assertEqual(r.returncode, 1)

    def test_a_rebase_between_arming_and_the_chain_still_carries(self):
        # The cycle is armed on the old parent, the branch is brought up to
        # date before the chain, and a later clean merge of the parent must
        # still carry: the stored base is older than what was reviewed.
        self.cli("--set-base", "layer-b")
        self.start()
        old_b = git(self.repo, "rev-parse", "layer-b").strip()
        git(self.repo, "checkout", "-q", "layer-b")
        self.commit_file("b2.py", "B2 = 1\n", msg="parent moves")
        git(self.repo, "checkout", "-q", BRANCH)
        git(self.repo, "rebase", "-q", "--onto", "layer-b", old_b)
        self.run_chain()
        git(self.repo, "checkout", "-q", "layer-b")
        self.commit_file("b3.py", "B3 = 1\n", msg="parent moves again")
        git(self.repo, "checkout", "-q", BRANCH)
        git(self.repo, "merge", "-q", "--no-edit", "layer-b")
        self.assert_carried(self.carry(), "rebase")

    def test_set_base_rejects_an_unknown_ref(self):
        self.assertEqual(self.cli("--set-base", "nope").returncode, 1)


class FixReceiptTest(ReceiptTestBase):
    """One /grumpy:fix run earns one fix gate."""

    def setUp(self):
        super().setUp()
        ignore_artifacts(self.repo)
        for step in ("simplify", "review"):
            self.receipt(step)
            self.assertEqual(self.verify(step).returncode, 0)

    def write_stage_receipt(self, stage):
        # grumpy 2.11's writer: `receipt.py fix --stage <stage>`.
        self.report("fix")
        time.sleep(0.01)
        r = self.run_py(
            GRUMPY_RECEIPT, "fix", "--stage", stage, "--worktree", self.repo
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        d = os.path.join(self.repo, ".sharpen", "grumpy", BRANCH, "receipts")
        with open(os.path.join(d, f"fix-{stage}.json")) as f:
            self.assertEqual(json.load(f)["stage"], stage)
        time.sleep(0.01)

    def test_a_stale_fix_report_is_refused_for_the_second_stage(self):
        self.write_stage_receipt("review")
        r = self.run_py(
            GRUMPY_RECEIPT, "fix", "--stage", "imagine", "--worktree", self.repo
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("predates the last receipt", r.stderr)

    def test_one_shared_fix_receipt_cannot_earn_both_fix_gates(self):
        self.receipt("fix")  # first fix: --verify skipped
        self.receipt("imagine")
        self.assertEqual(self.verify("imagine").returncode, 0)
        self.receipt("fix")  # second fix overwrites fix.json
        self.verify("fix")
        self.verify("fix")
        fixes = [g for g in self.gates() if g.startswith("grumpy-fix")]
        self.assertEqual(len(fixes), 1, self.gates())

    def test_a_review_stage_receipt_after_imagine_cannot_earn_gate_six(self):
        # review was re-run after imagine, so fix labels its run `review`:
        # the refreshed fix.json is newer than imagine but names the wrong stage.
        self.write_stage_receipt("review")
        self.assertEqual(self.verify("fix").returncode, 0)
        self.receipt("imagine")
        self.assertEqual(self.verify("imagine").returncode, 0)
        self.write_stage_receipt("review")
        self.assertNotEqual(self.verify("fix").returncode, 0)
        self.assertNotIn("grumpy-fix-post-imagine", self.gates())

    def test_stage_receipts_earn_their_own_gates(self):
        self.write_stage_receipt("review")
        self.assertEqual(self.verify("fix").returncode, 0)
        self.receipt("imagine")
        self.assertEqual(self.verify("imagine").returncode, 0)
        self.write_stage_receipt("imagine")
        self.assertEqual(self.verify("fix").returncode, 0)
        self.assertIn("grumpy-fix-post-imagine", self.gates())


class RoundEdgeTest(ReceiptTestBase):
    def test_hand_edited_entry_does_not_crash_init(self):
        self.edit_store(lambda data: data.update({BRANCH: "garbage"}))
        r = self.cli("--init", "small-medium")
        self.assertNotIn("Traceback", r.stderr)

    def test_new_rounds_starts_over(self):
        ignore_artifacts(self.repo)
        for _ in range(2):
            self.receipt("review")
            self.verify("review")
            self.commit()
            self.cli("--init", "small-medium")
        self.assertEqual(len(self.bd()["rounds"]), 3)
        self.assertEqual(
            self.cli("--init", "small-medium", "--new-rounds").returncode, 0
        )
        self.assertEqual(len(self.bd()["rounds"]), 1)
        self.assertEqual(self.cli("--status", "--new-rounds").returncode, 1)


class RoundCounterTest(ReceiptTestBase):
    """An advisory count of review rounds; it never blocks."""

    def setUp(self):
        super().setUp()
        ignore_artifacts(self.repo)

    def grow(self, n):
        path = os.path.join(self.repo, f"f{n}.py")
        with open(path, "w") as f:
            f.write("x = 1\n" * n)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", f"grow {n}")

    def earn_review(self):
        self.receipt("review")
        self.assertEqual(self.verify("review").returncode, 0)

    def test_first_cycle_is_round_one_and_silent(self):
        self.assertEqual(len(self.bd()["rounds"]), 1)
        self.assertNotIn("Round", self.cli("--status").stdout)

    def test_reinit_before_any_pass_is_the_same_round(self):
        r = self.cli("--init", "small-medium")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(self.bd()["rounds"]), 1)

    def test_reinit_after_a_pass_counts_and_reports_growth(self):
        self.grow(4)
        self.cli("--init", "small-medium")
        self.earn_review()
        self.grow(10)
        r = self.cli("--init", "small-medium")
        self.assertEqual(r.returncode, 0)
        self.assertIn("Round 2", r.stderr)
        first, last = re.search(r"from (\d+) to (\d+) added", r.stderr).groups()
        self.assertEqual(int(last) - int(first), 10)
        self.assertNotIn("splitting", r.stderr)
        self.assertIn("Round 2", self.cli("--status").stdout)

    def test_round_three_advises_and_never_blocks(self):
        for _ in range(2):
            self.earn_review()
            self.commit()
            self.cli("--init", "small-medium")
        r = self.cli("--init", "small-medium")
        self.assertEqual(r.returncode, 0)
        self.assertIn("Round 3", r.stderr)
        self.assertIn("splitting", r.stderr)
        self.run_chain()
        self.assertEqual(self.pr_create().returncode, 0)


if __name__ == "__main__":
    unittest.main()
