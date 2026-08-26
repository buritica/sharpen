#!/usr/bin/env python3
"""Tests for the portable CI gate validator."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
SCRIPT = os.path.join(SCRIPTS, "ci-validate.py")
sys.path.insert(0, SCRIPTS)

import gate_store as gs  # noqa: E402

spec = importlib.util.spec_from_file_location("ci_validate", SCRIPT)
ci_validate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_validate)


def git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], capture_output=True, check=True)


def make_repo(branch="feat/x"):
    repo = tempfile.mkdtemp()
    git(repo, "init", "-q", "-b", branch)
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    return repo


def read_json(path):
    with open(path) as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self.store_path = gs.default_store_path(self.repo)

    def _init(self, tier="tiny", gates=None, report=None):
        data = {}
        gs.init_gates(data, "feat/x", tier)
        for gate in gates or []:
            gs.record_gate(data, "feat/x", gate, authorized=True)
        if report is not None:
            data["feat/x"]["review_report"] = report
        write_json(self.store_path, data)

    def test_no_cycle_allows(self):
        allowed, reason = ci_validate.validate("feat/x", "abc123", cwd=self.repo)
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_complete_cycle_allows(self):
        self._init(tier="tiny", gates=["tests", "lint", "typecheck"])
        allowed, reason = ci_validate.validate("feat/x", "abc123", cwd=self.repo)
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_incomplete_cycle_denies(self):
        self._init(tier="tiny", gates=["tests"])
        allowed, reason = ci_validate.validate("feat/x", "abc123", cwd=self.repo)
        self.assertFalse(allowed)
        self.assertIn("lint", reason)
        self.assertIn("typecheck", reason)

    def test_corrupt_store_denies(self):
        os.makedirs(os.path.dirname(self.store_path), exist_ok=True)
        with open(self.store_path, "w") as f:
            f.write("not json")
        allowed, reason = ci_validate.validate("feat/x", "abc123", cwd=self.repo)
        self.assertFalse(allowed)
        self.assertIn("unreadable", reason)

    def test_passing_review_report_with_matching_head_allows(self):
        report = {
            "protocol_version": "1",
            "status": "pass",
            "provenance": {"kind": "git-range", "base": "main", "head": "abc123"},
            "findings": [],
        }
        self._init(tier="tiny", gates=["tests", "lint", "typecheck"], report=report)
        allowed, reason = ci_validate.validate("feat/x", "abc123", cwd=self.repo)
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_passing_review_report_with_mismatched_head_denies(self):
        report = {
            "protocol_version": "1",
            "status": "pass",
            "provenance": {"kind": "git-range", "base": "main", "head": "abc123"},
            "findings": [],
        }
        self._init(tier="tiny", gates=["tests", "lint", "typecheck"], report=report)
        allowed, reason = ci_validate.validate("feat/x", "def456", cwd=self.repo)
        self.assertFalse(allowed)
        self.assertIn("does not match", reason)

    def test_failing_review_report_denies(self):
        report = {
            "protocol_version": "1",
            "status": "fail",
            "provenance": {"kind": "git-range", "base": "main", "head": "abc123"},
            "findings": [
                {
                    "severity": "serious",
                    "summary": "x",
                    "location": "y",
                    "consequence": "z",
                }
            ],
        }
        self._init(tier="tiny", gates=["tests", "lint", "typecheck"], report=report)
        allowed, reason = ci_validate.validate("feat/x", "abc123", cwd=self.repo)
        self.assertFalse(allowed)
        self.assertIn("status=fail", reason)

    def test_invalid_review_report_denies(self):
        report = {"protocol_version": "1", "status": "pass"}
        self._init(tier="tiny", gates=["tests", "lint", "typecheck"], report=report)
        allowed, reason = ci_validate.validate("feat/x", "abc123", cwd=self.repo)
        self.assertFalse(allowed)
        self.assertIn("invalid", reason)

    def test_legacy_provenance_skips_head_check(self):
        report = {
            "protocol_version": "1",
            "status": "pass",
            "provenance": {"kind": "legacy"},
            "findings": [],
        }
        self._init(tier="tiny", gates=["tests", "lint", "typecheck"], report=report)
        allowed, reason = ci_validate.validate("feat/x", "abc123", cwd=self.repo)
        self.assertTrue(allowed)
        self.assertIsNone(reason)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self.store_path = gs.default_store_path(self.repo)

    def run_cli(self, env=None):
        env = env or dict(os.environ)
        env.pop("GITHUB_HEAD_REF", None)
        env.pop("GITHUB_SHA", None)
        return subprocess.run(
            ["python3", SCRIPT],
            capture_output=True,
            cwd=self.repo,
            env=env,
        )

    def test_no_cycle_exits_0(self):
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr.decode())

    def test_incomplete_cycle_exits_1(self):
        data = {}
        gs.init_gates(data, "feat/x", "tiny")
        write_json(self.store_path, data)
        r = self.run_cli()
        self.assertEqual(r.returncode, 1)
        self.assertIn("BLOCKED", r.stderr.decode())

    def test_complete_cycle_exits_0(self):
        data = {}
        gs.init_gates(data, "feat/x", "tiny")
        for gate in ("tests", "lint", "typecheck"):
            gs.record_gate(data, "feat/x", gate, authorized=True)
        write_json(self.store_path, data)
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertIn("gates complete", r.stderr.decode())

    def test_github_env_overrides_local_detection(self):
        data = {}
        gs.init_gates(data, "other/branch", "tiny")
        for gate in ("tests", "lint", "typecheck"):
            gs.record_gate(data, "other/branch", gate, authorized=True)
        write_json(self.store_path, data)
        r = self.run_cli(
            env=dict(
                os.environ,
                GITHUB_HEAD_REF="other/branch",
                GITHUB_SHA="abc123",
            )
        )
        self.assertEqual(r.returncode, 0, r.stderr.decode())


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
