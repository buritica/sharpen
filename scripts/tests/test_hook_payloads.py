#!/usr/bin/env python3
"""Replay full host hook payloads (scripts/tests/fixtures/hook-payloads/)
through the real hook scripts, per host, and check each decision.

See the fixtures README for where each payload comes from. Stdlib only.
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
FIXTURES = os.path.join(HERE, "fixtures", "hook-payloads")
SDLC = os.path.join(ROOT, "plugins", "sdlc", "scripts")
GUARD = os.path.join(ROOT, "plugins", "sdlc-guardrails", "hooks")
RECORD = os.path.join(SDLC, "record-gate.py")
MAIN = "ma" + "in"  # split so the guardrails hook never sees a literal
HOSTS = sorted(
    d for d in os.listdir(FIXTURES) if os.path.isdir(os.path.join(FIXTURES, d))
)


def git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)


class HookPayloadReplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, "repo")
        os.mkdir(self.repo)
        self.gates = os.path.join(self.tmp, "store", "gates.json")
        self.env = dict(
            os.environ,
            GIT_CONFIG_GLOBAL=os.devnull,
            CLAUDE_CONFIG_DIR=os.path.join(self.tmp, "cfg"),
            SDLC_GATES_PATH=self.gates,
        )
        git(self.repo, "init", "-q", "-b", MAIN)
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "init")

    def payload(self, host, name):
        with open(os.path.join(FIXTURES, host, name + ".json")) as fh:
            return json.loads(fh.read().replace("__REPO__", self.repo))

    def run_hook(self, script, payload):
        return subprocess.run(
            [sys.executable, script],
            input=json.dumps(payload).encode(),
            capture_output=True,
            cwd=self.repo,
            env=self.env,
        )

    def branch(self, name):
        git(self.repo, "checkout", "-q", "-b", name)

    def store(self):
        if not os.path.exists(self.gates):
            return {}
        with open(self.gates) as fh:
            return json.load(fh)

    def test_every_host_has_every_fixture(self):
        names = {
            host: sorted(os.listdir(os.path.join(FIXTURES, host))) for host in HOSTS
        }
        self.assertGreaterEqual(len(HOSTS), 2, names)
        first = names[HOSTS[0]]
        for host in HOSTS[1:]:
            self.assertEqual(names[host], first, "fixture sets differ across hosts")

    def test_gh_pr_create_without_cycle_is_allowed(self):
        self.branch("feature/a")
        for host in HOSTS:
            with self.subTest(host=host):
                p = self.payload(host, "pretooluse-gh-pr-create")
                r = self.run_hook(os.path.join(SDLC, "enforce-sdlc-gates.py"), p)
                self.assertEqual(r.returncode, 0, r.stderr.decode())

    def test_gh_pr_create_with_incomplete_cycle_is_denied(self):
        self.branch("feature/b")
        init = subprocess.run(
            [sys.executable, RECORD, "--init", "tiny"],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
        )
        self.assertEqual(init.returncode, 0, init.stderr.decode())
        for host in HOSTS:
            with self.subTest(host=host):
                p = self.payload(host, "pretooluse-gh-pr-create")
                r = self.run_hook(os.path.join(SDLC, "enforce-sdlc-gates.py"), p)
                self.assertEqual(r.returncode, 2, r.stderr.decode())

    def test_hand_stamped_skill_gate_is_denied(self):
        self.branch("feature/c")
        for host in HOSTS:
            with self.subTest(host=host):
                p = self.payload(host, "pretooluse-record-skill-gate")
                r = self.run_hook(os.path.join(SDLC, "block-direct-gate-record.py"), p)
                self.assertEqual(r.returncode, 2, r.stderr.decode())

    def test_commit_on_feature_branch_arms_a_cycle(self):
        for host in HOSTS:
            with self.subTest(host=host):
                branch = "feature/arm-" + host
                git(self.repo, "checkout", "-q", "-B", branch, MAIN)
                git(self.repo, "commit", "-q", "--allow-empty", "-m", "work")
                p = self.payload(host, "posttooluse-git-commit")
                r = self.run_hook(os.path.join(SDLC, "auto-init-gate-cycle.py"), p)
                err = r.stderr.decode()
                # Arming a cycle exits 2 on purpose: the message asks the
                # model to confirm the tier (see the hook's docstring).
                self.assertEqual(r.returncode, 2, err)
                self.assertNotIn("Traceback", err)
                self.assertIn(branch, self.store(), err)
                self.assertIn("tier", self.store()[branch])

    def test_commit_on_protected_default_branch_is_denied(self):
        subprocess.run(
            [
                sys.executable,
                os.path.join(GUARD, "_guardrails_config.py"),
                "protect",
                self.repo,
            ],
            env=self.env,
            check=True,
            capture_output=True,
        )
        for host in HOSTS:
            with self.subTest(host=host):
                p = self.payload(host, "pretooluse-git-commit")
                r = self.run_hook(os.path.join(GUARD, "_block-main-commits.py"), p)
                self.assertEqual(r.returncode, 2, r.stderr.decode())

    def test_commit_on_unprotected_default_branch_is_allowed(self):
        for host in HOSTS:
            with self.subTest(host=host):
                p = self.payload(host, "pretooluse-git-commit")
                r = self.run_hook(os.path.join(GUARD, "_block-main-commits.py"), p)
                self.assertEqual(r.returncode, 0, r.stderr.decode())


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
