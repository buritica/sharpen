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

from test_receipts import BRANCH, ReceiptTestBase, git, ignore_artifacts


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
        path = os.path.join(self.repo, ".claude", "sdlc", BRANCH, "base.json")
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
        self.receipt("fix")
        d = os.path.join(self.repo, ".claude", "grumpy", BRANCH, "receipts")
        with open(os.path.join(d, "fix.json")) as f:
            receipt = json.load(f)
        receipt["stage"] = stage
        with open(os.path.join(d, f"fix-{stage}.json"), "w") as f:
            json.dump(receipt, f)
        os.unlink(os.path.join(d, "fix.json"))

    def test_one_shared_fix_receipt_cannot_earn_both_fix_gates(self):
        self.receipt("fix")  # first fix: --verify skipped
        self.receipt("imagine")
        self.assertEqual(self.verify("imagine").returncode, 0)
        self.receipt("fix")  # second fix overwrites fix.json
        self.verify("fix")
        self.verify("fix")
        fixes = [g for g in self.gates() if g.startswith("grumpy-fix")]
        self.assertEqual(len(fixes), 1, self.gates())

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
