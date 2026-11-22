#!/usr/bin/env python3
"""Focused stdlib tests for portable manifests, reports, and adapters."""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)

import capabilities  # noqa: E402
import generic_adapter as ga  # noqa: E402
import gate_receipts as gr  # noqa: E402
import gate_store as gs  # noqa: E402
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


class ExecutorStatusTest(unittest.TestCase):
    """A gate earned from a one-shot receipt says so in --status."""

    def stamp(self, data, receipt):
        data["feat/x"].setdefault("gates", {})["grumpy-review"] = "2026-11-22T00:00:00Z"
        gr.stamp_receipt(data, "feat/x", "grumpy-review", receipt, [], {})

    def test_oneshot_receipt_is_named_then_cleared_by_an_agent_receipt(self):
        data = {}
        gs.init_gates(data, "feat/x", "small-medium")
        head = {"head": "a" * 40, "written_at": "2026-11-22T00:00:00+00:00"}
        executor = {"host": "oneshot", "provider": "openai", "model": "gpt-x"}
        self.stamp(data, dict(head, executor=executor))
        self.assertIn(
            "grumpy-review — 2026-11-22T00:00:00Z (via oneshot: openai gpt-x)",
            gs.format_status(data["feat/x"], "feat/x"),
        )
        self.stamp(data, head)
        self.assertNotIn("via oneshot", gs.format_status(data["feat/x"], "feat/x"))
        self.assertNotIn(gr.EVIDENCE_EXECUTORS_KEY, data["feat/x"])


if __name__ == "__main__":
    unittest.main()
