#!/usr/bin/env python3
"""Tests for the generic portable-core adapter."""

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
SCRIPT = os.path.join(SCRIPTS, "generic_adapter.py")
sys.path.insert(0, SCRIPTS)

import capabilities  # noqa: E402
import gate_store as gs  # noqa: E402

spec = importlib.util.spec_from_file_location("generic_adapter", SCRIPT)
ga = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ga)


def git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], capture_output=True, check=True)


def make_repo(branch="feat/x"):
    repo = tempfile.mkdtemp()
    git(repo, "init", "-q", "-b", branch)
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    return repo


def make_manifest(capabilities_list, host_map=None):
    return {
        "protocol_version": "1",
        "provider": {
            "name": "generic",
            "agent": "generic-adapter",
            "model": "none",
        },
        "capabilities": capabilities_list,
        **({"x-host-command-map": host_map} if host_map else {}),
    }


def write_manifest(path, manifest):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(manifest, f)


class ResolveCommandsTest(unittest.TestCase):
    def test_host_map_overrides_default(self):
        manifest = make_manifest(["test"], {"test": "pytest"})
        commands = ga._resolve_commands(manifest)
        self.assertEqual(commands["test"], "pytest")

    def test_default_fallback(self):
        manifest = make_manifest(["test", "lint", "typecheck"])
        commands = ga._resolve_commands(manifest)
        self.assertIn("test", commands)
        self.assertIn("lint", commands)
        self.assertIn("typecheck", commands)

    def test_unmapped_capability_skipped(self):
        manifest = make_manifest(["imagine"])
        commands = ga._resolve_commands(manifest)
        self.assertNotIn("imagine", commands)


class RunGatesTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def test_passing_gates(self):
        manifest = make_manifest(
            ["test", "lint", "typecheck"],
            {"test": "true", "lint": "true", "typecheck": "true"},
        )
        results = ga.run_gates(manifest, "baseline", cwd=self.repo)
        self.assertEqual(len(results), 3)
        for r in results:
            self.assertEqual(r["exit_code"], 0)

    def test_failing_gate(self):
        manifest = make_manifest(
            ["test", "lint", "typecheck"],
            {"test": "true", "lint": "false", "typecheck": "true"},
        )
        results = ga.run_gates(manifest, "baseline", cwd=self.repo)
        self.assertEqual(results[1]["exit_code"], 1)
        self.assertIn("false", results[1]["command"])

    def test_unmapped_gate_fails(self):
        manifest = make_manifest(["test", "lint", "typecheck"])
        # Remove the default for lint to force an unmapped capability
        del ga.DEFAULT_COMMANDS["lint"]
        try:
            results = ga.run_gates(manifest, "baseline", cwd=self.repo)
            self.assertEqual(results[1]["exit_code"], 1)
            self.assertIn("no command mapped", results[1]["stderr"])
        finally:
            ga.DEFAULT_COMMANDS["lint"] = "python3 -m py_compile"


class BuildReviewReportTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def test_pass_report(self):
        manifest = make_manifest(["test"])
        results = [
            {
                "capability": "test",
                "command": "true",
                "exit_code": 0,
                "duration_s": 0.01,
                "stdout": "",
                "stderr": "",
            }
        ]
        report = ga.build_review_report(manifest, results, cwd=self.repo)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(len(report["findings"]), 0)
        self.assertEqual(report["provenance"]["kind"], "git-range")
        self.assertIn("base", report["provenance"])
        self.assertIn("head", report["provenance"])
        self.assertEqual(report["executor"]["adapter"], "generic")

    def test_fail_report(self):
        manifest = make_manifest(["test"])
        results = [
            {
                "capability": "test",
                "command": "false",
                "exit_code": 1,
                "duration_s": 0.01,
                "stdout": "",
                "stderr": "command failed",
            }
        ]
        report = ga.build_review_report(manifest, results, cwd=self.repo)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(len(report["findings"]), 1)
        self.assertEqual(report["findings"][0]["severity"], "serious")


class CliTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self.store_path = gs.default_store_path(self.repo)

    def run_cli(self, manifest_path, cwd=None):
        return subprocess.run(
            ["python3", SCRIPT, manifest_path],
            capture_output=True,
            cwd=cwd or self.repo,
        )

    def test_no_manifest_exits_2(self):
        r = self.run_cli("/nonexistent")
        self.assertEqual(r.returncode, 2)

    def test_missing_capability_exits_2(self):
        manifest = make_manifest(["imagine"])
        path = os.path.join(self.repo, "caps.json")
        write_manifest(path, manifest)
        r = self.run_cli(path)
        self.assertEqual(r.returncode, 2)
        self.assertIn("no complete baseline", r.stderr.decode())

    def test_passing_run_exits_0(self):
        manifest = make_manifest(
            ["test", "lint", "typecheck"],
            {"test": "true", "lint": "true", "typecheck": "true"},
        )
        path = os.path.join(self.repo, "caps.json")
        write_manifest(path, manifest)
        # Need a gate cycle for attach_report to succeed
        data = {}
        gs.init_gates(data, "feat/x", "tiny")
        os.makedirs(os.path.dirname(self.store_path), exist_ok=True)
        with open(self.store_path, "w") as f:
            json.dump(data, f)
        r = self.run_cli(path)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertIn("all gates passed", r.stderr.decode())
        # Verify the review report was attached
        with open(self.store_path) as f:
            store = json.load(f)
        self.assertIn("review_report", store["feat/x"])
        self.assertEqual(store["feat/x"]["review_report"]["status"], "pass")

    def test_failing_run_exits_1(self):
        manifest = make_manifest(
            ["test", "lint", "typecheck"],
            {"test": "true", "lint": "false", "typecheck": "true"},
        )
        path = os.path.join(self.repo, "caps.json")
        write_manifest(path, manifest)
        data = {}
        gs.init_gates(data, "feat/x", "tiny")
        os.makedirs(os.path.dirname(self.store_path), exist_ok=True)
        with open(self.store_path, "w") as f:
            json.dump(data, f)
        r = self.run_cli(path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("1 gate(s) failed", r.stderr.decode())
        with open(self.store_path) as f:
            store = json.load(f)
        self.assertEqual(store["feat/x"]["review_report"]["status"], "fail")

    def test_no_cycle_still_runs_but_warns(self):
        manifest = make_manifest(
            ["test", "lint", "typecheck"],
            {"test": "true", "lint": "true", "typecheck": "true"},
        )
        path = os.path.join(self.repo, "caps.json")
        write_manifest(path, manifest)
        r = self.run_cli(path)
        self.assertEqual(r.returncode, 0)
        self.assertIn("could not attach review report", r.stderr.decode())


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
