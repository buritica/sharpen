#!/usr/bin/env python3
"""Receipt-backed skill-gate recording (sharpen#52, #53).

A receipt-backed cycle stamps a skill gate only from the receipt its grumpy
pass leaves, naming the branch's current HEAD. These tests drive the real
CLI, hooks and grumpy's receipt writer as subprocesses, the way every host
reaches them.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, "..", "scripts"))
GRUMPY_RECEIPT = os.path.normpath(
    os.path.join(HERE, "..", "..", "grumpy", "scripts", "receipt.py")
)
sys.path.insert(0, SCRIPTS)

# Isolate from the machine's real cross-repo registry, and make sure no
# opt-out leaks in from the environment: these cycles must be receipt-backed.
os.environ["SDLC_CROSS_REPO_ROUTES_PATH"] = os.path.join(
    tempfile.mkdtemp(), "cross-repo-routes.json"
)
os.environ.pop("SDLC_GATE_EVIDENCE", None)

import gate_receipts as rc  # noqa: E402
import gate_store as gs  # noqa: E402

RECORD = os.path.join(SCRIPTS, "record-gate.py")
ENFORCE = os.path.join(SCRIPTS, "enforce-sdlc-gates.py")
AUTO = os.path.join(SCRIPTS, "auto-record-skill-gate.py")
SWEEP = os.path.join(SCRIPTS, "sweep-skill-receipts.py")
BRANCH = "feat/x"


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, check=True, text=True
    ).stdout.strip()


def make_repo():
    repo = os.path.realpath(tempfile.mkdtemp())
    git(repo, "init", "-q", "-b", BRANCH)
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    return repo


class ReceiptTestBase(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.store = os.path.join(self.repo, ".sharpen", "data", "gates.json")
        self.env = dict(os.environ, SDLC_GATES_PATH=self.store)
        self.cli("--init", "small-medium")

    def run_py(self, script, *args, stdin=None):
        return subprocess.run(
            ["python3", script, *args],
            input=stdin,
            capture_output=True,
            text=True,
            cwd=self.repo,
            env=self.env,
        )

    def cli(self, *args):
        return self.run_py(RECORD, *args)

    def receipt(self, pass_name):
        r = self.run_py(GRUMPY_RECEIPT, pass_name, "--worktree", self.repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        # Receipts compare by timestamp against gate stamps; keep the next
        # stamp strictly later even on a coarse clock.
        time.sleep(0.01)

    def verify(self, pass_name):
        return self.cli("--verify", pass_name)

    def commit(self, msg="change"):
        git(self.repo, "commit", "-q", "--allow-empty", "-m", msg)

    def bd(self):
        with open(self.store) as f:
            return json.load(f)[BRANCH]

    def gates(self):
        return self.bd().get("gates", {})

    def hook(self, script, payload):
        return self.run_py(script, stdin=json.dumps(payload))

    def run_chain(self):
        for step in ("simplify", "review", "fix", "imagine", "fix"):
            self.receipt(step)
            r = self.verify(step)
            self.assertEqual(r.returncode, 0, r.stderr)
        for gate in ("tests", "lint", "typecheck"):
            self.assertEqual(self.cli("--record", gate).returncode, 0)

    def pr_create(self):
        return self.hook(
            ENFORCE,
            {
                "tool_name": "Bash",
                "tool_input": {"command": "gh pr create --title t --body b"},
                "cwd": self.repo,
            },
        )


class InitTest(ReceiptTestBase):
    def test_cli_init_makes_a_receipt_backed_cycle(self):
        self.assertEqual(self.bd().get("evidence"), "receipts")

    def test_opt_out_env_makes_a_legacy_cycle(self):
        self.env["SDLC_GATE_EVIDENCE"] = "events"
        self.cli("--init", "small-medium")
        self.assertNotIn("evidence", self.bd())


class VerifyTest(ReceiptTestBase):
    def test_no_receipt_records_nothing_and_says_why(self):
        r = self.verify("review")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no receipt yet", r.stderr)
        self.assertNotIn("grumpy-review", self.gates())

    def test_fresh_receipt_records_and_keeps_the_head(self):
        self.receipt("review")
        r = self.verify("review")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("grumpy-review", self.gates())
        head = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(self.bd()["evidence_heads"]["grumpy-review"], head)

    def test_receipt_for_an_older_commit_is_refused(self):
        self.receipt("review")
        self.commit()
        r = self.verify("review")
        self.assertEqual(r.returncode, 1)
        self.assertIn("re-run the pass", r.stderr)

    def test_receipt_from_before_the_cycle_is_refused(self):
        self.receipt("review")
        time.sleep(0.01)
        self.cli("--init", "small-medium")
        r = self.verify("review")
        self.assertEqual(r.returncode, 1)
        self.assertIn("predates this gate cycle", r.stderr)

    def test_first_fix_receipt_cannot_earn_the_second_fix_gate(self):
        self.receipt("review")
        self.verify("review")
        self.receipt("fix")
        self.assertEqual(self.verify("fix").returncode, 0)
        self.receipt("imagine")
        self.assertEqual(self.verify("imagine").returncode, 0)
        # The fix receipt on disk predates the imagine stamp.
        r = self.verify("fix")
        self.assertEqual(r.returncode, 1)
        self.assertIn("predates the grumpy-imagine gate", r.stderr)
        self.receipt("fix")
        self.assertEqual(self.verify("fix").returncode, 0)
        self.assertIn("grumpy-fix-post-imagine", self.gates())

    def test_fix_receipt_written_before_review_is_refused(self):
        self.receipt("fix")
        self.receipt("review")
        self.verify("review")
        r = self.verify("fix")
        self.assertEqual(r.returncode, 1)
        self.assertIn("predates the grumpy-review gate", r.stderr)

    def test_verify_all_sweeps_in_chain_order(self):
        for step in ("simplify", "review"):
            self.receipt(step)
        r = self.verify("all")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("simplify", self.gates())
        self.assertIn("grumpy-review", self.gates())
        self.assertNotIn("grumpy-fix-post-review", self.gates())

    def test_unknown_pass_is_an_error(self):
        r = self.verify("audit")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a gated skill", r.stderr)

    def test_manual_record_of_a_skill_gate_is_still_refused(self):
        self.receipt("review")
        r = self.cli("--record", "grumpy-review")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("grumpy-review", self.gates())


class HostHookTest(ReceiptTestBase):
    def skill_event(self, skill):
        return self.hook(
            AUTO,
            {
                "tool_name": "Skill",
                "tool_input": {"skill": skill},
                "tool_response": {},
                "cwd": self.repo,
            },
        )

    def test_skill_hook_at_launch_records_nothing(self):
        # #53: PostToolUse fires when the instructions load, before any work.
        r = self.skill_event("grumpy:review")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("grumpy-review", self.gates())

    def test_cached_repeat_event_records_once_the_receipt_exists(self):
        # #52: whatever event fires (or doesn't), the receipt decides.
        self.skill_event("grumpy:review")
        self.receipt("review")
        self.skill_event("grumpy:review")
        self.assertIn("grumpy-review", self.gates())

    def test_stop_sweep_records_fresh_receipts_and_never_blocks(self):
        self.receipt("simplify")
        self.receipt("review")
        r = self.hook(SWEEP, {"hook_event_name": "Stop", "cwd": self.repo})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("recorded from receipts", r.stdout)
        self.assertIn("simplify", self.gates())
        self.assertIn("grumpy-review", self.gates())

    def test_stop_sweep_with_nothing_to_do_is_silent(self):
        r = self.hook(SWEEP, {"hook_event_name": "Stop", "cwd": self.repo})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def test_stop_sweep_outside_a_repo_never_blocks(self):
        r = subprocess.run(
            ["python3", SWEEP],
            input=json.dumps({"cwd": tempfile.mkdtemp()}),
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertEqual(r.returncode, 0)


class LegacyCycleTest(ReceiptTestBase):
    def setUp(self):
        super().setUp()
        gs.update_store(self.store, lambda d: gs.init_gates(d, BRANCH, "small-medium"))

    def test_sweep_never_stamps_a_legacy_cycle(self):
        r = self.hook(SWEEP, {"hook_event_name": "Stop", "cwd": self.repo})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.gates(), {})

    def test_verify_on_a_legacy_cycle_says_to_re_init(self):
        self.receipt("review")
        r = self.verify("review")
        self.assertEqual(r.returncode, 1)
        self.assertIn("predates receipts", r.stderr)

    def test_skill_hook_keeps_event_only_recording(self):
        self.hook(
            AUTO,
            {
                "tool_name": "Skill",
                "tool_input": {"skill": "grumpy:review"},
                "tool_response": {},
                "cwd": self.repo,
            },
        )
        self.assertIn("grumpy-review", self.gates())


class EnforceStalenessTest(ReceiptTestBase):
    def test_complete_chain_on_head_allows_the_pr(self):
        self.run_chain()
        r = self.pr_create()
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_commit_after_the_chain_blocks_and_names_stale_gates(self):
        self.run_chain()
        self.commit("after review")
        r = self.pr_create()
        self.assertEqual(r.returncode, 2)
        self.assertIn("stale", r.stderr)
        self.assertIn("grumpy-fix-post-imagine", r.stderr)

    def test_legacy_cycle_is_not_judged_on_staleness(self):
        gs.update_store(self.store, lambda d: gs.init_gates(d, BRANCH, "small-medium"))
        # Skill gates first: the code-mutating ones clear bash gates.
        order = rc.SKILL_GATE_ORDER + gs.BASH_GATES
        for gate in order:
            gs.update_store(
                self.store,
                lambda d, gate=gate: gs.record_gate(d, BRANCH, gate, authorized=True),
            )
        self.commit("after review")
        self.assertEqual(self.pr_create().returncode, 0)


class ReceiptWriterTest(ReceiptTestBase):
    def test_receipt_shape(self):
        self.receipt("review")
        path = rc.receipt_path(self.repo, BRANCH, "grumpy:review")
        with open(path) as f:
            receipt = json.load(f)
        self.assertEqual(
            set(receipt), {"protocol_version", "skill", "branch", "head", "written_at"}
        )
        self.assertEqual(receipt["head"], git(self.repo, "rev-parse", "HEAD"))

    def test_detached_head_is_refused(self):
        git(self.repo, "checkout", "-q", "--detach")
        r = self.run_py(GRUMPY_RECEIPT, "review", "--worktree", self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("detached HEAD", r.stderr)

    def test_unknown_pass_is_refused(self):
        r = self.run_py(GRUMPY_RECEIPT, "audit", "--worktree", self.repo)
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
