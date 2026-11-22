#!/usr/bin/env python3
"""Tests for host detection, the support matrix, and how auto-init uses them.

The auto-init tests drive the real hook as a subprocess with a pinned host
environment, because this suite itself may run under Claude Code
(CLAUDECODE=1) or pi (SHARPEN_HOST=pi).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
AUTO_INIT = os.path.join(SCRIPTS, "auto-init-gate-cycle.py")
sys.path.insert(0, SCRIPTS)

import hosts  # noqa: E402

SKILLS = ["simplify", "review", "fix", "imagine"]
BASE = ["test", "lint", "typecheck", "plan", "ship"]


def git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], capture_output=True, check=True)


def host_env(**overrides):
    env = dict(os.environ)
    for key in (
        "SHARPEN_HOST",
        "CLAUDECODE",
        "SDLC_CAPABILITIES_PATH",
        "SDLC_GATE_EVIDENCE",
        "SDLC_GATES_PATH",
    ):
        env.pop(key, None)
    env.update({k: v for k, v in overrides.items() if v is not None})
    return env


class DetectHostTest(unittest.TestCase):
    def test_explicit_host_wins(self):
        self.assertEqual(
            hosts.detect_host({"SHARPEN_HOST": "pi", "CLAUDECODE": "1"}), "pi"
        )

    def test_claude_code_is_recognized_by_its_own_marker(self):
        self.assertEqual(hosts.detect_host({"CLAUDECODE": "1"}), "claude-code")

    def test_no_signal_is_unknown_not_a_guess(self):
        self.assertEqual(hosts.detect_host({}), "unknown")
        self.assertEqual(hosts.detect_host({"CLAUDECODE": "0"}), "unknown")

    def test_malformed_explicit_host_is_unknown(self):
        # It ends up in a filename: capabilities.<host>.json.
        for bad in ("../etc", "a b", "x" * 80, "/abs"):
            with self.subTest(bad=bad):
                self.assertEqual(hosts.detect_host({"SHARPEN_HOST": bad}), "unknown")

    def test_explicit_host_is_normalized(self):
        self.assertEqual(hosts.detect_host({"SHARPEN_HOST": " Codex "}), "codex")


class RecordingModeTest(unittest.TestCase):
    def test_hosts_with_a_sweep_are_automatic(self):
        self.assertEqual(hosts.recording_mode("claude-code"), "automatic")
        self.assertEqual(hosts.recording_mode("pi"), "automatic")

    def test_everything_else_is_manual(self):
        # Codex stays manual until its Stop hook is seen firing.
        for host in ("codex", "unknown", "instructions-only", "fx"):
            with self.subTest(host=host):
                self.assertEqual(hosts.recording_mode(host), "manual")


class SupportMatrixTest(unittest.TestCase):
    def test_every_row_fills_every_column(self):
        for name, row in hosts.HOSTS.items():
            for key, _ in hosts.MATRIX_COLUMNS:
                field = "recording_cell" if key == "recording" else key
                with self.subTest(host=name, column=key):
                    self.assertTrue(row.get(field))
                    self.assertNotIn("|", row[field])

    def test_codex_open_questions_are_marked_unverified(self):
        row = hosts.HOSTS["codex"]
        self.assertIn(hosts.UNVERIFIED, row["recording_cell"])
        self.assertIn(hosts.UNVERIFIED, row["plugin_root"])

    def test_committed_doc_matches_the_table(self):
        path = os.path.join(REPO_ROOT, "docs", "hosts.md")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(
            hosts.splice_matrix(text), text, "run hosts.py --write docs/hosts.md"
        )

    def test_splice_without_markers_is_an_error(self):
        with self.assertRaises(ValueError):
            hosts.splice_matrix("# no markers here\n")

    def test_write_flag_refreshes_a_stale_doc(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        doc = os.path.join(tmp, "hosts.md")
        with open(doc, "w", encoding="utf-8") as f:
            f.write(f"x\n{hosts.MATRIX_BEGIN}\nold\n{hosts.MATRIX_END}\n")
        r = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "hosts.py"), "--write", doc],
            capture_output=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        with open(doc, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(hosts.splice_matrix(text), text)
        self.assertNotIn("\nold\n", text)


class AutoInitHostTest(unittest.TestCase):
    """auto-init reads its own host's manifest and says how gates 2-6 record
    on that host. The tier never changes because of the manifest."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        git(self.repo, "init", "-q", "-b", "feat/x")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "init")
        self.data = os.path.join(self.repo, ".sharpen", "data")
        os.makedirs(self.data)
        self.gp = os.path.join(self.data, "gates.json")

    def write_manifest(self, name, host, caps, **extra):
        manifest = {
            "protocol_version": "1",
            "provider": {"name": host},
            "capabilities": caps,
            **extra,
        }
        with open(os.path.join(self.data, name), "w", encoding="utf-8") as f:
            json.dump(manifest, f)

    def commit(self, **env):
        payload = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}}
        r = subprocess.run(
            [sys.executable, AUTO_INIT],
            input=json.dumps(payload).encode(),
            capture_output=True,
            cwd=self.repo,
            env=host_env(**env),
        )
        with open(self.gp, encoding="utf-8") as f:
            cycle = json.load(f)["feat/x"]
        return r, cycle

    def test_pi_reads_its_own_manifest_and_records_automatically(self):
        self.write_manifest(
            "capabilities.pi.json",
            "pi",
            BASE + SKILLS,
            **{"x-recording": "automatic", "x-receipts": True},
        )
        r, cycle = self.commit(SHARPEN_HOST="pi")
        out = r.stderr.decode()
        self.assertEqual(r.returncode, 2, out)
        self.assertEqual(cycle["tier"], "small-medium")
        self.assertEqual(cycle["evidence"], "receipts")
        self.assertIn("records it at the end of the turn", out)
        self.assertNotIn("lists no grumpy", out)

    def test_another_hosts_manifest_is_ignored(self):
        # A Claude Code session's declaration says nothing about pi.
        self.write_manifest(
            "capabilities.claude-code.json", "claude-code", BASE + SKILLS
        )
        r, cycle = self.commit(SHARPEN_HOST="pi")
        self.assertNotIn("profile", cycle)
        self.assertNotIn("capabilities", cycle)

    def test_unknown_host_is_told_to_run_verify(self):
        r, cycle = self.commit()
        out = r.stderr.decode()
        self.assertEqual(cycle["tier"], "small-medium")
        self.assertIn("run `record-gate.py --verify <pass>` after each pass", out)

    def test_claude_code_falls_back_to_the_pre_4_15_manifest_name(self):
        self.write_manifest(hosts.LEGACY_MANIFEST_NAME, "claude-code", BASE + SKILLS)
        r, cycle = self.commit(CLAUDECODE="1")
        self.assertIn("simplify", cycle["capabilities"])

    def test_other_hosts_do_not_read_the_pre_4_15_manifest(self):
        self.write_manifest(hosts.LEGACY_MANIFEST_NAME, "claude-code", BASE + SKILLS)
        r, cycle = self.commit(SHARPEN_HOST="codex")
        self.assertNotIn("capabilities", cycle)

    def test_missing_grumpy_passes_are_named_and_tier_stays(self):
        self.write_manifest("capabilities.pi.json", "pi", BASE + ["review"])
        r, cycle = self.commit(SHARPEN_HOST="pi")
        out = r.stderr.decode()
        self.assertEqual(cycle["tier"], "small-medium")
        self.assertIn("lists no grumpy simplify, fix, imagine", out)
        self.assertIn("/sdlc:gate --init tiny", out)

    def test_grumpy_without_receipts_gets_an_event_only_cycle(self):
        # grumpy < 2.9.0: no pass writes a receipt, so a receipt-backed
        # cycle could never complete.
        self.write_manifest(
            "capabilities.pi.json", "pi", BASE + SKILLS, **{"x-receipts": False}
        )
        r, cycle = self.commit(SHARPEN_HOST="pi")
        out = r.stderr.decode()
        self.assertEqual(cycle["tier"], "small-medium")
        self.assertNotIn("evidence", cycle)
        self.assertIn("predates receipts", out)
        self.assertIn("2.9.0", out)

    def test_no_receipts_without_grumpy_at_all_keeps_receipt_cycle(self):
        # Nothing records without grumpy either way; the message names it.
        self.write_manifest("capabilities.pi.json", "pi", BASE, **{"x-receipts": False})
        r, cycle = self.commit(SHARPEN_HOST="pi")
        self.assertEqual(cycle["evidence"], "receipts")
        self.assertIn("lists no grumpy", r.stderr.decode())

    def test_events_override_still_wins(self):
        self.write_manifest(
            "capabilities.pi.json", "pi", BASE + SKILLS, **{"x-receipts": True}
        )
        r, cycle = self.commit(SHARPEN_HOST="pi", SDLC_GATE_EVIDENCE="events")
        self.assertNotIn("evidence", cycle)


if __name__ == "__main__":
    unittest.main()
