#!/usr/bin/env python3
"""Characterization tests for the pre-profile SDLC gate baseline.

These tests freeze the compatibility contract that the portable-core profile
work must preserve. They intentionally describe current behavior; they do not
introduce or imply a new profile resolver.
"""

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

RECORD = os.path.join(SCRIPTS, "record-gate.py")
ENFORCE = os.path.join(SCRIPTS, "enforce-sdlc-gates.py")


def git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], capture_output=True, check=True)


def make_repo(branch="feat/x"):
    repo = tempfile.mkdtemp()
    git(repo, "init", "-q", "-b", branch)
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    return repo


def run_cli(args, repo, gates_path):
    env = dict(os.environ, SDLC_GATES_PATH=gates_path)
    return subprocess.run(
        ["python3", RECORD, *args], capture_output=True, cwd=repo, env=env
    )


def run_enforce(repo, gates_path, command="gh pr create --fill"):
    env = dict(os.environ, SDLC_GATES_PATH=gates_path)
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    return subprocess.run(
        ["python3", ENFORCE],
        input=json.dumps(payload).encode(),
        capture_output=True,
        cwd=repo,
        env=env,
    )


class HistoricalGateSemanticsTest(unittest.TestCase):
    def test_tier_names_and_required_gates_remain_exact(self):
        self.assertEqual(gs.TIERS, ("tiny", "small-medium", "significant"))
        self.assertEqual(
            gs.GATES_BY_TIER,
            {
                "tiny": ["tests", "lint", "typecheck"],
                "small-medium": [
                    "tests",
                    "simplify",
                    "grumpy-review",
                    "grumpy-fix-post-review",
                    "grumpy-imagine",
                    "grumpy-fix-post-imagine",
                    "lint",
                    "typecheck",
                ],
                "significant": [
                    "tests",
                    "simplify",
                    "grumpy-review",
                    "grumpy-fix-post-review",
                    "grumpy-imagine",
                    "grumpy-fix-post-imagine",
                    "lint",
                    "typecheck",
                ],
            },
        )

    def test_protected_skill_gate_set_remains_exact(self):
        self.assertEqual(
            gs.SKILL_FOR_GATE,
            {
                "simplify": "/grumpy:simplify",
                "grumpy-review": "/grumpy:review",
                "grumpy-fix-post-review": "/grumpy:fix",
                "grumpy-imagine": "/grumpy:imagine",
                "grumpy-fix-post-imagine": "/grumpy:fix",
            },
        )
        self.assertEqual(gs.BASH_GATES, ["tests", "lint", "typecheck"])
        self.assertEqual(
            gs.SKILL_TO_GATE,
            {
                "grumpy:simplify": "simplify",
                "grumpy:review": "grumpy-review",
                "grumpy:imagine": "grumpy-imagine",
                "grumpy:fix": "grumpy-fix",
            },
        )
        self.assertEqual(gs.RENAMED_SKILLS, {"simplify": "grumpy:simplify"})

    def test_direct_recording_requires_authorization_only_for_skill_gates(self):
        data = {}
        gs.init_gates(data, "feat/x", "small-medium")
        for gate in ("tests", "lint", "typecheck"):
            gs.record_gate(data, "feat/x", gate)
        for gate in gs.SKILL_FOR_GATE:
            with self.subTest(gate=gate), self.assertRaises(ValueError):
                gs.record_gate(data, "feat/x", gate)
            gs.record_gate(data, "feat/x", gate, authorized=True)
        self.assertEqual(gs.missing_gates(data["feat/x"]), [])


