#!/usr/bin/env python3
"""receipt.py: per-stage fix receipts and their argument checks."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.normpath(os.path.join(HERE, "..", "scripts", "receipt.py"))
BRANCH = "feature/x"


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, check=True, text=True
    ).stdout.strip()


class ReceiptBase(unittest.TestCase):
    def setUp(self):
        self.repo = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write(".sharpen/grumpy/\n.claude/grumpy/\n")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "checkout", "-q", "-b", BRANCH)
        self.dir = os.path.join(self.repo, ".sharpen", "grumpy", BRANCH)
        os.makedirs(self.dir)

    def report(self):
        time.sleep(0.01)
        with open(os.path.join(self.dir, "fix.md"), "w") as f:
            f.write(f"# fix {time.time()}\n")
        time.sleep(0.01)

    def run_receipt(self, *args):
        return subprocess.run(
            [sys.executable, SCRIPT, *args, "--worktree", self.repo],
            capture_output=True,
            text=True,
        )

    def receipt(self, name):
        with open(os.path.join(self.dir, "receipts", f"{name}.json")) as f:
            return json.load(f)


class ReceiptTest(ReceiptBase):
    def test_stage_writes_its_own_file_and_refreshes_fix_json(self):
        self.report()
        r = self.run_receipt("fix", "--stage", "review")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.receipt("fix-review")["stage"], "review")
        self.assertEqual(self.receipt("fix")["stage"], "review")

    def test_a_second_stage_refreshes_fix_json(self):
        self.report()
        self.assertEqual(self.run_receipt("fix", "--stage", "review").returncode, 0)
        self.report()
        self.assertEqual(self.run_receipt("fix", "--stage", "imagine").returncode, 0)
        self.assertEqual(self.receipt("fix")["stage"], "imagine")
        self.assertEqual(self.receipt("fix-review")["stage"], "review")

    def test_bad_stage_arguments_are_refused(self):
        self.report()
        for args in (
            ("review", "--stage", "review"),
            ("fix", "--stage", "simplify"),
        ):
            self.assertEqual(self.run_receipt(*args).returncode, 1, args)
        r = subprocess.run(
            [sys.executable, SCRIPT, "fix", "--stage"], capture_output=True, text=True
        )
        self.assertEqual(r.returncode, 1)

    def test_plain_fix_after_a_stage_receipt_needs_a_fresh_report(self):
        self.report()
        self.assertEqual(self.run_receipt("fix", "--stage", "review").returncode, 0)
        r = self.run_receipt("fix")
        self.assertEqual(r.returncode, 1)
        self.assertIn("predates the last receipt (fix", r.stderr)

    def test_a_stageless_fix_removes_stale_stage_receipts(self):
        # Left in place, sdlc 4.17+ would read fix-review.json (it prefers a
        # stage file) and refuse it as older than the latest review.
        self.report()
        self.assertEqual(self.run_receipt("fix", "--stage", "review").returncode, 0)
        self.report()
        r = self.run_receipt("fix")
        self.assertEqual(r.returncode, 0, r.stderr)
        receipts = os.path.join(self.dir, "receipts")
        self.assertFalse(os.path.exists(os.path.join(receipts, "fix-review.json")))
        self.assertNotIn("stage", self.receipt("fix"))

    def test_a_failed_fix_json_copy_keeps_the_stage_receipt(self):
        os.makedirs(os.path.join(self.dir, "receipts", "fix.json"))
        self.report()
        r = self.run_receipt("fix", "--stage", "imagine")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("sdlc 4.17+ records from it", r.stderr)
        self.assertEqual(self.receipt("fix-imagine")["stage"], "imagine")


class AutoStageTest(ReceiptBase):
    """`fix --stage auto`: the stage of the pass still owed a fix, imagine
    first when both are (the order sdlc's determine_gate fills them in)."""

    def pass_receipt(self, name):
        time.sleep(0.01)
        with open(os.path.join(self.dir, f"{name}.md"), "w") as f:
            f.write(f"# {name} {time.time()}\n")
        r = self.run_receipt(name)
        self.assertEqual(r.returncode, 0, r.stderr)

    def auto_fix(self):
        self.report()
        r = self.run_receipt("fix", "--stage", "auto")
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.receipt("fix")["stage"]

    def test_the_agent_chain_order_gets_one_stage_each(self):
        self.pass_receipt("review")
        self.assertEqual(self.auto_fix(), "review")
        self.pass_receipt("imagine")
        self.assertEqual(self.auto_fix(), "imagine")

    def test_review_and_imagine_before_any_fix(self):
        # The adapter's order, both gates recorded before the fix: sdlc opens
        # post-imagine first, so imagine first, then review, no flag by hand.
        self.pass_receipt("review")
        self.pass_receipt("imagine")
        self.assertEqual(self.auto_fix(), "imagine")
        self.assertEqual(self.auto_fix(), "review")

    def test_a_new_round_of_both_passes_starts_at_imagine(self):
        self.pass_receipt("review")
        self.auto_fix()
        self.pass_receipt("imagine")
        self.auto_fix()
        self.pass_receipt("review")
        self.pass_receipt("imagine")
        self.assertEqual(self.auto_fix(), "imagine")
        self.assertEqual(self.auto_fix(), "review")

    def test_a_new_review_alone_is_the_review_stage(self):
        self.pass_receipt("review")
        self.auto_fix()
        self.pass_receipt("imagine")
        self.auto_fix()
        self.pass_receipt("review")
        self.assertEqual(self.auto_fix(), "review")

    def test_a_second_fix_after_imagine_stays_on_imagine(self):
        self.pass_receipt("review")
        self.auto_fix()
        self.pass_receipt("imagine")
        self.auto_fix()
        self.assertEqual(self.auto_fix(), "imagine")

    def test_a_repeated_fix_before_imagine_stays_on_review(self):
        self.pass_receipt("review")
        self.auto_fix()
        self.pass_receipt("imagine")
        self.auto_fix()
        self.pass_receipt("review")
        self.auto_fix()
        self.assertEqual(self.auto_fix(), "review")

    def test_imagine_alone_is_the_imagine_stage(self):
        self.pass_receipt("imagine")
        self.assertEqual(self.auto_fix(), "imagine")

    def test_no_review_receipt_says_so_and_writes_review(self):
        self.report()
        r = self.run_receipt("fix", "--stage", "auto")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no review or imagine receipt", r.stderr)
        self.assertEqual(self.receipt("fix")["stage"], "review")

    def test_a_legacy_fix_receipt_counts(self):
        # A fix an older grumpy stamped under .claude/ settles imagine.
        self.pass_receipt("imagine")
        time.sleep(0.01)
        legacy = os.path.join(self.repo, ".claude", "grumpy", BRANCH, "receipts")
        os.makedirs(legacy)
        with open(os.path.join(legacy, "fix-imagine.json"), "w") as f:
            json.dump({"written_at": datetime.now(timezone.utc).isoformat()}, f)
        self.pass_receipt("review")
        self.assertEqual(self.auto_fix(), "review")

    def test_a_newer_legacy_receipt_wins_over_the_sharpen_copy(self):
        self.pass_receipt("imagine")
        self.assertEqual(self.auto_fix(), "imagine")
        self.pass_receipt("review")
        # The .sharpen imagine receipt predates that fix; a legacy copy of it
        # under .claude/ is newer, so imagine is owed again.
        with open(os.path.join(self.dir, "receipts", "imagine.json"), "w") as f:
            json.dump({"written_at": "2000-01-01T00:00:00+00:00"}, f)
        legacy = os.path.join(self.repo, ".claude", "grumpy", BRANCH, "receipts")
        os.makedirs(legacy)
        with open(os.path.join(legacy, "imagine.json"), "w") as f:
            json.dump({"written_at": datetime.now(timezone.utc).isoformat()}, f)
        self.assertEqual(self.auto_fix(), "imagine")

    def test_an_unreadable_receipt_falls_back_to_the_mtime(self):
        self.pass_receipt("review")
        with open(os.path.join(self.dir, "receipts", "review.json"), "w") as f:
            f.write("{not json")
        self.report()
        r = self.run_receipt("fix", "--stage", "auto")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no readable written_at", r.stderr)
        self.assertEqual(self.receipt("fix")["stage"], "review")

    def test_auto_is_only_for_fix(self):
        self.pass_receipt("review")
        with open(os.path.join(self.dir, "imagine.md"), "w") as f:
            f.write("# imagine\n")
        self.assertEqual(self.run_receipt("imagine", "--stage", "auto").returncode, 1)


if __name__ == "__main__":
    unittest.main()
