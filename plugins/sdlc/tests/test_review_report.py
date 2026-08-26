#!/usr/bin/env python3
"""Stdlib tests for portable review reports and gate-store attachment."""

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
import review_report  # noqa: E402

RECORD = os.path.join(SCRIPTS, "record-gate.py")


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


def report(**overrides):
    data = {
        "protocol_version": "1",
        "status": "pass",
        "provenance": {"kind": "git-range", "base": "origin/main", "head": "HEAD"},
        "executor": {"agent": "unit-test", "adapter": "test"},
        "findings": [],
    }
    data.update(overrides)
    return data


class ReportValidationTest(unittest.TestCase):
    def test_valid_report_preserves_extension_fields(self):
        result = review_report.validate_report(report(**{"x-host": {"id": "r1"}}))
        self.assertEqual(result["x-host"], {"id": "r1"})

    def test_protocol_version_is_required(self):
        for value in (None, 1, "2"):
            with self.subTest(value=value):
                data = report()
                if value is None:
                    data.pop("protocol_version")
                else:
                    data["protocol_version"] = value
                with self.assertRaisesRegex(ValueError, "protocol_version"):
                    review_report.validate_report(data)

    def test_git_range_requires_base_and_head(self):
        for key in ("base", "head"):
            with self.subTest(key=key):
                data = report()
                del data["provenance"][key]
                with self.assertRaisesRegex(ValueError, f"provenance.{key}"):
                    review_report.validate_report(data)

    def test_legacy_provenance_needs_no_range(self):
        result = review_report.validate_report(
            report(provenance={"kind": "legacy", "x-note": "imported"})
        )
        self.assertEqual(result["provenance"]["x-note"], "imported")

    def test_finding_requires_actionable_fields(self):
        finding = {
            "severity": "serious",
            "summary": "unsafe parse",
            "location": "app.py:10",
            "consequence": "bad input reaches sink",
        }
        for key in ("summary", "location", "consequence"):
            with self.subTest(key=key):
                broken = dict(finding)
                broken[key] = ""
                with self.assertRaisesRegex(ValueError, f"findings\\[0\\].{key}"):
                    review_report.validate_report(report(findings=[broken]))

    def test_invalid_status_and_severity_fail(self):
        with self.assertRaisesRegex(ValueError, "status"):
            review_report.validate_report(report(status="approved"))
        with self.assertRaisesRegex(ValueError, "severity"):
            review_report.validate_report(
                report(
                    findings=[
                        {
                            "severity": "high",
                            "summary": "x",
                            "location": "x",
                            "consequence": "x",
                        }
                    ]
                )
            )


class AttachReportCliTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.gates_path = os.path.join(self.repo, ".claude", "data", "gates.json")

    def write_report(self, data, name="review.json"):
        path = os.path.join(self.repo, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return path

    def read_cycle(self):
        with open(self.gates_path, encoding="utf-8") as f:
            return json.load(f)["feat/x"]

    def test_attach_requires_existing_cycle_and_does_not_create_store(self):
        path = self.write_report(report())
        result = run_cli(["--attach-review", path], self.repo, self.gates_path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("No gate cycle", result.stderr.decode())
        self.assertFalse(os.path.exists(self.gates_path))

    def test_attach_preserves_gate_timestamps_and_surfaces_status(self):
        init = run_cli(["--init", "tiny"], self.repo, self.gates_path)
        self.assertEqual(init.returncode, 0, init.stderr.decode())
        record = run_cli(["--record", "tests"], self.repo, self.gates_path)
        self.assertEqual(record.returncode, 0, record.stderr.decode())
        before = self.read_cycle()

        path = self.write_report(
            report(
                status="fail",
                findings=[
                    {
                        "severity": "suggestion",
                        "summary": "name is vague",
                        "location": "plugins/sdlc/scripts/review_report.py:1",
                        "consequence": "readers may not infer scope",
                    }
                ],
            )
        )
        result = run_cli(["--attach-review", path], self.repo, self.gates_path)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("attached fail review report", result.stderr.decode())
        after = self.read_cycle()
        self.assertEqual(after["gates"], before["gates"])
        self.assertEqual(after["review_report"]["status"], "fail")
        self.assertEqual(len(after["review_report"]["findings"]), 1)

        status = run_cli(["--status"], self.repo, self.gates_path)
        self.assertEqual(status.returncode, 0, status.stderr.decode())
        self.assertIn("Review report: fail", status.stdout.decode())
        self.assertIn("origin/main...HEAD", status.stdout.decode())
        oneline = run_cli(["--oneline"], self.repo, self.gates_path)
        self.assertIn("SDLC tiny/review:fail:", oneline.stdout.decode())

    def test_invalid_report_fails_without_mutating_cycle(self):
        init = run_cli(["--init", "tiny"], self.repo, self.gates_path)
        self.assertEqual(init.returncode, 0, init.stderr.decode())
        before = self.read_cycle()
        path = self.write_report(report(status="approved"))
        result = run_cli(["--attach-review", path], self.repo, self.gates_path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("status", result.stderr.decode())
        self.assertEqual(self.read_cycle(), before)

    def test_attach_rejects_combination_with_gate_commands(self):
        path = self.write_report(report())
        for args in (
            ["--attach-review", path, "--init", "tiny"],
            ["--attach-review", path, "--record", "tests"],
            ["--attach-review", path, "--status"],
            ["--attach-review", path, "--oneline"],
            ["--attach-review", path, "--profile", "review"],
        ):
            with self.subTest(args=args):
                result = run_cli(args, self.repo, self.gates_path)
                self.assertEqual(result.returncode, 1)
                self.assertIn("--attach-review", result.stderr.decode())
        self.assertFalse(os.path.exists(self.gates_path))


class StoreAttachmentTest(unittest.TestCase):
    def test_attach_revalidates_direct_store_calls(self):
        data = {}
        gs.init_gates(data, "feat/x", "tiny")
        with self.assertRaisesRegex(ValueError, "protocol_version"):
            review_report.attach_report(data, "feat/x", {})


if __name__ == "__main__":
    unittest.main()
