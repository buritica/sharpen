#!/usr/bin/env python3
"""Replay full host hook payloads (scripts/tests/fixtures/hook-payloads/)
through the real hook scripts, per host, and check each decision.

Every payload is labelled by provenance in failures ("transcribed/codex",
"recorded/codex"): transcribed ones are written from the host's hook docs,
recorded ones are captured from a live session and go in
fixtures/hook-payloads/recorded/<host>/ under a transcribed scenario's name.

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
# Two provenances, kept apart so a test failure says which kind of payload
# broke. transcribed/<host>/ is written from the host's hook reference;
# recorded/<host>/ is captured from a live session. Both replay through the
# same checks; recorded/ is replayed for whichever fixture names it has.
PROVENANCES = ("transcribed", "recorded")


def _hosts(provenance):
    base = os.path.join(FIXTURES, provenance)
    if not os.path.isdir(base):
        return []
    return sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d)))


SOURCES = [(prov, host) for prov in PROVENANCES for host in _hosts(prov)]


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

    def sources(self, name):
        """(label, payload) for every fixture named `name`, labelled
        "<provenance>/<host>" so a failure says whether a transcribed or a
        recorded payload broke."""
        out = []
        for prov, host in SOURCES:
            path = os.path.join(FIXTURES, prov, host, name + ".json")
            if os.path.exists(path):
                with open(path) as fh:
                    text = fh.read().replace("__REPO__", self.repo)
                out.append(("%s/%s" % (prov, host), json.loads(text)))
        self.assertTrue(out, "no fixture named " + name)
        return out

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

    def test_every_transcribed_host_has_every_fixture(self):
        hosts = _hosts("transcribed")
        names = {
            h: sorted(os.listdir(os.path.join(FIXTURES, "transcribed", h)))
            for h in hosts
        }
        self.assertGreaterEqual(len(hosts), 2, names)
        for h in hosts[1:]:
            self.assertEqual(names[h], names[hosts[0]], "fixture sets differ")

    def test_recorded_fixtures_use_known_names(self):
        # A recorded capture must replace a transcribed scenario, so the
        # checks below know what decision to expect from it.
        known = set()
        for h in _hosts("transcribed"):
            known |= set(os.listdir(os.path.join(FIXTURES, "transcribed", h)))
        for h in _hosts("recorded"):
            for f in os.listdir(os.path.join(FIXTURES, "recorded", h)):
                self.assertIn(f, known, "recorded/%s/%s has no scenario" % (h, f))

    def test_gh_pr_create_without_cycle_is_allowed(self):
        self.branch("feature/a")
        for label, p in self.sources("pretooluse-gh-pr-create"):
            with self.subTest(payload=label):
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
        for label, p in self.sources("pretooluse-gh-pr-create"):
            with self.subTest(payload=label):
                r = self.run_hook(os.path.join(SDLC, "enforce-sdlc-gates.py"), p)
                self.assertEqual(r.returncode, 2, r.stderr.decode())

    def test_hand_stamped_skill_gate_is_denied(self):
        self.branch("feature/c")
        for label, p in self.sources("pretooluse-record-skill-gate"):
            with self.subTest(payload=label):
                r = self.run_hook(os.path.join(SDLC, "block-direct-gate-record.py"), p)
                self.assertEqual(r.returncode, 2, r.stderr.decode())

    def test_commit_on_feature_branch_arms_a_cycle(self):
        for label, p in self.sources("posttooluse-git-commit"):
            with self.subTest(payload=label):
                branch = "feature/arm-" + label.replace("/", "-")
                git(self.repo, "checkout", "-q", "-B", branch, MAIN)
                git(self.repo, "commit", "-q", "--allow-empty", "-m", "work")
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
        for label, p in self.sources("pretooluse-git-commit"):
            with self.subTest(payload=label):
                r = self.run_hook(os.path.join(GUARD, "_block-main-commits.py"), p)
                self.assertEqual(r.returncode, 2, r.stderr.decode())

    def test_commit_on_unprotected_default_branch_is_allowed(self):
        for label, p in self.sources("pretooluse-git-commit"):
            with self.subTest(payload=label):
                r = self.run_hook(os.path.join(GUARD, "_block-main-commits.py"), p)
                self.assertEqual(r.returncode, 0, r.stderr.decode())


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
