#!/usr/bin/env python3
"""Contract tests for host-neutral automatic skill-gate completion."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)

import gate_store as gs  # noqa: E402
import skill_gate_completion as completion  # noqa: E402


def git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], capture_output=True, check=True)


def make_repo(branch="feat/x"):
    repo = tempfile.mkdtemp()
    git(repo, "init", "-q", "-b", branch)
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    return repo


class SkillGateCompletionContractTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.store = os.path.join(self.repo, ".claude", "data", "gates.json")
        os.environ["SDLC_GATES_PATH"] = self.store
        gs.update_store(
            self.store, lambda data: gs.init_gates(data, "feat/x", "small-medium")
        )

    def read_store(self):
        with open(self.store) as f:
            return json.load(f)

    def complete(self, skill, succeeded=True, **kwargs):
        return completion.record_skill_completion(
            skill=skill,
            cwd=self.repo,
            succeeded=succeeded,
            host="test-host",
            host_version="1.0",
            invocation_id="test-1",
            **kwargs,
        )

    def test_successful_completion_records_the_gate(self):
        result = self.complete("grumpy:review")
        self.assertTrue(result["recorded"])
        self.assertEqual(result["gate"], "grumpy-review")
        self.assertEqual(result["host"], "test-host")
        self.assertIn("grumpy-review", self.read_store()["feat/x"]["gates"])

    def test_failed_or_unknown_completion_does_not_write(self):
        before = self.read_store()
        failed = self.complete("grumpy:review", succeeded=False)
        unknown = self.complete("unknown:skill")
        self.assertFalse(failed["recorded"])
        self.assertFalse(unknown["recorded"])
        self.assertEqual(before, self.read_store())

    def test_mutating_completion_invalidates_bash_gates(self):
        for gate in ("tests", "lint", "typecheck"):
            gs.update_store(
                self.store, lambda data, gate=gate: gs.record_gate(data, "feat/x", gate)
            )
        result = self.complete("grumpy:simplify")
        self.assertEqual(set(result["invalidated"]), {"tests", "lint", "typecheck"})
        gates = self.read_store()["feat/x"]["gates"]
        self.assertNotIn("tests", gates)
        self.assertNotIn("lint", gates)
        self.assertNotIn("typecheck", gates)

    def test_requires_normalized_host_and_cwd(self):
        with self.assertRaises(ValueError):
            completion.record_skill_completion(
                skill="grumpy:review", cwd="", succeeded=True, host=""
            )
        with self.assertRaises(ValueError):
            completion.record_skill_completion(
                skill="grumpy:review",
                cwd="relative/worktree",
                succeeded=True,
                host="test-host",
            )


if __name__ == "__main__":
    unittest.main()
