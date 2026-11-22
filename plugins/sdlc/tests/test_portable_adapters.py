#!/usr/bin/env python3
"""Focused stdlib tests for portable manifests, reports, and adapters."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
RECORD = os.path.join(SCRIPTS, "record-gate.py")
GRUMPY_RECEIPT = os.path.normpath(
    os.path.join(HERE, "..", "..", "grumpy", "scripts", "receipt.py")
)
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

    def test_non_string_and_empty_executor_fields_are_dropped(self):
        # `{"model": null}` used to be stored as the text "None" and shown as
        # gate evidence; a host that isn't a non-empty string is no executor.
        data = {}
        gs.init_gates(data, "feat/x", "small-medium")
        head = {"head": "a" * 40, "written_at": "2026-11-22T00:00:00+00:00"}
        self.stamp(
            data, dict(head, executor={"host": "oneshot", "provider": 7, "model": None})
        )
        self.assertEqual(
            data["feat/x"][gr.EVIDENCE_EXECUTORS_KEY]["grumpy-review"],
            {"host": "oneshot"},
        )
        self.assertIn("(via oneshot)", gs.format_status(data["feat/x"], "feat/x"))
        for host in ("", 0, None):
            self.stamp(data, dict(head, executor={"host": host, "model": "m"}))
            self.assertNotIn(gr.EVIDENCE_EXECUTORS_KEY, data["feat/x"], host)

    def test_status_tolerates_a_hand_edited_executor(self):
        # format_status already tolerates hand-edited attestations; the new
        # key must not be the one that crashes --status.
        def note(executor):
            bd = {"gates": {}, gr.EVIDENCE_EXECUTORS_KEY: {"grumpy-review": executor}}
            return gr.executor_note(bd, "grumpy-review")

        self.assertEqual(note([]), "")
        self.assertEqual(note({"host": ["oneshot"]}), "")
        self.assertEqual(note({"host": "oneshot", "model": 3}), " (via oneshot)")
        self.assertEqual(gr.executor_note({gr.EVIDENCE_EXECUTORS_KEY: "junk"}, "g"), "")


class OneshotReceiptChainTest(unittest.TestCase):
    """The cross-plugin chain this layer exists for, end to end: grumpy's
    `write_receipt(..., executor=...)` leaves the receipt, sdlc's
    `record-gate.py --verify review` records gate 3 from it, and `--status`
    says the gate came from oneshot."""

    def setUp(self):
        self.repo = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        for args in (
            ("init", "-q", "-b", "feat/x"),
            ("config", "user.email", "t@t"),
            ("config", "user.name", "t"),
            ("commit", "-q", "--allow-empty", "-m", "init"),
        ):
            subprocess.run(["git", "-C", self.repo, *args], check=True)
        self.env = dict(
            os.environ,
            SDLC_GATES_PATH=os.path.join(self.repo, ".sharpen", "data", "gates.json"),
            SDLC_CROSS_REPO_ROUTES_PATH=os.path.join(self.repo, "routes.json"),
        )
        self.env.pop("SDLC_GATE_EVIDENCE", None)
        spec = importlib.util.spec_from_file_location("grumpy_receipt", GRUMPY_RECEIPT)
        self.receipt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.receipt)

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, RECORD, *args],
            capture_output=True,
            text=True,
            cwd=self.repo,
            env=self.env,
        )

    def test_oneshot_receipt_records_gate_3_and_status_names_the_path(self):
        r = self.cli("--init", "small-medium")
        self.assertEqual(r.returncode, 0, r.stderr)
        folder = os.path.join(self.repo, *self.receipt.GRUMPY_ROOT, "feat/x")
        os.makedirs(folder)
        with open(os.path.join(folder, "review.md"), "w") as f:
            f.write(
                "# Code Review: x\n\n_Model: gpt-x via openai (one-shot, no agent)_\n"
            )
        path, written = self.receipt.write_receipt(
            "review",
            self.repo,
            executor={"host": "oneshot", "provider": "openai", "model": "gpt-x"},
        )
        with open(path) as f:
            self.assertEqual(json.load(f)["executor"], written["executor"])
        r = self.cli("--verify", "review")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.cli("--status")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout + r.stderr
        self.assertRegex(out, r"grumpy-review — \S+ \(via oneshot: openai gpt-x\)")
        with open(self.env["SDLC_GATES_PATH"]) as f:
            bd = json.load(f)["feat/x"]
        self.assertIn("grumpy-review", bd["gates"])
        self.assertEqual(
            bd[gr.EVIDENCE_EXECUTORS_KEY]["grumpy-review"], written["executor"]
        )


if __name__ == "__main__":
    unittest.main()
