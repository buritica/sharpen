#!/usr/bin/env python3
"""Tests for scripts/pre-merge-check.sh, the repo-local PreToolUse hook that
runs check-marketplace.py before `gh pr create` / `gh pr merge`.

Runs a copy of the hook in throwaway repos with stub check-marketplace.py
scripts, so the result never depends on this checkout's state. Stdlib only.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
HOOK = os.path.join(ROOT, "scripts", "pre-merge-check.sh")


def make_repo(parent, name, check_exit, marker):
    """A git repo holding a copy of the hook and a stub marketplace check that
    prints `marker` and exits `check_exit`."""
    repo = os.path.join(parent, name)
    os.makedirs(os.path.join(repo, "scripts"))
    subprocess.run(["git", "init", "-q", repo], check=True, capture_output=True)
    shutil.copy2(HOOK, os.path.join(repo, "scripts", "pre-merge-check.sh"))
    with open(os.path.join(repo, "scripts", "check-marketplace.py"), "w") as f:
        f.write("print(%r)\nraise SystemExit(%d)\n" % (marker, check_exit))
    return repo


class PreMergeCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.main = make_repo(self.tmp, "main", 0, "MAIN-OK")
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull)

    def run_hook(self, command, cwd=None, tool="Bash"):
        payload = {"tool_name": tool, "tool_input": {"command": command}}
        if cwd is not None:
            payload["cwd"] = cwd
        return subprocess.run(
            ["bash", os.path.join(self.main, "scripts", "pre-merge-check.sh")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=self.main,
            env=self.env,
            timeout=30,
        )

    def test_unrelated_command_is_not_checked(self):
        make_repo(self.tmp, "broken", 1, "BROKEN")
        r = self.run_hook("ls -la", cwd=os.path.join(self.tmp, "broken"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_non_bash_tool_is_not_checked(self):
        r = self.run_hook("gh pr create", tool="Read")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_passing_check_allows_pr_create(self):
        r = self.run_hook("gh pr create --fill", cwd=self.main)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_failing_check_in_the_payload_cwd_denies(self):
        # The session runs this hook from the main checkout, but the PR is
        # opened from a worktree: the worktree's tree is the one checked.
        wt = make_repo(self.tmp, "worktree", 1, "WORKTREE-BROKEN")
        r = self.run_hook("gh pr merge 5 --squash", cwd=wt)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("WORKTREE-BROKEN", r.stderr)
        deny = json.loads(r.stderr)
        self.assertEqual(deny["decision"], "deny")
        self.assertIn(os.path.realpath(wt), deny["reason"])

    def break_main(self):
        with open(os.path.join(self.main, "scripts", "check-marketplace.py"), "w") as f:
            f.write("print('MAIN-BROKEN')\nraise SystemExit(1)\n")

    def test_missing_cwd_falls_back_to_the_hooks_checkout(self):
        self.break_main()
        r = self.run_hook("gh pr create")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("MAIN-BROKEN", r.stderr)

    def test_cwd_outside_any_sharpen_checkout_falls_back(self):
        # A repo with no scripts/check-marketplace.py is not a sharpen tree.
        self.break_main()
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        subprocess.run(["git", "init", "-q", other], check=True, capture_output=True)
        r = self.run_hook("gh pr create", cwd=other)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("MAIN-BROKEN", r.stderr)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
