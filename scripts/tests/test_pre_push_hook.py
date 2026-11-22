#!/usr/bin/env python3
"""Tests for this repo's own .githooks/pre-push, run in a throwaway repo with
a stub check-marketplace.py so they don't depend on this checkout's state.

The hook chains to the sdlc plugin's own pre-push.py through
`git config sharpen.sdlcScripts`, which may not be set (no sdlc version that
ships pre-push.py installed). These pin that its absence never fails a push,
and that the config key and file name still match what the plugin's
install-pre-push.py records. Stdlib only.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
HOOK = os.path.join(ROOT, ".githooks", "pre-push")
INSTALLER = os.path.join(ROOT, "plugins", "sdlc", "scripts", "install-pre-push.py")
REFS = "refs/heads/x 1111111 refs/heads/x 0000000\n"


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", repo, *args], check=True, capture_output=True, text=True
    ).stdout.strip()


class PrePushHook(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull)
        git(self.repo, "init", "-q")
        os.makedirs(os.path.join(self.repo, ".githooks"))
        os.makedirs(os.path.join(self.repo, "scripts"))
        self.hook = os.path.join(self.repo, ".githooks", "pre-push")
        shutil.copy2(HOOK, self.hook)
        self.marketplace(0)
        git(self.repo, "config", "core.hooksPath", ".githooks")

    def marketplace(self, code):
        with open(os.path.join(self.repo, "scripts", "check-marketplace.py"), "w") as f:
            f.write("import sys\nsys.exit(%d)\n" % code)

    def product(self, code):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with open(os.path.join(d, "pre-push.py"), "w") as f:
            f.write(
                "import sys\n"
                "data = sys.stdin.read()\n"
                "print('product', ' '.join(sys.argv[1:]), data.split()[0])\n"
                "sys.exit(%d)\n" % code
            )
        git(self.repo, "config", "sharpen.sdlcScripts", d)
        return d

    def chained(self, body):
        path = os.path.join(self.repo, ".git", "hooks", "pre-push")
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, 0o755)
        return path

    def push(self):
        return subprocess.run(
            [self.hook, "origin", "git@example:repo.git"],
            input=REFS,
            capture_output=True,
            text=True,
            cwd=self.repo,
            env=self.env,
            timeout=30,
        )

    def test_no_product_hook_configured_passes_quietly(self):
        # No sdlc pre-push installed: sharpen.sdlcScripts is never set.
        r = self.push()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")

    def test_configured_but_missing_product_hook_passes_with_one_line(self):
        git(self.repo, "config", "sharpen.sdlcScripts", "/nonexistent/sdlc/scripts")
        r = self.push()
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertIn("skipped", lines[0])

    def test_marketplace_failure_blocks(self):
        self.marketplace(1)
        r = self.push()
        self.assertEqual(r.returncode, 1)
        self.assertIn("marketplace check failed", r.stderr)

    def test_product_hook_gets_args_and_refs_and_its_exit_code_counts(self):
        self.product(0)
        r = self.push()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("product origin git@example:repo.git refs/heads/x", r.stdout)
        self.product(1)
        self.assertEqual(self.push().returncode, 1)

    def test_chained_hook_that_ignores_stdin_does_not_fail_the_push(self):
        self.chained("#!/bin/sh\nexit 0\n")
        for _ in range(3):
            r = self.push()
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_chained_hook_failure_blocks(self):
        self.chained("#!/bin/sh\ncat >/dev/null\nexit 3\n")
        self.assertEqual(self.push().returncode, 3)

    def test_symlink_back_to_itself_does_not_loop(self):
        os.symlink(self.hook, os.path.join(self.repo, ".git", "hooks", "pre-push"))
        r = self.push()  # timeout=30 turns a loop into an error
        self.assertEqual(r.returncode, 0, r.stderr)


class ChainContract(unittest.TestCase):
    """The hook finds the sdlc pre-push only through a config key and a file
    name. If the plugin's installer renames either, the chain silently never
    fires; this goes red instead. Vacuous until install-pre-push.py exists in
    this checkout."""

    def test_installer_records_the_key_and_file_the_hook_reads(self):
        with open(HOOK) as f:
            hook = f.read()
        self.assertIn("sharpen.sdlcScripts", hook)
        self.assertIn('"$dir/pre-push.py"', hook)
        if not os.path.exists(INSTALLER):
            return
        with open(INSTALLER) as f:
            installer = f.read()
        self.assertIn("sharpen.sdlcScripts", installer)
        scripts = os.path.dirname(INSTALLER)
        self.assertTrue(os.path.isfile(os.path.join(scripts, "pre-push.py")))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