class LegacyStoreCompatibilityTest(unittest.TestCase):
    def test_legacy_cycle_without_profile_still_loads_and_enforces(self):
        # Pre-profile historical shape: no `profile` key, and routes/tier_reason
        # are optional. A future profile-aware reader must continue to accept it.
        legacy_entry = {
            "tier": "small-medium",
            "created_at": "2026-01-01T00:00:00+00:00",
            "gates": {
                "tests": "2026-01-01T00:00:01+00:00",
                "simplify": "2026-01-01T00:00:02+00:00",
                "grumpy-review": "2026-01-01T00:00:03+00:00",
                "grumpy-fix-post-review": "2026-01-01T00:00:04+00:00",
                "grumpy-imagine": "2026-01-01T00:00:05+00:00",
                "grumpy-fix-post-imagine": "2026-01-01T00:00:06+00:00",
                "lint": "2026-01-01T00:00:07+00:00",
                "typecheck": "2026-01-01T00:00:08+00:00",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            store_path = os.path.join(tmp, "gates.json")
            with open(store_path, "w", encoding="utf-8") as f:
                json.dump({"feat/x": legacy_entry}, f)

            loaded = gs.load_store(store_path)
            self.assertNotIn("profile", loaded["feat/x"])
            self.assertEqual(
                gs.required_gates(loaded["feat/x"]), gs.GATES_BY_TIER["small-medium"]
            )
            self.assertEqual(
                gs.completed_gates(loaded["feat/x"]), gs.GATES_BY_TIER["small-medium"]
            )
            self.assertEqual(gs.missing_gates(loaded["feat/x"]), [])

            repo = make_repo()
            result = run_enforce(repo, store_path)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(result.stdout.decode(), "")


class ProfileInitCliTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.gates_path = os.path.join(self.repo, ".claude", "data", "gates.json")

    def write_manifest(self, capabilities, name="manifest.json"):
        path = os.path.join(self.repo, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "protocol_version": "1",
                    "provider": {"name": "test-host"},
                    "capabilities": capabilities,
                    "x-source": "test",
                },
                f,
            )
        return path

    def read_cycle(self):
        with open(self.gates_path, encoding="utf-8") as f:
            return json.load(f)["feat/x"]

    def test_legacy_init_state_shape_still_has_no_profile_metadata(self):
        result = run_cli(["--init", "tiny"], self.repo, self.gates_path)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        cycle = self.read_cycle()
        self.assertEqual(cycle["tier"], "tiny")
        self.assertNotIn("profile", cycle)
        self.assertNotIn("capabilities", cycle)

    def test_profile_without_capabilities_file_fails_without_writing(self):
        result = run_cli(
            ["--init", "tiny", "--profile", "review"], self.repo, self.gates_path
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("--profile requires --capabilities-file", result.stderr.decode())
        self.assertFalse(os.path.exists(self.gates_path))

    def test_valid_manifest_with_explicit_review_writes_sorted_snapshot(self):
        manifest = self.write_manifest(
            ["typecheck", "fix", "review", "lint", "imagine", "test"]
        )
        result = run_cli(
            [
                "--init",
                "small-medium",
                "--profile",
                "review",
                "--capabilities-file",
                manifest,
            ],
            self.repo,
            self.gates_path,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        cycle = self.read_cycle()
        self.assertEqual(cycle["profile"], "review")
        self.assertEqual(
            cycle["capabilities"],
            ["fix", "imagine", "lint", "review", "test", "typecheck"],
        )

    def test_manifest_only_init_selects_highest_complete_profile(self):
        manifest = self.write_manifest(
            ["test", "lint", "typecheck", "review", "imagine", "fix"]
        )
        result = run_cli(
            ["--init", "tiny", "--capabilities-file", manifest],
            self.repo,
            self.gates_path,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(self.read_cycle()["profile"], "adversarial")

    def test_unavailable_request_does_not_write_or_reset_existing_cycle(self):
        initial = run_cli(["--init", "tiny"], self.repo, self.gates_path)
        self.assertEqual(initial.returncode, 0, initial.stderr.decode())
        record = run_cli(["--record", "tests"], self.repo, self.gates_path)
        self.assertEqual(record.returncode, 0, record.stderr.decode())
        before = self.read_cycle()

        manifest = self.write_manifest(["test", "lint", "typecheck"])
        result = run_cli(
            [
                "--init",
                "small-medium",
                "--profile",
                "adversarial",
                "--capabilities-file",
                manifest,
            ],
            self.repo,
            self.gates_path,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing: review, imagine, fix", result.stderr.decode())
        self.assertEqual(self.read_cycle(), before)

    def test_legacy_status_unchanged_and_profiled_status_includes_profile(self):
        legacy = run_cli(["--init", "tiny"], self.repo, self.gates_path)
        self.assertEqual(legacy.returncode, 0, legacy.stderr.decode())
        status = run_cli(["--status"], self.repo, self.gates_path)
        self.assertEqual(status.returncode, 0, status.stderr.decode())
        self.assertIn("Tier: tiny", status.stdout.decode())
        self.assertNotIn("Profile:", status.stdout.decode())

        manifest = self.write_manifest(["test", "lint", "typecheck", "review"])
        profiled = run_cli(
            [
                "--init",
                "tiny",
                "--capabilities-file",
                manifest,
            ],
            self.repo,
            self.gates_path,
        )
        self.assertEqual(profiled.returncode, 0, profiled.stderr.decode())
        status = run_cli(["--status"], self.repo, self.gates_path)
        self.assertEqual(status.returncode, 0, status.stderr.decode())
        self.assertIn(
            "Profile: review (lint, review, test, typecheck)",
            status.stdout.decode(),
        )
        oneline = run_cli(["--oneline"], self.repo, self.gates_path)
        self.assertEqual(oneline.returncode, 0, oneline.stderr.decode())
        self.assertIn("SDLC tiny/review:", oneline.stdout.decode())


class CurrentEnforcementContractTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.gates_path = os.path.join(self.repo, ".claude", "data", "gates.json")

    def test_complete_legacy_full_cycle_permits_pr_creation(self):
        run_cli(["--init", "small-medium"], self.repo, self.gates_path)
        for gate in ("tests", "lint", "typecheck"):
            result = run_cli(["--record", gate], self.repo, self.gates_path)
            self.assertEqual(result.returncode, 0, result.stderr.decode())

        # Skill-gated records enter only through authorized store calls (the
        # auto-record hook path), not through manual CLI recording.
        gs.update_store(
            self.gates_path,
            lambda d: [
                gs.record_gate(d, "feat/x", gate, authorized=True)
                for gate in gs.SKILL_FOR_GATE
            ],
        )

        result = run_enforce(self.repo, self.gates_path)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout.decode(), "")

    def test_no_cycle_remains_opt_in_and_allows_pr_creation(self):
        result = run_enforce(self.repo, self.gates_path)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout.decode(), "")


if __name__ == "__main__":
    unittest.main()
