#!/usr/bin/env python3
"""Focused stdlib tests for portable manifests, reports, and adapters."""

import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)

import capabilities  # noqa: E402
import generic_adapter as ga  # noqa: E402
import gate_store as gs  # noqa: E402
import local_llm_adapter as la  # noqa: E402
import review_report  # noqa: E402


class CapabilitiesTest(unittest.TestCase):
    def manifest(self, caps):
        return {
            "protocol_version": "1",
            "provider": {"name": "test-host"},
            "capabilities": caps,
            "x-host-data": {"preserved": True},
        }

    def test_manifest_normalizes_and_preserves_extensions(self):
        manifest = capabilities.validate_manifest(
            self.manifest(["test", "lint", "typecheck"])
        )
        self.assertEqual(manifest["capabilities"], ["lint", "test", "typecheck"])
        self.assertEqual(manifest["x-host-data"], {"preserved": True})

    def test_manifest_rejects_unknown_capability(self):
        with self.assertRaisesRegex(ValueError, "unknown capability"):
            capabilities.validate_manifest(self.manifest(["test", "teleport"]))

    def test_profile_resolution_requires_requested_capabilities(self):
        result = capabilities.resolve_profile(["test", "lint", "typecheck"], "review")
        self.assertEqual(result["decision"], "unavailable")
        self.assertEqual(result["missing"], ["review"])
        self.assertEqual(
            capabilities.resolve_profile(["test", "lint", "typecheck", "review"])[
                "resolved_profile"
            ],
            "review",
        )


class ReviewReportTest(unittest.TestCase):
    def report(self, **overrides):
        report = {
            "protocol_version": "1",
            "status": "pass",
            "provenance": {"kind": "git-range", "base": "abc", "head": "def"},
            "executor": {"agent": "agent", "model": "model", "adapter": "adapter"},
            "findings": [],
        }
        report.update(overrides)
        return report

    def test_attach_preserves_gate_evidence(self):
        data = {"feat/x": {"tier": "tiny", "gates": {"tests": "stamp"}}}
        review_report.attach_report(data, "feat/x", self.report())
        self.assertEqual(data["feat/x"]["gates"], {"tests": "stamp"})
        self.assertEqual(data["feat/x"]["review_report"]["status"], "pass")

    def test_legacy_gate_file_wins_over_unrelated_neutral_state(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = os.path.join(directory, ".claude", "data", "gates.json")
            os.makedirs(os.path.dirname(legacy))
            with open(legacy, "w", encoding="utf-8") as f:
                f.write("{}")
            os.makedirs(os.path.join(directory, ".sharpen", "data"))
            self.assertEqual(
                gs.state_file_path("gates.json", "MISSING_ENV", directory),
                os.path.realpath(legacy),
            )

    def test_profile_and_report_are_rendered_without_changing_gates(self):
        data = {}
        cycle = gs.init_gates(
            data,
            "feat/x",
            "tiny",
            profile="baseline",
            capabilities=["typecheck", "test", "lint"],
        )
        review_report.attach_report(data, "feat/x", self.report())
        self.assertEqual(cycle["capabilities"], ["lint", "test", "typecheck"])
        self.assertEqual(cycle["gates"], {})
        self.assertIn("Profile: baseline", gs.format_status(cycle))
        self.assertIn("Review report: pass", gs.format_status(cycle))

    def test_rejects_malformed_finding(self):
        with self.assertRaisesRegex(ValueError, "findings\\[0\\].location"):
            review_report.validate_report(
                self.report(
                    findings=[
                        {
                            "severity": "serious",
                            "summary": "broken",
                            "location": "",
                            "consequence": "bad",
                        }
                    ]
                )
            )


class GenericAdapterTest(unittest.TestCase):
    def manifest(self):
        return {
            "protocol_version": "1",
            "provider": {"name": "test-host"},
            "capabilities": ["test", "lint", "typecheck"],
            "x-host-command-map": {
                "test": "python3 -c 'print(1)'",
                "lint": "python3 -c 'print(2)'",
                "typecheck": "python3 -c 'print(3)'",
            },
        }

    def test_run_gates_uses_host_commands(self):
        results = ga.run_gates(self.manifest(), "baseline")
        self.assertEqual([result["exit_code"] for result in results], [0, 0, 0])
        self.assertEqual(
            [result["capability"] for result in results], ["test", "lint", "typecheck"]
        )

    def test_default_syntax_commands_are_runnable(self):
        manifest = {"capabilities": ["test", "lint", "typecheck"]}
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "valid.py"), "w", encoding="utf-8") as f:
                f.write("value = 1\n")
            results = ga.run_gates(manifest, "baseline", directory)
        self.assertEqual([result["exit_code"] for result in results], [0, 0, 0])


class LocalLlmAdapterTest(unittest.TestCase):
    def manifest(self):
        return {"provider": {"name": "local", "model": "test-model"}}

    def passed_gates(self):
        return [{"capability": "test", "exit_code": 0}]

    @mock.patch.object(ga, "_detect_head", return_value="head")
    @mock.patch.object(ga, "_detect_base", return_value="base")
    @mock.patch.object(la, "_extract_diff", return_value="diff")
    @mock.patch.object(la, "_local_llm_chat")
    def test_critical_finding_fails_review(self, chat, *_):
        chat.return_value = (
            '[{"severity":"critical","summary":"loss","location":"a.py:1",'
            '"consequence":"data loss"}]'
        )
        report = la.build_review_report(self.manifest(), self.passed_gates())
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["findings"][0]["severity"], "critical")

    @mock.patch.object(ga, "_detect_head", return_value="head")
    @mock.patch.object(ga, "_detect_base", return_value="base")
    @mock.patch.object(la, "_extract_diff", return_value="diff")
    @mock.patch.object(la, "_local_llm_chat")
    def test_malformed_model_finding_fails_review(self, chat, *_):
        chat.return_value = '[{"severity":"nit","summary":"missing fields"}]'
        report = la.build_review_report(self.manifest(), self.passed_gates())
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["findings"][0]["summary"], "delegated review failed")

    @mock.patch.object(ga, "_detect_head", return_value="head")
    @mock.patch.object(ga, "_detect_base", return_value="base")
    @mock.patch.object(la, "_extract_diff", return_value="diff")
    @mock.patch.object(la, "_local_llm_chat", return_value="not JSON")
    def test_non_json_model_response_fails_review(self, *_):
        report = la.build_review_report(self.manifest(), self.passed_gates())
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["findings"][0]["summary"], "delegated review failed")


if __name__ == "__main__":
    unittest.main()
