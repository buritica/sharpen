#!/usr/bin/env python3
"""diff_base.py: on a three-layer stack the passes diff only the top layer."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.normpath(os.path.join(HERE, "..", "scripts", "diff_base.py"))


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, check=True, text=True
    ).stdout.strip()


class DiffBaseTest(unittest.TestCase):
    def setUp(self):
        self.repo = os.path.realpath(tempfile.mkdtemp())
        self.bin = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.addCleanup(shutil.rmtree, self.bin, True)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        self.commit("base.py")
        for branch, rel in (("layer-a", "a.py"), ("layer-b", "b.py"), ("top", "c.py")):
            git(self.repo, "checkout", "-q", "-b", branch)
            self.commit(rel)

    def commit(self, rel):
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write("x = 1\n")
        git(self.repo, "add", rel)
        git(self.repo, "commit", "-q", "-m", rel)

    def fake_gh(self, base_ref_name):
        """A `gh` on PATH that answers `gh pr view` with an open PR."""
        path = os.path.join(self.bin, "gh")
        reply = json.dumps({"baseRefName": base_ref_name, "state": "OPEN"})
        with open(path, "w") as f:
            f.write(f"#!/bin/sh\necho '{reply}'\n")
        os.chmod(path, 0o755)

    def run_script(self, *args):
        # PATH starts with our own bin: a fake gh when a test wrote one, and
        # otherwise a gh that finds no PR, so a real gh never runs.
        if not os.path.exists(os.path.join(self.bin, "gh")):
            with open(os.path.join(self.bin, "gh"), "w") as f:
                f.write("#!/bin/sh\necho 'no pull requests found' >&2\nexit 1\n")
            os.chmod(os.path.join(self.bin, "gh"), 0o755)
        env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin")
        return subprocess.run(
            [sys.executable, SCRIPT, *args],
            capture_output=True,
            text=True,
            env=env,
        )

    def base(self):
        r = self.run_script("--worktree", self.repo, "--why")
        self.assertEqual(r.returncode, 0, r.stderr)
        ref, why = r.stdout.splitlines()
        return ref, why

    def changed(self, ref):
        return git(self.repo, "diff", "--name-only", f"{ref}...HEAD").split()

    def test_sdlc_base_file_wins(self):
        d = os.path.join(self.repo, ".claude", "sdlc", "top")
        os.makedirs(d)
        with open(os.path.join(d, "base.json"), "w") as f:
            json.dump({"ref": "layer-b", "source": "config"}, f)
        git(self.repo, "config", "branch.top.sdlcBase", "layer-a")
        ref, why = self.base()
        self.assertEqual(ref, "layer-b")
        self.assertIn("base.json", why)
        self.assertEqual(self.changed(ref), ["c.py"])

    def test_config_then_upstream_then_default(self):
        git(self.repo, "config", "branch.top.sdlcBase", "layer-b")
        self.assertEqual(self.base()[0], "layer-b")
        git(self.repo, "config", "--unset", "branch.top.sdlcBase")
        git(self.repo, "branch", "--set-upstream-to=layer-b")
        self.assertEqual(self.base(), ("layer-b", "the branch's upstream"))
        git(self.repo, "branch", "--unset-upstream")
        self.assertEqual(self.base(), ("main", "the default branch"))
        self.assertEqual(sorted(self.changed("main")), ["a.py", "b.py", "c.py"])

    def test_a_stale_base_file_falls_through(self):
        d = os.path.join(self.repo, ".claude", "sdlc", "top")
        os.makedirs(d)
        with open(os.path.join(d, "base.json"), "w") as f:
            json.dump({"ref": "gone-branch"}, f)
        git(self.repo, "config", "branch.top.sdlcBase", "also-gone")
        r = self.run_script("--worktree", self.repo)
        self.assertEqual(r.stdout.strip(), "main")
        self.assertIn("'gone-branch', which doesn't resolve", r.stderr)
        self.assertIn("sdlcBase is 'also-gone'", r.stderr)

    def test_a_failed_gh_call_is_not_silent(self):
        with open(os.path.join(self.bin, "gh"), "w") as f:
            f.write("#!/bin/sh\necho 'HTTP 401: Bad credentials' >&2\nexit 1\n")
        os.chmod(os.path.join(self.bin, "gh"), 0o755)
        r = self.run_script("--worktree", self.repo)
        self.assertEqual(r.stdout.strip(), "main")
        self.assertIn("`gh pr view` failed (HTTP 401", r.stderr)

    def test_no_pr_is_quiet(self):
        r = self.run_script("--worktree", self.repo)
        self.assertEqual((r.stdout.strip(), r.stderr), ("main", ""))

    def test_open_pr_base_beats_the_upstream(self):
        self.fake_gh("layer-a")
        git(self.repo, "branch", "--set-upstream-to=layer-b")
        self.assertEqual(self.base(), ("layer-a", "the open PR's base branch"))

    def test_the_branch_s_own_remote_copy_is_not_a_parent(self):
        head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/top", head)
        git(self.repo, "config", "branch.top.remote", "origin")
        git(self.repo, "config", "branch.top.merge", "refs/heads/top")
        self.assertEqual(self.base(), ("main", "the default branch"))

    def test_detached_head_and_a_missing_worktree_value_fail(self):
        git(self.repo, "checkout", "-q", "--detach")
        self.assertEqual(self.run_script("--worktree", self.repo).returncode, 1)
        self.assertEqual(self.run_script("--worktree").returncode, 2)


if __name__ == "__main__":
    unittest.main()
