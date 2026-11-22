#!/usr/bin/env python3
"""diff_base.py: on a three-layer stack the passes diff only the top layer."""

import json
import os
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

    def base(self):
        env = dict(os.environ, PATH="/usr/bin:/bin")  # no gh: offline and stable
        r = subprocess.run(
            [sys.executable, SCRIPT, "--worktree", self.repo, "--why"],
            capture_output=True,
            text=True,
            env=env,
        )
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
        self.assertEqual(self.base()[0], "main")


if __name__ == "__main__":
    unittest.main()
