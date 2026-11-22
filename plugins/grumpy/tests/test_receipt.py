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

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.normpath(os.path.join(HERE, "..", "scripts", "receipt.py"))
BRANCH = "feature/x"


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, check=True, text=True
    ).stdout.strip()


class ReceiptTest(unittest.TestCase):
    def setUp(self):
        self.repo = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write(".sharpen/grumpy/\n")
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


if __name__ == "__main__":
    unittest.main()
