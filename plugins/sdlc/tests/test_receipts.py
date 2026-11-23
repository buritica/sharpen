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
AUTO_INIT = os.path.join(SCRIPTS, "auto-init-gate-cycle.py")
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

    def report(self, pass_name, text=None):
        """What the pass writes before its receipt: its report."""
        branch = git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        d = os.path.join(self.repo, ".sharpen", "grumpy", branch)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{pass_name}.md")
        with open(path, "w") as f:
            f.write(text or f"# {pass_name} report {time.time_ns()}\n")
        return path

    def receipt(self, pass_name, report=True):
        if report:
            self.report(pass_name)
            time.sleep(0.01)
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

    def edit_store(self, change):
        with open(self.store) as f:
            data = json.load(f)
        change(data)
        with open(self.store, "w") as f:
            json.dump(data, f)

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

    def test_auto_init_makes_a_receipt_backed_cycle(self):
        store = os.path.join(self.repo, ".sharpen", "data", "auto.json")
        env = dict(self.env, SDLC_GATES_PATH=store)
        r = subprocess.run(
            ["python3", AUTO_INIT],
            input=json.dumps(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "git commit -m x"},
                    "tool_response": {"exit_code": 0},
                    "cwd": self.repo,
                }
            ),
            capture_output=True,
            text=True,
            cwd=self.repo,
            env=env,
        )
        # Exit 2 is the hook's visible "armed a cycle" notice, not a failure.
        self.assertIn("armed", r.stderr)
        with open(store) as f:
            self.assertEqual(json.load(f)[BRANCH].get("evidence"), "receipts")

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

    def test_receipt_for_an_ancestor_records_with_its_own_head(self):
        # A later pass (fix) commits before the sweep runs; the review
        # receipt still counts, stamped with the commit it reviewed.
        self.receipt("review")
        reviewed = git(self.repo, "rev-parse", "HEAD")
        self.commit()
        r = self.verify("review")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.bd()["evidence_heads"]["grumpy-review"], reviewed)

    def test_receipt_for_a_commit_not_on_the_branch_is_refused(self):
        self.receipt("review")
        git(self.repo, "commit", "-q", "--amend", "--allow-empty", "-m", "rewritten")
        r = self.verify("review")
        self.assertEqual(r.returncode, 1)
        self.assertIn("re-run the pass", r.stderr)

    def test_receipt_whose_report_is_gone_is_refused(self):
        self.receipt("review")
        os.unlink(os.path.join(self.repo, ".sharpen", "grumpy", BRANCH, "review.md"))
        r = self.verify("review")
        self.assertEqual(r.returncode, 1)
        self.assertIn("report this receipt names is missing", r.stderr)

    def test_receipt_whose_report_changed_records_with_a_warning(self):
        # Coworkers tidy reports; that is worth a warning, not a stop.
        self.receipt("review")
        self.report("review", text="# tidied after the pass\n")
        r = self.verify("review")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("⚠", r.stderr)
        self.assertIn("changed after the receipt", r.stderr)
        self.assertIn("grumpy-review", self.gates())

    def test_already_recorded_verify_is_not_an_error(self):
        self.receipt("review")
        self.verify("review")
        r = self.verify("review")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nothing left to record", r.stderr)

    def test_verify_all_says_why_a_receipt_on_disk_did_not_count(self):
        self.receipt("review")
        git(self.repo, "commit", "-q", "--amend", "--allow-empty", "-m", "rewritten")
        r = self.verify("all")
        self.assertEqual(r.returncode, 1)
        self.assertIn("re-run the pass", r.stderr)

    def test_verify_fix_before_review_is_not_reported_as_done(self):
        self.receipt("fix")
        r = self.verify("fix")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("nothing left to record", r.stderr)

    def test_late_record_keeps_checks_that_ran_after_the_pass(self):
        # simplify finished and left its receipt; tests/lint/typecheck ran
        # after it; only then does anything record simplify.
        self.receipt("simplify")
        for gate in ("tests", "lint", "typecheck"):
            self.assertEqual(self.cli("--record", gate).returncode, 0)
        self.assertEqual(self.verify("simplify").returncode, 0)
        for gate in ("tests", "lint", "typecheck"):
            self.assertIn(gate, self.gates())

    def test_checks_from_before_the_pass_are_still_cleared(self):
        self.cli("--record", "tests")
        time.sleep(0.01)
        self.receipt("simplify")
        self.assertEqual(self.verify("simplify").returncode, 0)
        self.assertNotIn("tests", self.gates())

    def test_late_review_verify_does_not_strand_the_fix_receipt(self):
        # Review and fix both finished before anything recorded.
        self.receipt("review")
        self.receipt("fix")
        self.assertEqual(self.verify("review").returncode, 0)
        r = self.verify("fix")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("grumpy-fix-post-review", self.gates())

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
        self.assertIn("predates the grumpy-imagine pass", r.stderr)
        self.receipt("fix")
        self.assertEqual(self.verify("fix").returncode, 0)
        self.assertIn("grumpy-fix-post-imagine", self.gates())

    def auto_fix(self):
        self.report("fix")
        time.sleep(0.01)
        r = self.run_py(
            GRUMPY_RECEIPT, "fix", "--stage", "auto", "--worktree", self.repo
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        time.sleep(0.01)

    def test_auto_stage_fixes_record_in_the_agent_chain(self):
        # grumpy's `fix --stage auto` and determine_gate must agree.
        for step in ("review", "fix", "imagine", "fix"):
            self.auto_fix() if step == "fix" else self.receipt(step)
            r = self.verify(step)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("grumpy-fix-post-review", self.gates())
        self.assertIn("grumpy-fix-post-imagine", self.gates())

    def test_auto_stage_fixes_record_after_both_passes(self):
        # The no-agent adapters: review and imagine both recorded first.
        for step in ("review", "imagine"):
            self.receipt(step)
            self.assertEqual(self.verify(step).returncode, 0)
        for gate in ("grumpy-fix-post-imagine", "grumpy-fix-post-review"):
            self.auto_fix()
            r = self.verify("fix")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(gate, self.gates())

    def test_fix_receipt_written_before_review_is_refused(self):
        self.receipt("fix")
        self.receipt("review")
        self.verify("review")
        r = self.verify("fix")
        self.assertEqual(r.returncode, 1)
        self.assertIn("predates the grumpy-review pass", r.stderr)

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


class RoutedVerifyTest(ReceiptTestBase):
    """gate.md's --worktree flow: a session in worktree B drives the chain for
    A's branch. The pass writes its receipt in A; --verify runs from B."""

    def test_verify_from_the_driving_worktree_reads_the_target_receipt(self):
        driver = os.path.join(tempfile.mkdtemp(), "driver")
        git(self.repo, "worktree", "add", "-q", "-b", "feat/driver", driver)
        self.addCleanup(
            subprocess.run,
            ["git", "-C", self.repo, "worktree", "remove", "-f", driver],
            capture_output=True,
        )
        r = self.cli(
            "--init", "small-medium", "--branch", BRANCH, "--route-from", driver
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.receipt("review")  # written in A, for A's branch
        r = subprocess.run(
            ["python3", RECORD, "--verify", "review"],
            capture_output=True,
            text=True,
            cwd=driver,
            env=self.env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("grumpy-review", self.gates())


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

    def test_stop_sweep_records_review_and_fix_after_the_fix_commit(self):
        # The normal chain with no --verify at all: review, then fix commits,
        # then the turn ends.
        self.receipt("review")
        self.commit("fixes")
        self.receipt("fix")
        r = self.hook(SWEEP, {"hook_event_name": "Stop", "cwd": self.repo})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("grumpy-review", self.gates())
        self.assertIn("grumpy-fix-post-review", self.gates())

    def test_stop_sweep_reports_an_unreadable_store(self):
        with open(self.store, "w") as f:
            f.write("{not json")
        self.receipt("review")
        r = self.hook(SWEEP, {"hook_event_name": "Stop", "cwd": self.repo})
        self.assertEqual(r.returncode, 0)
        self.assertIn("sweep stopped", r.stdout)

    def test_stop_sweep_outside_a_receipt_cycle_takes_no_lock(self):
        plain = os.path.realpath(tempfile.mkdtemp())
        r = subprocess.run(
            ["python3", SWEEP],
            input=json.dumps({"cwd": plain}),
            capture_output=True,
            text=True,
            env=dict(os.environ),
        )
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr, "")
        self.assertFalse(os.path.exists(os.path.join(plain, ".sharpen")))

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

    def test_verify_all_on_a_legacy_cycle_is_not_a_silent_success(self):
        self.receipt("review")
        r = self.verify("all")
        self.assertEqual(r.returncode, 1)
        self.assertIn("predates receipts", r.stderr)

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

    def test_earlier_gates_on_older_heads_are_fine_when_the_last_is_on_head(self):
        # Fix passes commit, so real chains have a different head per gate.
        for step in ("simplify", "review"):
            self.receipt(step)
            self.assertEqual(self.verify(step).returncode, 0)
        self.commit("review fixes")
        for step in ("fix", "imagine"):
            self.receipt(step)
            self.assertEqual(self.verify(step).returncode, 0)
        self.commit("imagine fixes")
        self.receipt("fix")
        self.assertEqual(self.verify("fix").returncode, 0)
        for gate in ("tests", "lint", "typecheck"):
            self.cli("--record", gate)
        r = self.pr_create()
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_status_lines_warn_when_enforce_would_block_as_stale(self):
        self.run_chain()
        self.commit("after review")
        r = self.cli("--oneline")
        self.assertIn("Stale", r.stdout)
        self.assertIn("--init small-medium", r.stdout)
        self.assertIn("Stale", self.cli("--status").stdout)

    def test_unattested_headless_last_gate_is_stale(self):
        # A session still running a pre-receipt sdlc stamps at Skill load,
        # with no head: no evidence, so enforce must not wave it through.
        self.run_chain()
        gs.update_store(
            self.store,
            lambda d: d[BRANCH]["evidence_heads"].pop("grumpy-fix-post-imagine"),
        )
        r = self.pr_create()
        self.assertEqual(r.returncode, 2)
        self.assertIn("no receipt", r.stderr)

    def test_attested_last_gate_is_not_judged_stale(self):
        for step in ("simplify", "review", "fix", "imagine"):
            self.receipt(step)
            self.assertEqual(self.verify(step).returncode, 0)
        self.commit("fixes")
        r = self.cli(
            "--attest", "grumpy-fix-post-imagine", "--reason", "receipt step failed"
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        for gate in ("tests", "lint", "typecheck"):
            self.cli("--record", gate)
        self.assertEqual(self.pr_create().returncode, 0)

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


class OtherPrPathsStalenessTest(ReceiptTestBase):
    """Every PR path sits behind the same missing + stale check, not just
    `gh pr create` (area 2). Staleness is gate_receipts' rule, not re-derived."""

    def enforce(self, tool, tool_input):
        return self.hook(
            ENFORCE, {"tool_name": tool, "tool_input": tool_input, "cwd": self.repo}
        )

    def test_mcp_pr_after_a_later_commit_is_stale(self):
        # The MCP call names a repo, and the hook judges the checkout whose
        # remote matches it, so this checkout needs that remote.
        git(self.repo, "remote", "add", "origin", "https://github.com/o/r.git")
        self.run_chain()
        args = {"owner": "o", "repo": "r", "head": BRANCH, "base": "main"}
        self.assertEqual(
            self.enforce("mcp__github__create_pull_request", args).returncode, 0
        )
        self.commit("after review")
        r = self.enforce("mcp__github__create_pull_request", args)
        self.assertEqual(r.returncode, 2)
        self.assertIn("stale", r.stderr)

    def test_gh_pr_new_alias_after_a_later_commit_is_stale(self):
        self.run_chain()
        self.commit("after review")
        r = self.enforce("Bash", {"command": "gh pr new --fill"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("stale", r.stderr)

    def test_pre_push_warns_on_stale_gates_for_the_pushed_commit(self):
        self.run_chain()
        self.commit("after review")
        sha = git(self.repo, "rev-parse", "HEAD")
        line = f"refs/heads/{BRANCH} {sha} refs/heads/{BRANCH} {'0' * 40}\n"
        r = self.run_py(os.path.join(SCRIPTS, "pre-push.py"), stdin=line)
        self.assertEqual(r.returncode, 0)  # warn is the default
        self.assertIn("stale", r.stderr.lower())


class ReceiptWriterTest(ReceiptTestBase):
    def test_receipt_shape(self):
        self.receipt("review")
        path = rc.receipt_path(self.repo, BRANCH, "grumpy:review")
        with open(path) as f:
            receipt = json.load(f)
        self.assertEqual(
            set(receipt),
            {
                "protocol_version",
                "skill",
                "branch",
                "head",
                "written_at",
                "report",
                "report_sha256",
            },
        )
        self.assertEqual(receipt["report"], "review.md")
        self.assertEqual(receipt["head"], git(self.repo, "rev-parse", "HEAD"))

    def test_detached_head_is_refused(self):
        git(self.repo, "checkout", "-q", "--detach")
        r = self.run_py(GRUMPY_RECEIPT, "review", "--worktree", self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("detached HEAD", r.stderr)

    def test_no_report_no_receipt(self):
        # Headless hosts that won't write under .claude/ print the report
        # instead; that pass must not leave a receipt.
        r = self.run_py(GRUMPY_RECEIPT, "review", "--worktree", self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("report not written", r.stderr)
        self.assertFalse(
            os.path.exists(rc.receipt_path(self.repo, BRANCH, "grumpy:review"))
        )

    def test_empty_report_is_refused(self):
        self.report("review", text="   \n")
        r = self.run_py(GRUMPY_RECEIPT, "review", "--worktree", self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("empty", r.stderr)

    def test_report_left_from_the_last_run_is_refused(self):
        self.receipt("review")
        time.sleep(0.01)
        r = self.run_py(GRUMPY_RECEIPT, "review", "--worktree", self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("predates the last receipt", r.stderr)

    def test_dirty_tree_is_refused(self):
        path = os.path.join(self.repo, "f.txt")
        with open(path, "w") as f:
            f.write("a\n")
        git(self.repo, "add", "f.txt")
        r = self.run_py(GRUMPY_RECEIPT, "review", "--worktree", self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("uncommitted changes", r.stderr)

    def test_unknown_pass_is_refused(self):
        r = self.run_py(GRUMPY_RECEIPT, "audit", "--worktree", self.repo)
        self.assertEqual(r.returncode, 1)


def ignore_artifacts(repo):
    """Commit the .gitignore a real repo has, so receipts and the store
    don't count as changes on the branch."""
    with open(os.path.join(repo, ".gitignore"), "w") as f:
        f.write(".sharpen/\n.claude/\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "ignore artifacts")
    git(repo, "branch", "main")


if __name__ == "__main__":
    unittest.main()
