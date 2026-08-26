#!/usr/bin/env python3
"""
Stdlib tests for the JSON-backed SDLC gate system (no pip install).

Covers gate_store logic plus the three hooks + the CLI driven as real
subprocesses with JSON on stdin, asserting exit codes (0 allow, 2 deny).

Run: python3 plugins/sdlc/tests/test_gate.py
"""

import importlib.util
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)

import gate_store as gs  # noqa: E402
import hook_out as ho  # noqa: E402

auto = importlib.util.module_from_spec(  # the hook's filename has dashes
    importlib.util.spec_from_file_location(
        "auto_record", os.path.join(SCRIPTS, "auto-record-skill-gate.py")
    )
)
auto.__spec__.loader.exec_module(auto)

RECORD = os.path.join(SCRIPTS, "record-gate.py")
ENFORCE = os.path.join(SCRIPTS, "enforce-sdlc-gates.py")
BLOCK = os.path.join(SCRIPTS, "block-direct-gate-record.py")
AUTO = os.path.join(SCRIPTS, "auto-record-skill-gate.py")
AUTO_INIT = os.path.join(SCRIPTS, "auto-init-gate-cycle.py")


def git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], capture_output=True, check=True)


def make_repo(branch="feat/x"):
    repo = tempfile.mkdtemp()
    git(repo, "init", "-q", "-b", branch)
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    return repo


def run_hook(script, payload, repo, gates_path):
    env = dict(os.environ, SDLC_GATES_PATH=gates_path)
    return subprocess.run(
        ["python3", script],
        input=json.dumps(payload).encode(),
        capture_output=True,
        cwd=repo,
        env=env,
    )


def run_cli(args, repo, gates_path):
    env = dict(os.environ, SDLC_GATES_PATH=gates_path)
    return subprocess.run(
        ["python3", RECORD, *args], capture_output=True, cwd=repo, env=env
    )


def seed_gate(gates_path, branch, gate):
    # Fixtures that need a skill-gated gate already recorded go through the
    # store the way the auto-record hook does, rather than weakening the guard.
    gs.update_store(
        gates_path, lambda d: gs.record_gate(d, branch, gate, authorized=True)
    )


def stdout_json(tc, r):
    """The hook's stdout payload, asserted to be exactly one JSON object.

    Unknown keys fail the harness's schema validation and take the whole
    payload down with them, so the hook tests check the shape rather than
    just looking for text somewhere in the output."""
    out = r.stdout.decode().strip()
    tc.assertTrue(out, "expected a JSON payload on stdout, got nothing")
    return json.loads(out)


def read_text(path):
    with open(path) as f:
        return f.read()


def read_json(path):
    with open(path) as f:
        return json.load(f)


def clean_env():
    # Drop the SDLC_GATES_PATH pin so default_store_path resolves the store
    # itself (via --git-common-dir) — what the shared-store tests exercise.
    env = dict(os.environ)
    env.pop("SDLC_GATES_PATH", None)
    return env


class StoreTest(unittest.TestCase):
    def test_init_refuses_main(self):
        for b in ("main", "master"):
            with self.assertRaises(ValueError):
                gs.init_gates({}, b, "tiny")

    def test_init_invalid_tier(self):
        with self.assertRaises(ValueError):
            gs.init_gates({}, "feat/x", "huge")

    def test_record_unknown_gate(self):
        d = {}
        gs.init_gates(d, "feat/x", "tiny")
        with self.assertRaises(ValueError):
            gs.record_gate(d, "feat/x", "bogus")

    def test_record_without_cycle(self):
        with self.assertRaises(ValueError):
            gs.record_gate({}, "feat/x", "tests")

    def test_missing_completed(self):
        d = {}
        gs.init_gates(d, "feat/x", "tiny")  # tests, lint, typecheck
        gs.record_gate(d, "feat/x", "tests")
        self.assertEqual(gs.completed_gates(d["feat/x"]), ["tests"])
        self.assertEqual(set(gs.missing_gates(d["feat/x"])), {"lint", "typecheck"})

    def test_determine_gate_fix_is_contextual(self):
        d = {}
        bd = gs.init_gates(d, "feat/x", "small-medium")
        self.assertIsNone(gs.determine_gate("grumpy:fix", bd))
        bd["gates"]["grumpy-review"] = "t"
        self.assertEqual(gs.determine_gate("grumpy:fix", bd), "grumpy-fix-post-review")
        bd["gates"]["grumpy-fix-post-review"] = "t"
        bd["gates"]["grumpy-imagine"] = "t"
        self.assertEqual(gs.determine_gate("grumpy:fix", bd), "grumpy-fix-post-imagine")

    def test_save_load_roundtrip_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "sub", "gates.json")
            d = {}
            gs.init_gates(d, "feat/x", "tiny")
            gs.save_store(p, d)
            self.assertEqual(gs.load_store(p)["feat/x"]["tier"], "tiny")
            # no leftover temp files
            self.assertEqual(os.listdir(os.path.dirname(p)), ["gates.json"])


class HookOutTest(unittest.TestCase):
    """The output convention itself. Reached only through subprocesses
    otherwise, so a mistake in the payload shape or the log prefix would only
    show up as a hook that quietly stopped being read."""

    def test_deny_is_the_documented_pretooluse_shape(self):
        p = ho.deny("because")
        self.assertEqual(list(p), ["hookSpecificOutput"])  # no stray top-level keys
        self.assertEqual(
            p["hookSpecificOutput"],
            {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "because",
            },
        )

    def test_warn_is_a_systemmessage_with_no_decision(self):
        p = ho.warn("enforce", "one", "two")
        # A decision key here would turn a caveat into an auto-approval.
        self.assertEqual(list(p), ["systemMessage"])
        self.assertEqual(p["systemMessage"], "[gate] enforce: one\n[gate] enforce: two")

    def test_notify_writes_prefixed_stderr_and_returns_2(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = ho.notify("auto-init", "something happened")
        self.assertEqual(rc, 2)  # 2 is what makes PostToolUse stderr visible
        self.assertEqual(err.getvalue(), "[gate] auto-init: something happened\n")

    def test_notify_can_write_without_surfacing(self):
        # surface=False still writes the line but returns 0, leaving it in the
        # debug log. Untested, this silently becomes "always visible" and every
        # skill run in an ungated repo starts nagging.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = ho.notify("auto-record", "routine", surface=False)
        self.assertEqual(rc, 0)
        self.assertEqual(err.getvalue(), "[gate] auto-record: routine\n")

    def test_emit_writes_exactly_one_json_object(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ho.emit(ho.warn("enforce", "hi"))
        self.assertEqual(
            json.loads(out.getvalue())["systemMessage"], "[gate] enforce: hi"
        )


class CliTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.gp = os.path.join(self.repo, ".claude", "data", "gates.json")

    def test_init_and_record_roundtrip(self):
        r = run_cli(["--init", "tiny"], self.repo, self.gp)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_cli(["--record", "tests"], self.repo, self.gp)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = read_json(self.gp)
        self.assertIn("tests", data["feat/x"]["gates"])

    def test_reinit_reports_what_it_reset(self):
        run_cli(["--init", "tiny"], self.repo, self.gp)
        run_cli(["--record", "tests"], self.repo, self.gp)
        r = run_cli(["--init", "tiny"], self.repo, self.gp)
        self.assertEqual(r.returncode, 0)
        err = r.stderr.decode()
        self.assertIn("reset 1 recorded gate", err)
        self.assertIn("tests", err)
        # and a first init on a clean branch says nothing about resetting
        fresh = make_repo(branch="feat/fresh")
        gp = os.path.join(fresh, ".claude", "data", "gates.json")
        r = run_cli(["--init", "tiny"], fresh, gp)
        self.assertNotIn("reset", r.stderr.decode())

    def test_record_unknown_gate_exits_nonzero(self):
        run_cli(["--init", "tiny"], self.repo, self.gp)
        r = run_cli(["--record", "nope"], self.repo, self.gp)
        self.assertEqual(r.returncode, 1)

    def test_record_known_gate_not_required_by_tier_via_authorized_call(self):
        # Distinct from an unrecognized gate name entirely: "simplify" is a
        # real gate, just not one `tiny` requires. Unreachable through the
        # plain unauthorized CLI (every bash gate the CLI can record without
        # authorization is required by every tier), so this drives it the
        # way an authorized caller (the auto-record hook) would — same
        # pattern as test_inline_python_cannot_record above.
        run_cli(["--init", "tiny"], self.repo, self.gp)
        env = dict(os.environ, SDLC_GATES_PATH=self.gp, PYTHONPATH=SCRIPTS)
        r = subprocess.run(
            [
                "python3",
                "-c",
                "import gate_store as gs, os;"
                "gs.update_store(os.environ['SDLC_GATES_PATH'],"
                " lambda d: gs.record_gate(d, 'feat/x', 'simplify', authorized=True))",
            ],
            capture_output=True,
            cwd=self.repo,
            env=env,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not required", r.stderr.decode())
        self.assertFalse(read_json(self.gp)["feat/x"]["gates"].get("simplify"))


class EnforceTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.gp = os.path.join(self.repo, ".claude", "data", "gates.json")

    def payload(self, cmd="gh pr create --fill"):
        return {"tool_name": "Bash", "tool_input": {"command": cmd}}

    def test_no_cycle_allows(self):
        r = run_hook(ENFORCE, self.payload(), self.repo, self.gp)
        self.assertEqual(r.returncode, 0)

    def test_incomplete_blocks(self):
        run_cli(["--init", "tiny"], self.repo, self.gp)
        r = run_hook(ENFORCE, self.payload(), self.repo, self.gp)
        self.assertEqual(r.returncode, 2)
        self.assertIn("incomplete", r.stderr.decode())

    def test_complete_allows(self):
        run_cli(["--init", "tiny"], self.repo, self.gp)
        for g in ("tests", "lint", "typecheck"):
            run_cli(["--record", g], self.repo, self.gp)
        r = run_hook(ENFORCE, self.payload(), self.repo, self.gp)
        self.assertEqual(r.returncode, 0, r.stderr)
        # Silence is how a PreToolUse hook defers to the normal permission
        # flow; a payload here would be a decision we never meant to make.
        self.assertEqual(r.stdout.decode(), "")

    def test_non_pr_command_ignored(self):
        run_cli(["--init", "tiny"], self.repo, self.gp)
        r = run_hook(ENFORCE, self.payload("ls -la"), self.repo, self.gp)
        self.assertEqual(r.returncode, 0)

    def test_head_flag_forms_that_are_not_store_keys(self):
        # gh accepts `--head owner:branch` for a fork and a full ref. Neither
        # is how the store is keyed, so left raw they miss the cycle and read
        # as "no cycle -> allow" — a one-flag bypass of the entire gate.
        run_cli(["--init", "tiny"], self.repo, self.gp)
        for head in (
            "someuser:feat/x",
            "refs/heads/feat/x",
            "someuser:refs/heads/feat/x",
        ):
            r = run_hook(
                ENFORCE, self.payload(f"gh pr create --head {head}"), self.repo, self.gp
            )
            self.assertEqual(r.returncode, 2, f"{head} bypassed the gate")

    def test_clustered_short_head_flag_does_not_bypass(self):
        # gh is built on pflag, which accepts `-Hbranch` exactly as it accepts
        # `-H branch`. A reader that only knows the spaced forms sees no flag
        # and falls back to the CWD's branch — so this only proves anything
        # when the two differ: the cycle is on `gated/x` while the checkout
        # sits on ungated `feat/x`. Read wrongly, that's "no cycle -> allow",
        # and the gate is bypassed by one missing space.
        git(self.repo, "branch", "gated/x")
        run_cli(["--init", "tiny", "--branch", "gated/x"], self.repo, self.gp)
        for cmd in ("gh pr create -Hgated/x", "gh pr create -H gated/x"):
            r = run_hook(ENFORCE, self.payload(cmd), self.repo, self.gp)
            self.assertEqual(r.returncode, 2, f"{cmd} bypassed the gate")

    def test_pr_title_starting_with_dash_H_is_not_a_branch(self):
        # The clustered `-Hbranch` form only works if the scan can tell a flag
        # from a value. Read naively, `--title "-Hotfix: crash"` becomes the
        # head branch, its unknown name looks like "no cycle -> allow", and a
        # plausible PR title silently disables the gate.
        run_cli(["--init", "tiny"], self.repo, self.gp)
        for cmd in (
            'gh pr create --title "-Hotfix: crash on boot" -b x',
            'gh pr create -t "-Hmm" -b x',
            'gh pr create -b "-Hbody" --fill',
        ):
            r = run_hook(ENFORCE, self.payload(cmd), self.repo, self.gp)
            self.assertEqual(r.returncode, 2, f"{cmd} bypassed the gate")

    def test_denial_names_the_tier_escape(self):
        # auto-init stamps small-medium on a branch's first commit, so a
        # docs-only change inherits eight gates nobody chose. If the denial
        # doesn't name the way out, the gate is a wall.
        run_cli(["--init", "small-medium"], self.repo, self.gp)
        r = run_hook(ENFORCE, self.payload(), self.repo, self.gp)
        self.assertIn("--init", r.stderr.decode())

    def test_head_flag_branch(self):
        # cycle on feat/x is incomplete; --head feat/x must resolve and block
        run_cli(["--init", "tiny"], self.repo, self.gp)
        r = run_hook(
            ENFORCE, self.payload("gh pr create --head feat/x"), self.repo, self.gp
        )
        self.assertEqual(r.returncode, 2)

    def _seed_main_with_gitignore(self, ignored_name):
        # Branch `main` from the init commit and add a .gitignore that
        # ignores `ignored_name`. Feat/x is rebased/merged so the same
        # .gitignore applies there too — so a file added on feat/x with
        # that name is genuinely ignored across the diff.
        git(self.repo, "branch", "main")
        git(self.repo, "checkout", "-q", "main")
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write(ignored_name + "\n")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "commit", "-q", "-m", "ignore")
        git(self.repo, "checkout", "-q", "feat/x")
        git(self.repo, "merge", "-q", "--no-edit", "main")

    def _commit_file(self, path, force=False):
        with open(os.path.join(self.repo, path), "w") as f:
            f.write("x\n")
        add = ["-f", path] if force else [path]
        git(self.repo, "add", *add)
        git(self.repo, "commit", "-q", "-m", "add " + path)

    def test_gitignored_only_diff_still_blocks(self):
        # Regression guard for a removed escape. Enforce used to ALLOW here —
        # a branch whose entire diff-vs-base is gitignored — and announce the
        # waiver on exit-0 stderr, which nothing reads. PreToolUse has no way
        # to make an allow visible to the agent (see the hook's docstring), so
        # the waiver is gone: an incomplete cycle blocks regardless of what the
        # diff contains. If this ever allows again, the plugin's one job is
        # being opted out of silently.
        self._seed_main_with_gitignore("scratch.txt")
        self._commit_file("scratch.txt", force=True)  # only diff on feat/x
        run_cli(["--init", "tiny"], self.repo, self.gp)  # incomplete cycle
        r = run_hook(
            ENFORCE,
            self.payload(f"cd {self.repo} && gh pr create --fill --base main"),
            self.repo,
            self.gp,
        )
        self.assertEqual(r.returncode, 2, r.stdout.decode())
        self.assertIn("incomplete", r.stderr.decode())

    def enforce_cmd(self, cmd):
        return run_hook(ENFORCE, self.payload(cmd), self.repo, self.gp)

    def test_second_pr_create_is_also_checked(self):
        # One command can open two PRs. Judging only the first lets a gated
        # branch ride through behind a clean one.
        run_cli(["--init", "tiny"], self.repo, self.gp)
        git(self.repo, "branch", "done/branch")
        for g in ("tests", "lint", "typecheck"):
            run_cli(["--record", g, "--branch", "done/branch"], self.repo, self.gp)
        r = self.enforce_cmd(
            "gh pr create --head done/branch && gh pr create --head feat/x"
        )
        self.assertEqual(r.returncode, 2, r.stderr.decode())
        self.assertIn("feat/x", r.stderr.decode())

    def test_denial_uses_the_documented_pretooluse_payload(self):
        # The old form was `{"decision": "deny"}` on stderr, which the docs
        # don't define — it worked only because exit 2 blocks on its own, and
        # the agent read a raw JSON blob instead of the reason. Deny now goes
        # out as the documented stdout payload, with exit 2 kept underneath so
        # the gate still fails closed if the payload is ever rejected.
        run_cli(["--init", "tiny"], self.repo, self.gp)
        r = self.enforce_cmd("gh pr create --fill")
        self.assertEqual(r.returncode, 2)
        hso = stdout_json(self, r)["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn("incomplete", hso["permissionDecisionReason"])
        self.assertIn("/sdlc:gate", hso["permissionDecisionReason"])

    def test_repeated_caveat_is_reported_once(self):
        # Two PR creates behind the same unresolvable `cd` produce the same
        # caveat twice. Without the dedup in check_gates the user gets the
        # identical sentence printed back to back, which reads like two
        # separate problems.
        r = self.enforce_cmd(
            "cd /nope/not/a/dir && gh pr create --fill && gh pr create --fill"
        )
        self.assertEqual(r.returncode, 0)
        msg = stdout_json(self, r)["systemMessage"]
        self.assertEqual(msg.count("different repo"), 1, msg)

    def test_unresolvable_workdir_caveat_rides_the_denial(self):
        # `cd` to a directory that doesn't resolve: gates get checked against
        # this process's cwd, so the verdict may be about a different repo.
        # When the verdict is a denial, the caveat travels inside the reason —
        # the agent is about to act on that verdict and needs to know.
        run_cli(["--init", "tiny"], self.repo, self.gp)
        r = self.enforce_cmd("cd /nope/not/a/dir && gh pr create --fill")
        self.assertEqual(r.returncode, 2)
        reason = stdout_json(self, r)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("different repo", reason)
        self.assertIn("incomplete", reason)

    def test_unresolvable_workdir_caveat_surfaces_on_an_allow(self):
        # Same caveat, but nothing to deny (no cycle). There is no
        # agent-visible channel for an allow, so it goes to the user via
        # systemMessage rather than to exit-0 stderr, where it was invisible
        # to everyone.
        r = self.enforce_cmd("cd /nope/not/a/dir && gh pr create --fill")
        self.assertEqual(r.returncode, 0)
        payload = stdout_json(self, r)
        self.assertNotIn("hookSpecificOutput", payload)  # still no decision
        self.assertIn("different repo", payload["systemMessage"])

    def test_unparseable_stdin_surfaces_to_the_user(self):
        # A systematically broken hook allows everything. That has to be
        # visible somewhere the user actually looks.
        r = subprocess.run(
            ["python3", ENFORCE],
            input=b"{not json",
            capture_output=True,
            cwd=self.repo,
            env=dict(os.environ, SDLC_GATES_PATH=self.gp),
        )
        self.assertEqual(r.returncode, 0)
        self.assertIn("not enforcing", stdout_json(self, r)["systemMessage"])

    def test_unexpected_error_allows_but_says_so(self):
        # The fail-open branch: a bug in the enforcer must not wedge every Bash
        # call, so it allows — which makes it the single most dangerous silent
        # path in the plugin. It has to make a sound.
        driver = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('e', {ENFORCE!r})\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n"
            "def boom(*a, **k):\n"
            "    raise RuntimeError('enforcer bug')\n"
            "m.check_gates = boom\n"
            "sys.exit(m.main())\n"
        )
        r = subprocess.run(
            ["python3", "-c", driver],
            input=json.dumps(self.payload()).encode(),
            capture_output=True,
            cwd=self.repo,
            env=dict(os.environ, SDLC_GATES_PATH=self.gp, PYTHONPATH=SCRIPTS),
        )
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        msg = stdout_json(self, r)["systemMessage"]
        self.assertIn("NOT enforcing", msg)
        self.assertIn("enforcer bug", msg)

    def test_crew_actor_emits_structured_sdlc_state(self):
        # When SDLC_ACTOR=crew, denial payload includes the structured state
        # so headless dispatchers can parse missing gates instead of prose.
        run_cli(["--init", "small-medium"], self.repo, self.gp)
        env = dict(os.environ, SDLC_GATES_PATH=self.gp, SDLC_ACTOR="crew")
        r = subprocess.run(
            ["python3", ENFORCE],
            input=json.dumps(self.payload()).encode(),
            capture_output=True,
            cwd=self.repo,
            env=env,
        )
        self.assertEqual(r.returncode, 2)
        # Parse the LAST non-empty line, which is the documented contract:
        # gate_store may have written a prose warning to this same stream
        # first, and json.load() over all of stderr would die on its first
        # character. If this stops holding, headless dispatchers break exactly
        # when the repo layout is unusual — i.e. when they need it most.
        payload = json.loads(r.stderr.decode().strip().splitlines()[-1])
        self.assertEqual(payload["decision"], "deny")
        self.assertIn("sdlc_state", payload)
        # The structured state rides stderr, not the stdout payload: unknown
        # keys there fail schema validation and take the denial down with them.
        # So crew mode must still emit a clean, documented deny on stdout.
        hso = stdout_json(self, r)["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertNotIn("sdlc_state", r.stdout.decode())
        state = payload["sdlc_state"]
        self.assertEqual(state["branch"], "feat/x")
        self.assertEqual(state["tier"], "small-medium")
        self.assertIsInstance(state["missing"], list)
        self.assertIn("simplify", state["missing"])

    def test_interactive_actor_gets_prose_on_stderr(self):
        # Default (non-crew) actor gets prose, not a JSON blob: exit 2 puts
        # stderr in front of the agent verbatim, so it should read as the
        # reason. The structured state stays a crew opt-in.
        run_cli(["--init", "small-medium"], self.repo, self.gp)
        r = run_hook(ENFORCE, self.payload(), self.repo, self.gp)
        self.assertEqual(r.returncode, 2)
        err = r.stderr.decode()
        self.assertIn("incomplete", err)
        self.assertNotIn("sdlc_state", err)
        with self.assertRaises(ValueError):
            json.loads(err)


class BlockDirectTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.gp = os.path.join(self.repo, ".claude", "data", "gates.json")

    def hook(self, cmd):
        return run_hook(
            BLOCK,
            {"tool_name": "Bash", "tool_input": {"command": cmd}},
            self.repo,
            self.gp,
        )

    def test_blocks_skill_gated_with_the_documented_payload(self):
        # The plugin's two blocking hooks must ship ONE deny contract. If this
        # drifts from EnforceTest's equivalent, the next change to the payload
        # shape only gets made in one of them.
        r = self.hook("python3 record-gate.py --record grumpy-review")
        self.assertEqual(r.returncode, 2)
        hso = stdout_json(self, r)["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn("/grumpy:review", hso["permissionDecisionReason"])
        # ...and stderr is that same prose, not a JSON blob for the agent to
        # read. assertRaises alone would also pass on empty stderr, so assert
        # the prose is actually there.
        self.assertIn("/grumpy:review", r.stderr.decode())
        with self.assertRaises(ValueError):
            json.loads(r.stderr.decode())

    def test_allows_bash_gates(self):
        for g in ("tests", "lint", "typecheck"):
            r = self.hook(f"python3 record-gate.py --record {g}")
            self.assertEqual(r.returncode, 0, g)
            self.assertEqual(r.stdout.decode(), "", g)  # no decision on an allow

    def test_ignores_unrelated(self):
        self.assertEqual(self.hook("echo hi").returncode, 0)


class AutoRecordTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.gp = os.path.join(self.repo, ".claude", "data", "gates.json")
        run_cli(["--init", "small-medium"], self.repo, self.gp)

    def skill(self, name):
        return run_hook(
            AUTO,
            {"tool_name": "Skill", "tool_input": {"skill": name}},
            self.repo,
            self.gp,
        )

    def test_records_grumpy_review(self):
        r = self.skill("grumpy:review")
        self.assertEqual(r.returncode, 0)
        data = read_json(self.gp)
        self.assertIn("grumpy-review", data["feat/x"]["gates"])
        # A stamp on the LOCAL branch names no branch — saying "on feat/x"
        # while standing on feat/x is noise.
        self.assertNotIn(" on ", r.stderr.decode())

    def test_untracked_skill_noop(self):
        before = read_text(self.gp)
        self.skill("some:thing")
        self.assertEqual(read_text(self.gp), before)


class StoreEdgeTest(unittest.TestCase):
    def test_reinit_resets_gates_and_tier(self):
        d = {}
        gs.init_gates(d, "feat/x", "small-medium")
        gs.record_gate(d, "feat/x", "tests")
        gs.init_gates(d, "feat/x", "tiny")  # re-init
        self.assertEqual(d["feat/x"]["tier"], "tiny")
        self.assertEqual(d["feat/x"]["gates"], {})  # timestamps wiped

    def test_tier_reason_is_stored_when_given(self):
        d = {}
        bd = gs.init_gates(
            d, "feat/x", "tiny", tier_reason="docs-only diff auto-detected"
        )
        self.assertEqual(bd["tier_reason"], "docs-only diff auto-detected")

    def test_tier_reason_is_omitted_when_not_given(self):
        # Manual --init (no reason passed) must not add the key at all —
        # absence is how a reader tells "manually chosen" from "auto-detected",
        # and it keeps every pre-existing store byte-identical to before this
        # field existed.
        d = {}
        bd = gs.init_gates(d, "feat/x", "small-medium")
        self.assertNotIn("tier_reason", bd)

    def test_reinit_replaces_the_previous_tier_reason(self):
        d = {}
        gs.init_gates(d, "feat/x", "tiny", tier_reason="docs-only diff auto-detected")
        bd = gs.init_gates(d, "feat/x", "small-medium")  # manual re-init, no reason
        self.assertNotIn("tier_reason", bd)

    def test_branch_isolation(self):
        d = {}
        gs.init_gates(d, "feat/a", "tiny")
        gs.init_gates(d, "feat/b", "tiny")
        gs.record_gate(d, "feat/a", "tests")
        self.assertIn("tests", d["feat/a"]["gates"])
        self.assertEqual(d["feat/b"]["gates"], {})  # unaffected

    def test_format_status_shows_tier_reason_when_present(self):
        d = {}
        bd = gs.init_gates(
            d, "feat/x", "tiny", tier_reason="docs-only diff auto-detected"
        )
        self.assertIn("docs-only diff auto-detected", gs.format_status(bd, "feat/x"))

    def test_format_status_omits_reason_line_when_absent(self):
        d = {}
        bd = gs.init_gates(d, "feat/x", "small-medium")
        self.assertNotIn("reason", gs.format_status(bd, "feat/x").lower())

    def test_determine_gate_none_when_nothing_or_all_done(self):
        d = {}
        bd = gs.init_gates(d, "feat/x", "small-medium")
        self.assertIsNone(gs.determine_gate("grumpy:fix", bd))  # nothing recorded
        bd["gates"]["grumpy-review"] = "t"
        bd["gates"]["grumpy-fix-post-review"] = "t"
        bd["gates"]["grumpy-imagine"] = "t"
        bd["gates"]["grumpy-fix-post-imagine"] = "t"
        self.assertIsNone(gs.determine_gate("grumpy:fix", bd))  # all done

    def test_determine_gate_fix_prefers_post_imagine_when_both_pending(self):
        d = {}
        bd = gs.init_gates(d, "feat/x", "small-medium")
        bd["gates"]["grumpy-review"] = "t"
        bd["gates"]["grumpy-imagine"] = "t"  # both fixes pending
        self.assertEqual(gs.determine_gate("grumpy:fix", bd), "grumpy-fix-post-imagine")

    def test_load_corrupt_raises_missing_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "gates.json")
            self.assertEqual(gs.load_store(p), {})  # missing -> {}
            with open(p, "w") as f:
                f.write("{not json")
            with self.assertRaises(gs.StoreCorruptError):
                gs.load_store(p)

    def test_update_store_does_not_overwrite_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "gates.json")
            with open(p, "w") as f:
                f.write("garbage{")
            with self.assertRaises(gs.StoreCorruptError):
                gs.update_store(p, lambda d: gs.init_gates(d, "feat/x", "tiny"))
            self.assertEqual(read_text(p), "garbage{")  # untouched

    def test_bare_relative_path_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                gs.save_store("gates.json", {"feat/x": {"tier": "tiny", "gates": {}}})
                self.assertTrue(os.path.exists(os.path.join(tmp, "gates.json")))
            finally:
                os.chdir(cwd)


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_records_do_not_lose_updates(self):
        repo = make_repo()
        gp = os.path.join(repo, ".claude", "data", "gates.json")
        run_cli(["--init", "small-medium"], repo, gp)
        gates = ["tests", "lint", "typecheck"]
        env = dict(os.environ, SDLC_GATES_PATH=gp)
        procs = [
            subprocess.Popen(
                ["python3", RECORD, "--record", g],
                cwd=repo,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for g in gates
        ]
        for p in procs:
            p.wait()
        recorded = read_json(gp)["feat/x"]["gates"]
        for g in gates:
            self.assertIn(g, recorded, f"{g} lost under concurrency")


class EnforceEdgeTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.gp = os.path.join(self.repo, ".claude", "data", "gates.json")

    def enforce(self, cmd):
        return run_hook(
            ENFORCE,
            {"tool_name": "Bash", "tool_input": {"command": cmd}},
            self.repo,
            self.gp,
        )

    def test_corrupt_store_fails_closed(self):
        run_cli(["--init", "tiny"], self.repo, self.gp)
        with open(self.gp, "w") as f:
            f.write("{ broken")
        r = self.enforce("gh pr create --fill")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unreadable", r.stderr.decode())

    def test_echo_of_command_not_blocked(self):
        run_cli(["--init", "tiny"], self.repo, self.gp)
        # incomplete cycle, but this is a string literal, not the command
        r = self.enforce('echo "gh pr create"')
        self.assertEqual(r.returncode, 0)

    def test_head_equals_and_short_flag(self):
        run_cli(["--init", "tiny"], self.repo, self.gp)
        for cmd in ("gh pr create --head=feat/x", "gh pr create -H feat/x"):
            r = self.enforce(cmd)
            self.assertEqual(r.returncode, 2, cmd)

    def test_wrapped_pr_create_still_blocked(self):
        # BYPASS regression: wrapping the command in an interpreter hid it from
        # the old raw-string regex, so the gate could be skipped by typing
        # `bash -c "gh pr create"`. Detection is argv-based now.
        run_cli(["--init", "tiny"], self.repo, self.gp)
        for cmd in (
            'bash -c "gh pr create --fill"',
            'bash -lc "gh pr create --fill"',  # clustered short flags
            "sh -c 'gh pr create'",
            'eval "gh pr create"',
            "(gh pr create --fill)",
            "true\ngh pr create --fill",
            "git status # check\ngh pr create --fill",  # comment before newline
            "timeout 60 gh pr create --fill",
        ):
            r = self.enforce(cmd)
            # "incomplete" and not just exit 2: a denial for the wrong reason
            # (undetectable branch) would otherwise look like a pass.
            self.assertEqual(r.returncode, 2, cmd)
            self.assertIn("incomplete", r.stderr.decode(), cmd)

    def test_head_flag_read_from_the_pr_create_segment_only(self):
        # A --head belonging to a neighboring command must not decide which
        # branch is gated. feat/x is incomplete → still blocked.
        run_cli(["--init", "tiny"], self.repo, self.gp)
        r = self.enforce("gh pr list --head feat/other && gh pr create --fill")
        self.assertEqual(r.returncode, 2, r.stderr.decode())
        self.assertIn("incomplete", r.stderr.decode())

    def test_pr_body_heredoc_mentioning_command_not_confused(self):
        # A heredoc body is data. The body line starts AT COLUMN 0 with the
        # command on purpose: indented or mid-sentence it would be rejected on
        # argv[0] anyway, and the test would pass with heredoc handling deleted.
        run_cli(["--init", "tiny"], self.repo, self.gp)
        r = self.enforce(
            "gh pr create --body-file - <<'EOF'\ngit commit -m 'do this'\nEOF"
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("incomplete", r.stderr.decode())

    def test_small_medium_post_imagine_missing_blocks(self):
        # the #1069 case: everything but the final grumpy fix
        run_cli(["--init", "small-medium"], self.repo, self.gp)
        for g in (
            "tests",
            "simplify",
            "grumpy-review",
            "grumpy-fix-post-review",
            "grumpy-imagine",
            "lint",
            "typecheck",
        ):
            if g in gs.SKILL_FOR_GATE:
                seed_gate(self.gp, "feat/x", g)
            else:
                run_cli(["--record", g], self.repo, self.gp)
        # the fixture must really be one gate short, not seven
        self.assertEqual(
            gs.missing_gates(read_json(self.gp)["feat/x"]), ["grumpy-fix-post-imagine"]
        )
        r = self.enforce("gh pr create --fill")
        self.assertEqual(r.returncode, 2)
        self.assertIn("grumpy-fix-post-imagine", r.stderr.decode())


class CliStatusTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.gp = os.path.join(self.repo, ".claude", "data", "gates.json")

    def test_status_and_oneline(self):
        run_cli(["--init", "tiny"], self.repo, self.gp)
        run_cli(["--record", "tests"], self.repo, self.gp)
        st = run_cli(["--status"], self.repo, self.gp)
        self.assertEqual(st.returncode, 0)
        out = st.stdout.decode()
        self.assertIn("✓ tests", out)
        self.assertIn("✗ lint", out)
        one = run_cli(["--oneline"], self.repo, self.gp)
        self.assertIn("SDLC tiny:", one.stdout.decode())

    def test_init_refuses_main_via_cli(self):
        repo = make_repo(branch="main")
        gp = os.path.join(repo, ".claude", "data", "gates.json")
        r = run_cli(["--init", "tiny"], repo, gp)
        self.assertEqual(r.returncode, 1)


class BlockDirectEdgeTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.gp = os.path.join(self.repo, ".claude", "data", "gates.json")

    def hook(self, cmd):
        return run_hook(
            BLOCK,
            {"tool_name": "Bash", "tool_input": {"command": cmd}},
            self.repo,
            self.gp,
        )

    def test_blocks_equals_syntax(self):
        r = self.hook("python3 record-gate.py --record=grumpy-imagine")
        self.assertEqual(r.returncode, 2)

    def test_blocks_every_skill_gated_gate(self):
        for g in (
            "simplify",
            "grumpy-review",
            "grumpy-fix-post-review",
            "grumpy-imagine",
            "grumpy-fix-post-imagine",
        ):
            r = self.hook(f"python3 record-gate.py --record {g}")
            self.assertEqual(r.returncode, 2, g)

    def test_allows_init_and_status(self):
        for cmd in (
            "python3 record-gate.py --init tiny",
            "python3 record-gate.py --status",
            # the post-skill verification step gate.md prescribes
            'python3 record-gate.py --oneline --branch "$BRANCH"',
        ):
            self.assertEqual(self.hook(cmd).returncode, 0, cmd)

    def test_echo_of_record_command_not_blocked(self):
        # string literal, not a real invocation — must not be denied
        r = self.hook('echo "run record-gate.py --record simplify"')
        self.assertEqual(r.returncode, 0)

    def test_blocks_real_invocation_at_command_boundary(self):
        r = self.hook("cd /tmp && python3 record-gate.py --record grumpy-review")
        self.assertEqual(r.returncode, 2)

    def test_blocks_wrapped_invocation(self):
        # BYPASS regression: same wrapping trick as the enforce hook's.
        for cmd in (
            'bash -c "python3 record-gate.py --record grumpy-review"',
            "eval 'record-gate.py --record simplify'",
            "python3 /abs/path/record-gate.py --record grumpy-imagine",
        ):
            self.assertEqual(self.hook(cmd).returncode, 2, cmd)

    def test_blocks_skill_gate_chained_behind_an_allowed_one(self):
        # Only the first --record used to be inspected, so a skill-gated stamp
        # could ride along behind a manually-recordable one.
        r = self.hook(
            "record-gate.py --record lint && record-gate.py --record simplify"
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("simplify", r.stderr.decode())

    def test_blocks_recorder_under_every_name(self):
        # The name match must not narrow what the old pattern caught: a future
        # .sh/.mjs shim would silently lose enforcement.
        for name in (
            "record-gate.py",
            "record-gate.ts",
            "record-gate",
            "record-gate.sh",
        ):
            r = self.hook(f"{name} --record simplify")
            self.assertEqual(r.returncode, 2, name)

    def test_malformed_payload_does_not_crash(self):
        # A non-str command used to raise TypeError out of main() — exit 1 with
        # a traceback in the user's face on every Bash call. Hooks degrade to
        # "not blocking" with a breadcrumb; they never crash.
        for payload in (
            {"tool_name": "Bash", "tool_input": {"command": 123}},
            {"tool_name": "Bash", "tool_input": None},
            {"tool_name": "Bash"},
        ):
            r = run_hook(BLOCK, payload, self.repo, self.gp)
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            self.assertNotIn("Traceback", r.stderr.decode())

    def test_prefilter_does_not_change_the_verdict(self):
        # The `"record-gate" not in command` short-circuit must only skip work.
        # A recorder invocation always contains the stem, so anything it skips
        # is genuinely not a recorder call.
        for cmd in ("gate-record.py --record simplify", "echo --record simplify"):
            self.assertEqual(self.hook(cmd).returncode, 0, cmd)

    def test_heredoc_body_mentioning_record_not_blocked(self):
        # Writing a doc/PR body that quotes the command is not running it. The
        # body line starts at column 0 with the command, so this fails if
        # heredoc stripping is removed rather than passing on argv[0] alone.
        r = self.hook(
            "gh pr create --body-file - <<'EOF'\nrecord-gate.py --record simplify\nEOF"
        )
        self.assertEqual(r.returncode, 0, r.stderr.decode())


class AutoRecordEdgeTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.gp = os.path.join(self.repo, ".claude", "data", "gates.json")

    def skill(self, name):
        return run_hook(
            AUTO,
            {"tool_name": "Skill", "tool_input": {"skill": name}},
            self.repo,
            self.gp,
        )

    def test_no_cycle_does_not_create_store(self):
        # gate-tracked skill, but no cycle initialized -> must not write a file
        self.skill("grumpy:review")
        self.assertFalse(os.path.exists(self.gp))

    def test_grumpy_fix_records_post_review(self):
        run_cli(["--init", "small-medium"], self.repo, self.gp)
        self.skill("grumpy:review")
        self.skill("grumpy:fix")
        gates = read_json(self.gp)["feat/x"]["gates"]
        self.assertIn("grumpy-fix-post-review", gates)

    def test_idempotent_second_fire_noop(self):
        run_cli(["--init", "small-medium"], self.repo, self.gp)
        self.skill("grumpy:review")
        first = read_json(self.gp)["feat/x"]["gates"]["grumpy-review"]
        self.skill("grumpy:review")  # second fire
        second = read_json(self.gp)["feat/x"]["gates"]["grumpy-review"]
        self.assertEqual(first, second)  # not rewritten


class AutoInitTest(unittest.TestCase):
    """auto-init-gate-cycle.py: PostToolUse Bash hook that initializes a gate
    cycle on the first git commit to a non-default branch."""

    def setUp(self):
        self.repo = make_repo()
        self.gp = os.path.join(self.repo, ".claude", "data", "gates.json")

    def _commit(
        self,
        cmd="git commit --allow-empty -m 'x'",
        repo=None,
        gp=None,
        extra_env=None,
    ):
        payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
        env = dict(os.environ, SDLC_GATES_PATH=gp or self.gp)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["python3", AUTO_INIT],
            input=json.dumps(payload).encode(),
            capture_output=True,
            cwd=repo or self.repo,
            env=env,
        )

    def _write_manifest(self, capabilities):
        path = os.path.join(self.repo, "capabilities.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "protocol_version": "1",
                    "provider": {"name": "claude-code"},
                    "capabilities": capabilities,
                },
                f,
            )
        return path

    def test_failed_commit_stamps_nothing_and_claims_nothing(self):
        # PostToolUse fires on failed tool calls too. Every message this hook
        # emits opens with "Your commit succeeded" — asserting that over a
        # commit that didn't land is a flat lie on the one channel the hook
        # uses to be believed, and it would gate work that doesn't exist.
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m 'x'"},
            "tool_response": {"is_error": True, "stdout": "nothing to commit"},
        }
        r = run_hook(AUTO_INIT, payload, self.repo, self.gp)
        # Visible, not silent: `git commit && git push` reports ONE error for
        # two commands, so the commit may well have landed. Skipping quietly
        # there is the plugin's worst outcome — a branch that looks gated and
        # isn't. And nothing claims the commit succeeded.
        self.assertEqual(r.returncode, 2, r.stderr.decode())
        self.assertIn("reported an error", r.stderr.decode())
        self.assertNotIn("succeeded", r.stderr.decode())
        self.assertFalse(os.path.exists(self.gp))

    def test_failed_non_commit_stays_quiet(self):
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
            "tool_response": {"is_error": True},
        }
        r = run_hook(AUTO_INIT, payload, self.repo, self.gp)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr.decode(), "")

    def test_git_commit_inits_small_medium_cycle(self):
        r = self._commit()
        # Exit 2, not 0: arming a cycle tells the reader gates 2-6 need the
        # skills and to re-init as tiny if the change qualifies. That is a
        # request to act, and at exit 0 no reader ever receives it. Fires at
        # most once per branch, and does not block the commit.
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("already ran", r.stderr.decode())
        data = read_json(self.gp)
        self.assertIn("feat/x", data)
        self.assertEqual(data["feat/x"]["tier"], "small-medium")
        self.assertNotIn("profile", data["feat/x"])

    def test_git_commit_with_capability_manifest_stores_resolved_profile(self):
        manifest = self._write_manifest(
            ["test", "lint", "typecheck", "review", "imagine", "fix"]
        )
        r = self._commit(extra_env={"SDLC_CAPABILITIES_PATH": manifest})
        self.assertEqual(r.returncode, 2, r.stderr.decode())
        self.assertIn("portable profile adversarial", r.stderr.decode())
        cycle = read_json(self.gp)["feat/x"]
        self.assertEqual(cycle["tier"], "small-medium")
        self.assertEqual(cycle["profile"], "adversarial")
        self.assertEqual(
            cycle["capabilities"],
            ["fix", "imagine", "lint", "review", "test", "typecheck"],
        )

    def test_capability_manifest_does_not_rewrite_existing_cycle(self):
        first = self._commit()
        self.assertEqual(first.returncode, 2, first.stderr.decode())
        before = read_json(self.gp)["feat/x"]
        manifest = self._write_manifest(["test", "lint", "typecheck"])
        second = self._commit(extra_env={"SDLC_CAPABILITIES_PATH": manifest})
        self.assertEqual(second.returncode, 0, second.stderr.decode())
        self.assertEqual(read_json(self.gp)["feat/x"], before)

    def test_malformed_capability_manifest_preserves_legacy_auto_init(self):
        manifest = os.path.join(self.repo, "bad-capabilities.json")
        with open(manifest, "w", encoding="utf-8") as f:
            f.write("not json")
        r = self._commit(extra_env={"SDLC_CAPABILITIES_PATH": manifest})
        self.assertEqual(r.returncode, 2, r.stderr.decode())
        cycle = read_json(self.gp)["feat/x"]
        self.assertEqual(cycle["tier"], "small-medium")
        self.assertNotIn("profile", cycle)
        self.assertNotIn("capabilities", cycle)

    def _repo_off_main(self, branch="feat/docs"):
        # make_repo(branch="main") already does init/config/an initial commit
        # — these tests only need a real divergence point on top of that,
        # since _pick_tier auto-downgrades only when it can confidently diff
        # against main/master.
        repo = make_repo(branch="main")
        git(repo, "checkout", "-q", "-b", branch)
        return repo

    def test_docs_only_diff_auto_inits_tiny(self):
        # `_commit` only simulates the PostToolUse payload — it never actually
        # runs the command — so the real commit has to land first for the
        # hook's own `git diff` to see it.
        repo = self._repo_off_main()
        gp = os.path.join(repo, ".claude", "data", "gates.json")
        with open(os.path.join(repo, "docs.md"), "w") as f:
            f.write("more docs\n")
        git(repo, "add", "docs.md")
        git(repo, "commit", "-q", "-m", "docs")
        r = self._commit(cmd="git commit -q -m docs", repo=repo, gp=gp)
        self.assertEqual(r.returncode, 2, r.stderr)
        data = read_json(gp)
        self.assertEqual(data["feat/docs"]["tier"], "tiny")
        # Provenance: a reader six months later shouldn't have to re-derive
        # "why tiny" from git history that's moved on since.
        self.assertIn("docs-only", data["feat/docs"]["tier_reason"])

    def test_manual_init_via_cli_has_no_tier_reason(self):
        # Absence of the key is how a reader tells "manually chosen" apart
        # from "auto-detected" — a plain --init must not fabricate one.
        r = run_cli(["--init", "small-medium"], self.repo, self.gp)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = read_json(self.gp)
        self.assertNotIn("tier_reason", data["feat/x"])

    def test_mixed_diff_stays_small_medium(self):
        # One non-docs file in the diff must not qualify — conservative on
        # purpose, since under-classifying silently skips gates 2-6.
        repo = self._repo_off_main()
        gp = os.path.join(repo, ".claude", "data", "gates.json")
        with open(os.path.join(repo, "docs.md"), "w") as f:
            f.write("docs\n")
        with open(os.path.join(repo, "app.py"), "w") as f:
            f.write("print(1)\n")
        git(repo, "add", "docs.md", "app.py")
        git(repo, "commit", "-q", "-m", "mixed")
        r = self._commit(cmd="git commit -q -m mixed", repo=repo, gp=gp)
        self.assertEqual(r.returncode, 2, r.stderr)
        data = read_json(gp)
        self.assertEqual(data["feat/docs"]["tier"], "small-medium")
        self.assertEqual(
            data["feat/docs"]["tier_reason"], "default (diff includes non-docs files)"
        )

    def test_no_default_branch_to_diff_against_stays_small_medium(self):
        # No main/master anywhere (as in the plain feat/x fixture every other
        # test in this class uses) — can't confirm docs-only, so the safe
        # default holds. This is the existing behavior, asserted explicitly
        # so a future change to the diff logic can't silently start guessing.
        self._commit()
        data = read_json(self.gp)
        self.assertEqual(data["feat/x"]["tier"], "small-medium")
        self.assertEqual(
            data["feat/x"]["tier_reason"],
            "default (no default branch found to diff against)",
        )

    def test_prefers_origin_main_over_local_main_when_they_diverge(self):
        # The exact scenario _merge_base's docstring exists to protect
        # against: local main is ahead of origin/main (nobody fetches before
        # every commit). If local main were preferred, a docs-only commit on
        # top of a LOCAL-ONLY non-docs commit would misclassify as tiny —
        # origin/main doesn't know about that non-docs commit yet, so basing
        # the diff on it correctly pulls it into the comparison.
        repo = make_repo(branch="main")  # init commit: README.md only
        git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        with open(os.path.join(repo, "app.py"), "w") as f:
            f.write("print(1)\n")
        git(repo, "add", "app.py")
        git(repo, "commit", "-q", "-m", "app")  # local main only, origin/main unaware
        git(repo, "checkout", "-q", "-b", "feat/docs")
        gp = os.path.join(repo, ".claude", "data", "gates.json")
        with open(os.path.join(repo, "docs.md"), "w") as f:
            f.write("docs\n")
        git(repo, "add", "docs.md")
        git(repo, "commit", "-q", "-m", "docs")
        r = self._commit(cmd="git commit -q -m docs", repo=repo, gp=gp)
        self.assertEqual(r.returncode, 2, r.stderr)
        data = read_json(gp)
        self.assertEqual(data["feat/docs"]["tier"], "small-medium")

    def test_discovers_non_main_default_branch_via_origin_head(self):
        # A repo whose default branch isn't main/master (trunk, develop, ...)
        # would otherwise never resolve a merge-base at all — the auto-tiny
        # feature would silently never fire for it. origin/HEAD's symbolic
        # ref names the real default branch when it's set.
        repo = make_repo(branch="trunk")
        git(repo, "update-ref", "refs/remotes/origin/trunk", "HEAD")
        git(
            repo,
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/trunk",
        )
        git(repo, "checkout", "-q", "-b", "feat/docs")
        gp = os.path.join(repo, ".claude", "data", "gates.json")
        with open(os.path.join(repo, "docs.md"), "w") as f:
            f.write("docs\n")
        git(repo, "add", "docs.md")
        git(repo, "commit", "-q", "-m", "docs")
        r = self._commit(cmd="git commit -q -m docs", repo=repo, gp=gp)
        self.assertEqual(r.returncode, 2, r.stderr)
        data = read_json(gp)
        self.assertEqual(data["feat/docs"]["tier"], "tiny")

    def test_diff_failure_after_a_resolved_merge_base_gets_its_own_reason(self):
        # Regression: _pick_tier used to collapse "no default branch found"
        # and "found one, but the diff itself failed" into the same reason
        # string — factually wrong for the second case. Simulated at the
        # _git_output level since a real repo can't easily make `git diff`
        # fail right after `git merge-base` succeeds.
        spec = importlib.util.spec_from_file_location("ai3", AUTO_INIT)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)

        def fake_git_output(args, cwd=None):
            return "deadbeef" if args[0] == "merge-base" else None

        m._git_output = fake_git_output
        tier, reason = m._pick_tier(self.repo)
        self.assertEqual(tier, "small-medium")
        self.assertIn("git failure", reason)
        self.assertNotIn("no default branch found", reason)

    def test_no_branch_detected_exits_2_so_the_warning_is_seen(self):
        # A PostToolUse hook's stderr only reaches the model on exit 2; on
        # exit 0 it goes to the debug log. No cycle stamped means the later
        # `gh pr create` sails through unchecked, which is exactly the thing
        # nobody can afford to miss. Exit 2 doesn't block — the commit already
        # ran — it just makes the message visible.
        outside = tempfile.mkdtemp()  # not a git repo: no branch to detect
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        r = self._commit(repo=outside, gp=os.path.join(outside, "gates.json"))
        self.assertEqual(r.returncode, 2)
        err = r.stderr.decode()
        self.assertIn("will NOT be gated", err)
        # This arrives attached to the commit's own tool call and shaped like
        # an error. Without saying the commit is unaffected, the obvious
        # response is to run it again. It does NOT claim the commit succeeded —
        # this hook cannot know that.
        self.assertIn("already ran", err)

    def _commit_with_broken_parser(self, command):
        """Drive the hook with the command tokenizer forced to raise.

        The real failure needs a bug in shell_parse, so it's injected: the
        detection happens before the hook knows whether the command was a
        commit at all, and that ordering is exactly what's under test."""
        driver = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('ai', {AUTO_INIT!r})\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n"
            "def boom(cmd):\n"
            "    raise RuntimeError('tokenizer bug')\n"
            "m.is_git_commit = boom\n"
            "sys.exit(m.main())\n"
        )
        return subprocess.run(
            ["python3", "-c", driver],
            input=json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": command}}
            ).encode(),
            capture_output=True,
            cwd=self.repo,
            env=dict(os.environ, SDLC_GATES_PATH=self.gp, PYTHONPATH=SCRIPTS),
        )

    def test_parse_failure_on_a_commit_is_escalated(self):
        r = self._commit_with_broken_parser("git commit -m 'x'")
        self.assertEqual(r.returncode, 2, r.stderr.decode())
        self.assertIn("will NOT be gated", r.stderr.decode())

    def test_parse_failure_on_a_non_commit_stays_quiet(self):
        # A tokenizer bug on `ls` must not put "`gh pr create` will NOT be
        # gated" in front of the model: that command never had a cycle to
        # stamp, so nothing was lost and there is nothing to act on.
        r = self._commit_with_broken_parser("ls -la")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertNotIn("will NOT be gated", r.stderr.decode())

    def test_unwritable_store_exits_2_so_the_warning_is_seen(self):
        # Same contract for the write failures: no cycle stamped, so the work
        # only looks gated. Point the store at a path under a file to make the
        # write fail without touching permissions (which root would ignore).
        blocker = os.path.join(self.repo, "blocker")
        with open(blocker, "w") as f:
            f.write("x")
        r = self._commit(gp=os.path.join(blocker, "gates.json"))
        self.assertEqual(r.returncode, 2)
        err = r.stderr.decode()
        self.assertIn("will NOT be gated", err)
        # Must NOT advise `--init`: that writes to the same unwritable store
        # and fails identically. Name the path instead — on a filesystem
        # without flock this repeats on every commit.
        self.assertIn(blocker, err)
        self.assertNotIn("--init", err)

    def test_detached_head_stays_quiet_and_stamps_nothing(self):
        # Detached HEAD is the one no-cycle case that must NOT escalate.
        # `git commit --amend` mid-rebase lands here constantly, so exit 2
        # would interrupt every rebase step — and a cycle keyed to the literal
        # string "HEAD" would then match every future detached state in the
        # repo. The work lands on a branch that stamped its own cycle.
        git(self.repo, "checkout", "-q", "--detach")
        r = self._commit()
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertNotIn("will NOT be gated", r.stderr.decode())
        self.assertFalse(os.path.exists(self.gp))

    def test_unparseable_stdin_surfaces_to_the_user(self):
        # Left on exit-0 stderr this reached nobody, and a payload-shape change
        # would stop auto-init stamping cycles in every repo with no trace.
        r = subprocess.run(
            ["python3", AUTO_INIT],
            input=b"{not json",
            capture_output=True,
            cwd=self.repo,
            env=dict(os.environ, SDLC_GATES_PATH=self.gp),
        )
        self.assertEqual(r.returncode, 0)
        self.assertIn(
            "could not parse hook stdin", stdout_json(self, r)["systemMessage"]
        )

    def test_idempotent_second_commit_does_not_reinit(self):
        self._commit()
        first_ts = read_json(self.gp)["feat/x"]["created_at"]
        self._commit()
        second_ts = read_json(self.gp)["feat/x"]["created_at"]
        self.assertEqual(first_ts, second_ts)

    def test_non_commit_bash_command_no_op(self):
        payload = {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}
        run_hook(AUTO_INIT, payload, self.repo, self.gp)
        self.assertFalse(os.path.exists(self.gp))

    def test_non_bash_tool_no_op(self):
        payload = {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}}
        run_hook(AUTO_INIT, payload, self.repo, self.gp)
        self.assertFalse(os.path.exists(self.gp))

    def test_cd_prefixed_commit_triggers_init(self):
        # hook detects branch from the cd-prefix workdir, not the hook's cwd
        r = self._commit(f"cd {self.repo} && git commit -m 'x'")
        self.assertEqual(
            r.returncode, 2
        )  # armed a cycle: exit 2 is the visible-but-non-blocking channel
        data = read_json(self.gp)
        self.assertIn("feat/x", data)
        self.assertEqual(data["feat/x"]["tier"], "small-medium")

    def test_git_dash_c_commit_triggers_init(self):
        # hook detects branch from the -C workdir flag
        r = self._commit(f"git -C {self.repo} commit -m 'x'")
        self.assertEqual(
            r.returncode, 2
        )  # armed a cycle: exit 2 is the visible-but-non-blocking channel
        data = read_json(self.gp)
        self.assertIn("feat/x", data)
        self.assertEqual(data["feat/x"]["tier"], "small-medium")

    def test_main_branch_skipped(self):
        repo = make_repo(branch="main")
        gp = os.path.join(repo, ".claude", "data", "gates.json")
        r = self._commit("git commit -m 'x'", repo=repo, gp=gp)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(gp))

    def test_master_branch_skipped(self):
        repo = make_repo(branch="master")
        gp = os.path.join(repo, ".claude", "data", "gates.json")
        r = self._commit("git commit -m 'x'", repo=repo, gp=gp)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(gp))

    def test_git_log_grep_commit_no_op(self):
        # "commit" appears as argument value, not subcommand — must not trigger.
        # Tests both quoted and unquoted forms: only the unquoted form was a
        # false positive with the naive negative-lookahead regex.
        for cmd in ('git log --grep "commit"', "git log --grep commit"):
            payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
            run_hook(AUTO_INIT, payload, self.repo, self.gp)
            self.assertFalse(os.path.exists(self.gp), f"falsely triggered for: {cmd!r}")

    def test_wrapped_commit_triggers_init(self):
        # BYPASS regression: `bash -c "git commit"` used to slip past the raw
        # regex, so no cycle was stamped and the PR gate never engaged.
        r = self._commit(f'bash -c "git -C {self.repo} commit --allow-empty -m x"')
        self.assertEqual(
            r.returncode, 2, r.stderr
        )  # armed a cycle: exit 2 is the visible-but-non-blocking channel
        self.assertEqual(read_json(self.gp)["feat/x"]["tier"], "small-medium")

    def test_heredoc_body_mentioning_commit_no_op(self):
        # A PR body that talks about committing is not a commit. Column 0 on
        # purpose — see the note in the enforce heredoc test.
        cmd = "gh pr create --body-file - <<'EOF'\ngit commit -m 'x'\nEOF"
        payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
        run_hook(AUTO_INIT, payload, self.repo, self.gp)
        self.assertFalse(os.path.exists(self.gp))

    def test_project_overlay_skill_defers_auto_init(self):
        # When the repo ships .claude/skills/sdlc/SKILL.md, the overlay owns
        # tier logic; auto-init must skip silently (no gates.json written).
        overlay = os.path.join(self.repo, ".claude", "skills", "sdlc", "SKILL.md")
        os.makedirs(os.path.dirname(overlay))
        with open(overlay, "w") as f:
            f.write("# project overlay\n")
        r = self._commit()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.gp))
        self.assertIn("overlay skill detected", r.stderr.decode())

    def test_enforce_blocks_after_auto_init(self):
        # end-to-end: auto-init fires, then gh pr create is blocked (incomplete gates)
        self._commit()
        self.assertTrue(os.path.exists(self.gp))
        r = run_hook(
            ENFORCE,
            {"tool_name": "Bash", "tool_input": {"command": "gh pr create --fill"}},
            self.repo,
            self.gp,
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("incomplete", r.stderr.decode())


class WorkdirResolutionTest(unittest.TestCase):
    """The hook must inspect the repo the command targets, not its own cwd.

    Deliberately unpinned (clean_env drops SDLC_GATES_PATH) so the store path
    is resolved from the command's workdir — pinning it would mask the bug
    these tests exist for: detecting the invocation but resolving the branch
    and store somewhere else silently reads as 'no cycle' → allow."""

    def setUp(self):
        self.repo = make_repo()
        self.elsewhere = tempfile.mkdtemp()  # a non-repo cwd for the hook
        subprocess.run(
            ["python3", RECORD, "--init", "tiny", "--branch", "feat/x"],
            capture_output=True,
            cwd=self.repo,
            env=clean_env(),
        )

    def tearDown(self):
        for d in (self.elsewhere, self.repo):
            shutil.rmtree(d, ignore_errors=True)

    def enforce_from_elsewhere(self, cmd):
        return subprocess.run(
            ["python3", ENFORCE],
            input=json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": cmd}}
            ).encode(),
            capture_output=True,
            cwd=self.elsewhere,
            env=clean_env(),
        )

    def assertBlockedOnGates(self, r):
        # "incomplete" is the load-bearing part: a foreign cwd also denies with
        # "could not detect branch", so exit code 2 alone would pass even when
        # the workdir never resolved. Only this message proves the hook found
        # the target repo, its branch, and its cycle.
        self.assertEqual(r.returncode, 2, r.stderr.decode())
        self.assertIn("incomplete", r.stderr.decode())

    def test_cd_prefixed_pr_create_blocks_from_foreign_cwd(self):
        self.assertBlockedOnGates(
            self.enforce_from_elsewhere(f"cd {self.repo} && gh pr create --fill")
        )

    def test_wrapped_cd_pr_create_blocks_from_foreign_cwd(self):
        # The combined case: the wrapper hid the command from the old regex AND
        # the `cd` inside it hid the repo. Either miss alone allows the PR.
        self.assertBlockedOnGates(
            self.enforce_from_elsewhere(
                f'bash -c "cd {self.repo} && gh pr create --fill"'
            )
        )

    def test_auto_init_follows_wrapped_cd_to_the_right_repo(self):
        gp = gs.default_store_path(self.repo)
        if os.path.exists(gp):
            os.remove(gp)  # start clean so the stamp below is unambiguous
        subprocess.run(
            ["python3", AUTO_INIT],
            input=json.dumps(
                {
                    "tool_name": "Bash",
                    "tool_input": {
                        "command": f'bash -c "cd {self.repo} && git commit -m x"'
                    },
                }
            ).encode(),
            capture_output=True,
            cwd=self.elsewhere,
            env=clean_env(),
        )
        self.assertTrue(os.path.exists(gp), "cycle stamped in the wrong repo")
        self.assertIn("feat/x", read_json(gp))


class SharedWorktreeStoreTest(unittest.TestCase):
    """The store is shared across all worktrees of a repo and keyed by branch.

    These tests exercise default_store_path's --git-common-dir resolution, so
    they deliberately do NOT pin SDLC_GATES_PATH (clean_env strips it). Layout:
    a main checkout on feat/a plus a linked worktree on feat/b."""

    def setUp(self):
        self.main = make_repo(branch="feat/a")
        self._wt_parent = tempfile.mkdtemp()
        self.wt = os.path.join(self._wt_parent, "wt-b")
        git(self.main, "worktree", "add", "-b", "feat/b", self.wt)
        # the shared store must land in the MAIN checkout, not the linked worktree
        self.shared = os.path.join(self.main, ".sharpen", "data", "gates.json")

    def tearDown(self):
        # Deregister the linked worktree before nuking dirs — avoids leaving a
        # prunable entry behind if this pattern is ever copied to a real repo.
        subprocess.run(
            ["git", "-C", self.main, "worktree", "remove", "-f", self.wt],
            capture_output=True,
        )
        for d in (self._wt_parent, self.main):
            shutil.rmtree(d, ignore_errors=True)

    def test_record_and_enforce_resolve_identical_path(self):
        # The mechanism, not just the symptom: every checkout of the repo must
        # resolve default_store_path to the SAME string. This is what guarantees
        # the recorder and the enforcer touch one file (and one lock). It also
        # pins the realpath canonicalization — on a symlinked tmpdir (macOS
        # /var -> /private/var) a plain abspath would make these two differ.
        from_wt = gs.default_store_path(self.wt)
        from_main = gs.default_store_path(self.main)
        self.assertEqual(from_wt, from_main)
        # ...and it lands in the main checkout, not inside the linked worktree.
        self.assertEqual(from_main, os.path.realpath(self.shared))
        self.assertNotIn(os.path.realpath(self.wt), from_main)

    def cli(self, args, cwd):
        # CLI takes argv, not stdin — distinct from the stdin-driven hooks below.
        return subprocess.run(
            ["python3", RECORD, *args], capture_output=True, cwd=cwd, env=clean_env()
        )

    def _hook(self, script, payload, cwd):
        return subprocess.run(
            ["python3", script],
            input=json.dumps(payload).encode(),
            capture_output=True,
            cwd=cwd,
            env=clean_env(),
        )

    def enforce(self, cmd, cwd):
        return self._hook(
            ENFORCE, {"tool_name": "Bash", "tool_input": {"command": cmd}}, cwd
        )

    def auto(self, skill, cwd):
        return self._hook(
            AUTO, {"tool_name": "Skill", "tool_input": {"skill": skill}}, cwd
        )

    def test_store_lands_in_main_checkout_from_linked_worktree(self):
        # init from the LINKED worktree → file resolves to the MAIN checkout
        r = self.cli(["--init", "tiny", "--branch", "feat/b"], self.wt)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(
            os.path.exists(self.shared), "store should land in main checkout"
        )
        self.assertFalse(
            os.path.exists(os.path.join(self.wt, ".sharpen", "data", "gates.json")),
            "store must NOT be written inside the linked worktree",
        )
        self.assertIn("feat/b", read_json(self.shared))

    def test_enforce_bypass_closed_across_worktrees(self):
        # ENFORCE BYPASS regression: cycle recorded in worktree B is seen by the
        # enforcer running from the MAIN checkout (different cwd, --head feat/b).
        self.cli(["--init", "tiny", "--branch", "feat/b"], self.wt)
        r = self.enforce("gh pr create --head feat/b", self.main)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("incomplete", r.stderr.decode())

    def test_two_branches_isolated_by_key(self):
        self.cli(["--init", "tiny", "--branch", "feat/a"], self.main)
        self.cli(["--init", "tiny", "--branch", "feat/b"], self.wt)
        for g in ("tests", "lint", "typecheck"):  # complete feat/a only
            self.cli(["--record", g, "--branch", "feat/a"], self.main)
        data = read_json(self.shared)
        self.assertEqual(set(data["feat/a"]["gates"]), {"tests", "lint", "typecheck"})
        self.assertEqual(data["feat/b"]["gates"], {})  # untouched
        # both reads come from the one shared file: feat/a allowed, feat/b blocked
        self.assertEqual(
            self.enforce("gh pr create --head feat/a", self.main).returncode, 0
        )
        self.assertEqual(
            self.enforce("gh pr create --head feat/b", self.main).returncode, 2
        )

    def test_auto_record_miss_closed_across_worktrees(self):
        # AUTO-RECORD MISS regression: a session in the MAIN checkout (feat/a, no
        # cycle) auto-records a grumpy gate for feat/b — the cycle lives in the
        # shared store and feat/b is checked out in worktree B.
        self.cli(["--init", "small-medium", "--branch", "feat/b"], self.wt)
        r = self.auto("grumpy:review", self.main)
        self.assertIn("grumpy-review", read_json(self.shared)["feat/b"]["gates"])
        # A cross-worktree stamp must NAME the branch it landed on, AND surface
        # (exit 2): it is a write, and a wrong-branch stamp cannot be undone
        # (skill-gated gates have no manual --record). Keying the message off
        # `routed` alone would silently drop the branch name here, and miss the
        # surfacing too — the adoption path, not an explicit route.
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn(" on feat/b", r.stderr.decode())


class StoreGuardTest(unittest.TestCase):
    """The invariant lives in the mutator, so every recorder is covered."""

    def test_record_gate_refuses_skill_gated_by_default(self):
        for g in gs.SKILL_FOR_GATE:
            d = {}
            gs.init_gates(d, "feat/x", "small-medium")
            with self.assertRaises(ValueError, msg=g):
                gs.record_gate(d, "feat/x", g)
            self.assertEqual(d["feat/x"]["gates"], {})

    def test_record_gate_allows_authorized(self):
        d = {}
        gs.init_gates(d, "feat/x", "small-medium")
        gs.record_gate(d, "feat/x", "simplify", authorized=True)
        self.assertIn("simplify", d["feat/x"]["gates"])

    def test_record_gate_refuses_a_gate_not_required_by_the_tier(self):
        # `tiny` doesn't require simplify/grumpy-*. Recording one anyway
        # (even authorized) would populate `gates` with a key required_gates
        # filters back out everywhere else — a landmine for anything that
        # ever reads the raw dict instead of going through the accessors.
        d = {}
        gs.init_gates(d, "feat/x", "tiny")
        with self.assertRaises(ValueError):
            gs.record_gate(d, "feat/x", "simplify", authorized=True)
        self.assertEqual(d["feat/x"]["gates"], {})

    def test_record_gate_allows_a_gate_required_by_the_tier(self):
        d = {}
        gs.init_gates(d, "feat/x", "tiny")
        gs.record_gate(d, "feat/x", "tests")
        self.assertIn("tests", d["feat/x"]["gates"])

    def test_bash_gates_need_no_authorization(self):
        d = {}
        gs.init_gates(d, "feat/x", "small-medium")
        for g in gs.BASH_GATES:
            gs.record_gate(d, "feat/x", g)
        self.assertEqual(set(gs.completed_gates(d["feat/x"])), set(gs.BASH_GATES))

    def test_protected_set_is_exactly_these_five(self):
        # every other assertion derives from SKILL_FOR_GATE, so dropping a gate
        # from it would silently demote that gate everywhere
        self.assertEqual(
            sorted(gs.SKILL_FOR_GATE),
            [
                "grumpy-fix-post-imagine",
                "grumpy-fix-post-review",
                "grumpy-imagine",
                "grumpy-review",
                "simplify",
            ],
        )
        self.assertFalse(set(gs.BASH_GATES) & set(gs.SKILL_FOR_GATE))
        # every gate a tracked skill can record must be protected — otherwise
        # adding a skill and forgetting SKILL_FOR_GATE silently demotes its
        # gate to manually recordable, the exact thing this guard prevents
        for gate in gs.SKILL_TO_GATE.values():
            targets = (
                ["grumpy-fix-post-review", "grumpy-fix-post-imagine"]
                if gate == "grumpy-fix"
                else [gate]
            )
            for t in targets:
                self.assertIn(t, gs.SKILL_FOR_GATE, t)
        self.assertEqual(
            sorted(gs.BASH_GATES + list(gs.SKILL_FOR_GATE)), sorted(gs.ALL_GATE_NAMES)
        )


class CliSkillGatedGuardTest(unittest.TestCase):
    """record-gate.py surfaces the store's refusal instead of recording."""

    def setUp(self):
        self.repo = make_repo()
        self.gp = os.path.join(self.repo, ".claude", "data", "gates.json")
        run_cli(["--init", "small-medium"], self.repo, self.gp)

    def test_refusal_names_every_blocked_gate(self):
        cmd = (
            "record-gate.py --record simplify && record-gate.py --record grumpy-review"
        )
        r = run_hook(
            BLOCK, {"tool_name": "Bash", "tool_input": {"command": cmd}}, HERE, "/nope"
        )
        self.assertEqual(r.returncode, 2)
        # Denials carry the reason in the documented stdout payload; stderr
        # is the same text as prose, not a JSON blob for the agent to read.
        reason = stdout_json(self, r)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("simplify", reason)
        self.assertIn("grumpy-review", reason)

    def test_refuses_every_skill_gated_gate(self):
        for g in gs.SKILL_FOR_GATE:
            r = run_cli(["--record", g], self.repo, self.gp)
            self.assertEqual(r.returncode, 1, g)
            self.assertIn("skill-gated", r.stderr.decode())
            self.assertFalse(read_json(self.gp)["feat/x"]["gates"].get(g), g)

    def test_refuses_quoted_and_expanded_forms(self):
        # Real shells: the quoting and expansion that slipped past a raw-string
        # hook are gone by the time argv arrives, so the refusal has to come
        # from the store — assert on the message, not just the exit code.
        env = dict(os.environ, SDLC_GATES_PATH=self.gp, GATE="simplify")
        for cmd in (
            f'python3 "{RECORD}" --record "simplify"',
            f"python3 '{RECORD}' --record 'simplify'",
            f'python3 "{RECORD}" --record "$GATE"',
            f"bash -c 'python3 \"{RECORD}\" --record simplify'",
        ):
            r = subprocess.run(
                ["bash", "-c", cmd], capture_output=True, cwd=self.repo, env=env
            )
            self.assertEqual(r.returncode, 1, cmd)
            self.assertIn("skill-gated", r.stderr.decode(), cmd)
        self.assertFalse(read_json(self.gp)["feat/x"]["gates"].get("simplify"))

    def test_inline_python_cannot_record(self):
        # the form the Bash hook cannot see at all: it returns [] for python -c
        env = dict(os.environ, SDLC_GATES_PATH=self.gp, PYTHONPATH=SCRIPTS)
        r = subprocess.run(
            [
                "python3",
                "-c",
                "import gate_store as gs, os;"
                "gs.update_store(os.environ['SDLC_GATES_PATH'],"
                " lambda d: gs.record_gate(d, 'feat/x', 'simplify'))",
            ],
            capture_output=True,
            cwd=self.repo,
            env=env,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("skill-gated", r.stderr.decode())
        self.assertFalse(read_json(self.gp)["feat/x"]["gates"].get("simplify"))

    def test_still_records_bash_gates(self):
        for g in gs.BASH_GATES:
            r = run_cli(["--record", g], self.repo, self.gp)
            self.assertEqual(r.returncode, 0, g)
            self.assertTrue(read_json(self.gp)["feat/x"]["gates"][g], g)

    def test_auto_record_hook_still_works(self):
        # the auto-record hook is the one authorized caller
        run_hook(
            AUTO,
            {"tool_name": "Skill", "tool_input": {"skill": "simplify"}},
            self.repo,
            self.gp,
        )
        self.assertTrue(read_json(self.gp)["feat/x"]["gates"]["simplify"])


class BlockAutoRecordDrivingTest(unittest.TestCase):
    def hook(self, cmd):
        return run_hook(
            BLOCK, {"tool_name": "Bash", "tool_input": {"command": cmd}}, HERE, "/nope"
        )

    def test_blocks_driving_the_auto_record_hook_by_hand(self):
        # the authorized recorder is a script on disk; hand-feeding it a forged
        # payload records a gate whose skill never ran
        for cmd in (
            "echo '{}' | python3 auto-record-skill-gate.py",
            'printf "%s" "$P" | python3 plugins/sdlc/scripts/auto-record-skill-gate.py',
            "bash -c 'python3 auto-record-skill-gate.py < payload.json'",
        ):
            r = self.hook(cmd)
            self.assertEqual(r.returncode, 2, cmd)
            self.assertIn(
                "auto-record",
                stdout_json(self, r)["hookSpecificOutput"]["permissionDecisionReason"],
            )

    def test_allows_mentioning_it_in_a_string(self):
        r = self.hook('echo "see auto-record-skill-gate.py for the hook"')
        self.assertEqual(r.returncode, 0)


class AutoRecordAmbiguityTest(unittest.TestCase):
    def test_two_pending_cycles_records_nothing(self):
        # a gate stamped on the wrong branch cannot be corrected — skill-gated
        # gates have no manual --record — so guessing is worse than skipping
        data = {}
        gs.init_gates(data, "feat/a", "small-medium")
        gs.init_gates(data, "feat/b", "small-medium")
        res = auto.handle_skill_completion(
            "grumpy:review",
            data,
            branch="feat/c",
            active_branches={"feat/a", "feat/b", "feat/c"},
        )
        self.assertFalse(res["recorded"])
        self.assertEqual(data["feat/a"]["gates"], {})
        self.assertEqual(data["feat/b"]["gates"], {})

    def test_ambiguous_skip_is_surfaced_to_the_agent(self):
        # PostToolUse stderr only reaches the model on exit 2. A skip the caller
        # expected to be a record has to use it, or the gate silently doesn't
        # happen; "no cycle anywhere" is the opt-out and must stay quiet.
        repo = make_repo(branch="feat/c")
        gp = os.path.join(repo, ".claude", "data", "gates.json")
        for b in ("feat/a", "feat/b"):
            wt = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, wt, ignore_errors=True)
            git(repo, "worktree", "add", "-q", "-b", b, wt)
            run_cli(["--init", "small-medium", "--branch", b], repo, gp)

        payload = {"tool_name": "Skill", "tool_input": {"skill": "grumpy:review"}}
        r = run_hook(AUTO, payload, repo, gp)  # two live candidates, none ours
        self.assertEqual(r.returncode, 2, r.stderr)
        err = r.stderr.decode().lower()
        self.assertIn("refusing", err)
        self.assertIn("feat/a", err)
        self.assertIn("feat/b", err)
        store = read_json(gp)
        self.assertEqual(store["feat/a"]["gates"], {})
        self.assertEqual(store["feat/b"]["gates"], {})

    def test_no_cycle_anywhere_stays_quiet(self):
        # the opt-out path: nagging here would fire on every skill run in every
        # ungated repo
        repo = make_repo(branch="feat/quiet")
        gp = os.path.join(repo, ".claude", "data", "gates.json")
        r = run_hook(
            AUTO,
            {"tool_name": "Skill", "tool_input": {"skill": "grumpy:review"}},
            repo,
            gp,
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_tiny_cycle_is_not_a_candidate(self):
        # `tiny` never requires grumpy-review, so a sibling tiny cycle must not
        # count as pending — it used to both mask the real target and collect a
        # gate its tier doesn't want
        data = {}
        gs.init_gates(data, "feat/a", "small-medium")
        gs.init_gates(data, "feat/b", "tiny")
        res = auto.handle_skill_completion(
            "grumpy:review",
            data,
            branch="feat/c",
            active_branches={"feat/a", "feat/b", "feat/c"},
        )
        self.assertTrue(res["recorded"])
        self.assertEqual(res["target"], "feat/a")
        self.assertEqual(data["feat/b"]["gates"], {})

    def test_tiny_cycle_does_not_collect_skill_gates(self):
        data = {}
        gs.init_gates(data, "feat/x", "tiny")
        for skill in ("grumpy:review", "simplify", "grumpy:imagine"):
            self.assertIsNone(gs.determine_gate(skill, data["feat/x"]), skill)
        res = auto.handle_skill_completion(
            "grumpy:review", data, branch="feat/x", active_branches={"feat/x"}
        )
        self.assertFalse(res["recorded"])
        self.assertEqual(data["feat/x"]["gates"], {})

    def test_one_pending_cycle_still_records(self):
        data = {}
        gs.init_gates(data, "feat/a", "small-medium")
        res = auto.handle_skill_completion(
            "grumpy:review", data, branch="feat/c", active_branches={"feat/a", "feat/c"}
        )
        self.assertTrue(res["recorded"])
        self.assertEqual(res["target"], "feat/a")

    def test_detached_head_falls_back_to_the_one_pending_cycle(self):
        # The anchor checkout of a worktree-based session sits on detached
        # HEAD by design (git worktree add's normal shape) — that must not
        # block the cross-worktree fallback that exists precisely for "can't
        # detect branch from cwd". Issue #5.
        data = {}
        gs.init_gates(data, "feat/a", "small-medium")
        res = auto.handle_skill_completion(
            "grumpy:review", data, branch="HEAD", active_branches={"feat/a"}
        )
        self.assertTrue(res["recorded"])
        self.assertEqual(res["target"], "feat/a")

    def test_detached_head_with_no_cycles_stays_quiet(self):
        # Existing behavior preserved: routine mid-rebase, nothing lost.
        data = {}
        res = auto.handle_skill_completion(
            "grumpy:review", data, branch="HEAD", active_branches=set()
        )
        self.assertFalse(res["recorded"])
        self.assertFalse(res.get("surprising"))
        self.assertIn("detached HEAD", res["reason"])

    def test_detached_head_with_ambiguous_cycles_surfaces(self):
        # Two live candidates, still on detached HEAD: refuse to guess, same
        # as the named-branch case — and say so loudly, since this WAS a
        # cycle we declined to pick.
        data = {}
        gs.init_gates(data, "feat/a", "small-medium")
        gs.init_gates(data, "feat/b", "small-medium")
        res = auto.handle_skill_completion(
            "grumpy:review",
            data,
            branch="HEAD",
            active_branches={"feat/a", "feat/b"},
        )
        self.assertFalse(res["recorded"])
        self.assertTrue(res["surprising"])
        self.assertIn("detached HEAD", res["reason"])
        self.assertEqual(data["feat/a"]["gates"], {})
        self.assertEqual(data["feat/b"]["gates"], {})

    def test_unrouted_worktree_list_failure_fails_closed_and_names_the_branch(self):
        # Distinct from test_routed_stamp_fails_closed_when_worktree_list_fails:
        # that one covers the routed early-return path. This is the ordinary
        # unrouted lookup — the ordinary `where` f-string this diff's fix
        # touches — which had no active_branches=None coverage at all before.
        data = {}
        gs.init_gates(data, "feat/a", "small-medium")
        res = auto.handle_skill_completion(
            "grumpy:review",
            data,
            branch="feat/b",
            active_branches=None,
        )
        self.assertFalse(res["recorded"])
        self.assertTrue(res["surprising"])
        self.assertIn('branch "feat/b"', res["reason"])
        self.assertIn("git worktree list", res["reason"])
        self.assertEqual(data["feat/a"]["gates"], {})


class PortableStateRootTest(unittest.TestCase):
    def test_default_store_prefers_neutral_sharpen_data(self):
        repo = make_repo(branch="feat/a")
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        self.assertEqual(
            gs.default_store_path(repo),
            os.path.realpath(os.path.join(repo, ".sharpen", "data", "gates.json")),
        )

    def test_legacy_claude_data_store_remains_active_until_neutral_exists(self):
        repo = make_repo(branch="feat/a")
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        legacy_dir = os.path.join(repo, ".claude", "data")
        os.makedirs(legacy_dir)
        legacy = os.path.join(legacy_dir, "gates.json")
        self.assertEqual(gs.default_store_path(repo), os.path.realpath(legacy))
        neutral_dir = os.path.join(repo, ".sharpen", "data")
        os.makedirs(neutral_dir)
        self.assertEqual(
            gs.default_store_path(repo),
            os.path.realpath(os.path.join(neutral_dir, "gates.json")),
        )


class StoreHousekeepingTest(unittest.TestCase):
    def test_fallback_store_honors_the_caller_cwd(self):
        # callers pass the workdir they resolved out of the command; using the
        # process cwd instead splits recorder from enforcer
        with tempfile.TemporaryDirectory() as tmp:
            got = subprocess.run(
                [
                    "python3",
                    "-c",
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "import gate_store as gs; "
                    "print(gs.default_store_path(cwd=sys.argv[2]))",
                    SCRIPTS,
                    tmp,
                ],
                capture_output=True,
                cwd=HERE,
                env=clean_env(),
            )
            path = got.stdout.decode().strip()
            self.assertTrue(
                path.startswith(os.path.realpath(tmp)), f"{path} not under {tmp}"
            )

    def test_update_store_sweeps_only_its_own_stale_temps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gates.json")
            stale = os.path.join(tmp, gs._TMP_PREFIX + "orphan.tmp")
            fresh = os.path.join(tmp, gs._TMP_PREFIX + "live.tmp")
            someone_elses = os.path.join(tmp, "other-tool.tmp")
            for f in (stale, fresh, someone_elses):
                open(f, "w").close()
            for f in (stale, someone_elses):
                os.utime(f, (0, 0))  # older than the sweep cutoff
            gs.update_store(path, lambda d: gs.init_gates(d, "feat/x", "tiny"))
            self.assertFalse(os.path.exists(stale), "stale temp not swept")
            self.assertTrue(os.path.exists(fresh), "live temp must be left alone")
            self.assertTrue(
                os.path.exists(someone_elses),
                ".claude/data is shared — only our own prefix may be swept",
            )


class RouteStoreTest(unittest.TestCase):
    """Pure store-level routing logic — no git, no subprocesses."""

    def setUp(self):
        self.d = {}
        gs.init_gates(self.d, "feat/a", "small-medium")
        gs.init_gates(self.d, "feat/b", "small-medium")

    def test_set_and_resolve(self):
        gs.set_route(self.d, "feat/a", "/wt/b")
        self.assertEqual(gs.routed_branch(self.d, "/wt/b")[0], "feat/a")

    def test_route_is_one_to_one_per_source(self):
        # A source worktree drives exactly one branch; re-routing moves it
        # rather than leaving two cycles both claiming the same driver.
        gs.set_route(self.d, "feat/a", "/wt/b")
        gs.set_route(self.d, "feat/b", "/wt/b")
        self.assertNotIn(gs.ROUTE_KEY, self.d["feat/a"])
        self.assertEqual(gs.routed_branch(self.d, "/wt/b")[0], "feat/b")

    def test_two_sources_may_drive_one_branch(self):
        # With a scalar route the second driver silently evicted the first, whose
        # gates then fell back to its own branch — the very misroute this closes.
        gs.set_route(self.d, "feat/a", "/wt/x")
        gs.set_route(self.d, "feat/a", "/wt/y")
        self.assertEqual(gs.routed_branch(self.d, "/wt/x")[0], "feat/a")
        self.assertEqual(gs.routed_branch(self.d, "/wt/y")[0], "feat/a")

    def test_clearing_one_source_leaves_the_other(self):
        gs.set_route(self.d, "feat/a", "/wt/x")
        gs.set_route(self.d, "feat/a", "/wt/y")
        gs.clear_route(self.d, "/wt/x")
        self.assertIsNone(gs.routed_branch(self.d, "/wt/x"))
        self.assertEqual(gs.routed_branch(self.d, "/wt/y")[0], "feat/a")

    def test_last_source_removes_the_key_entirely(self):
        # An unrouted cycle must be byte-identical to a never-routed one, or
        # update_store's change detection starts rewriting the store for nothing.
        gs.set_route(self.d, "feat/a", "/wt/x")
        gs.clear_route(self.d, "/wt/x")
        self.assertNotIn(gs.ROUTE_KEY, self.d["feat/a"])

    def test_re_routing_the_same_source_does_not_duplicate_it(self):
        gs.set_route(self.d, "feat/a", "/wt/x")
        gs.set_route(self.d, "feat/a", "/wt/x")
        self.assertEqual(self.d["feat/a"][gs.ROUTE_KEY], ["/wt/x"])

    def test_scalar_route_from_an_older_store_still_resolves(self):
        self.d["feat/a"][gs.ROUTE_KEY] = "/wt/x"  # pre-list on-disk shape
        self.assertEqual(gs.routed_branch(self.d, "/wt/x")[0], "feat/a")
        self.assertEqual(gs.clear_route(self.d, "/wt/x"), ["feat/a"])

    def test_distinct_sources_route_independently(self):
        gs.set_route(self.d, "feat/a", "/wt/x")
        gs.set_route(self.d, "feat/b", "/wt/y")
        self.assertEqual(gs.routed_branch(self.d, "/wt/x")[0], "feat/a")
        self.assertEqual(gs.routed_branch(self.d, "/wt/y")[0], "feat/b")

    def test_unknown_source_and_none_resolve_to_nothing(self):
        gs.set_route(self.d, "feat/a", "/wt/b")
        self.assertIsNone(gs.routed_branch(self.d, "/wt/other"))
        self.assertIsNone(gs.routed_branch(self.d, None))

    def test_route_mismatch_reports_the_other_branch(self):
        gs.set_route(self.d, "feat/a", "/wt/b")
        self.assertEqual(gs.route_mismatch(self.d, "/wt/b", "feat/b"), "feat/a")

    def test_route_mismatch_is_none_when_routed_to_the_asked_branch(self):
        gs.set_route(self.d, "feat/a", "/wt/b")
        self.assertIsNone(gs.route_mismatch(self.d, "/wt/b", "feat/a"))

    def test_route_mismatch_is_none_when_not_routed_at_all(self):
        self.assertIsNone(gs.route_mismatch(self.d, "/wt/nowhere", "feat/a"))

    def test_route_mismatch_note_names_branch_and_unroute(self):
        gs.set_route(self.d, "feat/a", "/wt/b")
        note = gs.route_mismatch_note(self.d, "/wt/b", "feat/b")
        self.assertIn("feat/a", note)
        self.assertIn("--unroute", note)

    def test_route_mismatch_note_is_none_when_routed_to_the_asked_branch(self):
        gs.set_route(self.d, "feat/a", "/wt/b")
        self.assertIsNone(gs.route_mismatch_note(self.d, "/wt/b", "feat/a"))

    def test_has_any_route_false_on_an_unrouted_store(self):
        self.assertFalse(gs.has_any_route(self.d))

    def test_has_any_route_true_once_any_branch_is_routed(self):
        gs.set_route(self.d, "feat/a", "/wt/b")
        self.assertTrue(gs.has_any_route(self.d))

    def test_route_survives_reinit(self):
        # --init doubles as the post-gate reset. Losing the route there would
        # silently unhook the driving worktree halfway through the chain.
        gs.set_route(self.d, "feat/a", "/wt/b")
        gs.record_gate(self.d, "feat/a", "tests")
        gs.init_gates(self.d, "feat/a", "small-medium")
        self.assertEqual(gs.route_sources(self.d["feat/a"]), ["/wt/b"])
        self.assertEqual(self.d["feat/a"]["gates"], {})  # timestamps still cleared

    def test_clear_route_reports_branches(self):
        gs.set_route(self.d, "feat/a", "/wt/b")
        self.assertEqual(gs.clear_route(self.d, "/wt/b"), ["feat/a"])
        self.assertEqual(gs.clear_route(self.d, "/wt/b"), [])

    def test_route_to_missing_cycle_rejected(self):
        with self.assertRaises(ValueError):
            gs.set_route(self.d, "feat/nope", "/wt/b")

    def test_reinit_over_a_corrupt_entry_does_not_raise(self):
        # A non-dict under a branch key would make the route-preserving read
        # raise AttributeError, which none of the CLI's handlers catch.
        gs.init_gates({"feat/x": "not-a-dict"}, "feat/x", "tiny")

    def test_trunk_is_never_a_route_target(self):
        # Unreachable via init_gates, but a hand-edited store must not be able
        # to aim the auto-recorder at the trunk.
        for trunk in ("main", "master"):
            d = {trunk: {"tier": "tiny", "gates": {}, gs.ROUTE_KEY: ["/wt/b"]}}
            self.assertIsNone(gs.routed_branch(d, "/wt/b"))

    def test_route_sources_tolerates_garbage(self):
        for junk in (None, "x", 3, {"a": 1}, [], [1, "/wt/x"]):
            self.assertNotIn(None, gs.route_sources({gs.ROUTE_KEY: junk}))
        self.assertEqual(gs.route_sources({gs.ROUTE_KEY: [1, "/wt/x"]}), ["/wt/x"])
        self.assertEqual(gs.route_sources("not-a-dict"), [])

    def test_status_shows_route(self):
        gs.set_route(self.d, "feat/a", "/wt/b")
        self.assertIn(
            "Driven from: /wt/b", gs.format_status(self.d["feat/a"], "feat/a")
        )


class CrossWorktreeRoutingTest(unittest.TestCase):
    """`/sdlc:gate --worktree A` driven from session B.

    Layout mirrors the real thing: a main checkout on feat/a, plus TWO linked
    worktrees (feat/b, feat/c). Skills always run in B's cwd; the gates must
    land on A's branch anyway."""

    def setUp(self):
        self.main = make_repo(branch="feat/a")
        self._parent = tempfile.mkdtemp()
        self.wt_b = os.path.join(self._parent, "wt-b")
        self.wt_c = os.path.join(self._parent, "wt-c")
        git(self.main, "worktree", "add", "-b", "feat/b", self.wt_b)
        git(self.main, "worktree", "add", "-b", "feat/c", self.wt_c)
        self.gp = os.path.join(self.main, ".claude", "data", "gates.json")

    def tearDown(self):
        for wt in (self.wt_b, self.wt_c):
            if not wt:
                continue
            subprocess.run(
                ["git", "-C", self.main, "worktree", "remove", "-f", wt],
                capture_output=True,
            )
        for d in (self._parent, self.main):
            shutil.rmtree(d, ignore_errors=True)

    def cli(self, args, cwd):
        return run_cli(args, cwd, self.gp)

    def skill(self, name, cwd, payload_cwd=None):
        payload = {"tool_name": "Skill", "tool_input": {"skill": name}}
        if payload_cwd:
            payload["cwd"] = payload_cwd
        return run_hook(AUTO, payload, cwd, self.gp)

    def gates(self, branch):
        return read_json(self.gp).get(branch, {}).get("gates", {})

    def enforce(self, cmd, cwd):
        return run_hook(
            ENFORCE, {"tool_name": "Bash", "tool_input": {"command": cmd}}, cwd, self.gp
        )

    def init_routed(self, target, cwd, tier="small-medium"):
        r = self.cli(["--init", tier, "--branch", target, "--route-from", cwd], cwd)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_skill_gate_lands_on_target_not_driving_worktree(self):
        # THE BUG: B drives A's cycle; the skill runs in B; without a route the
        # gate was stamped on B's own branch.
        self.init_routed("feat/c", self.wt_b)
        r = self.skill("grumpy:review", self.wt_b)
        # The write landed away from this worktree's own branch — irreversible
        # (no manual --record for skill gates), so it surfaces (exit 2).
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("grumpy-review", self.gates("feat/c"))
        self.assertNotIn("feat/b", read_json(self.gp))

    def test_detached_head_anchor_falls_back_to_the_one_pending_worktree_cycle(self):
        # Issue #5, end to end with real git: the anchor checkout of a
        # worktree-based session sits on detached HEAD by design (`git
        # worktree add`'s normal shape). That must not block the
        # cross-worktree fallback that exists precisely for "can't detect
        # branch from cwd" — only one other checked-out branch (feat/b) has
        # a pending cycle, so it's unambiguous.
        self.cli(["--init", "small-medium", "--branch", "feat/b"], self.wt_b)
        git(self.main, "checkout", "-q", "--detach")
        r = self.skill("grumpy:review", self.main)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("grumpy-review", self.gates("feat/b"))

    def test_status_names_the_branch_this_worktree_actually_routes_to(self):
        # Before this, --status from the DRIVING worktree only showed that
        # worktree's own (often nonexistent) local cycle — the only way to
        # learn "you're routing away" was --unroute's output or inference.
        self.init_routed("feat/c", self.wt_b)
        r = self.cli(["--status", "--branch", "feat/b"], self.wt_b)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout.decode()
        self.assertIn("feat/c", out)
        self.assertIn("--unroute", out)

    def test_status_says_nothing_about_routing_when_there_is_none(self):
        self.cli(["--init", "small-medium", "--branch", "feat/b"], self.wt_b)
        r = self.cli(["--status", "--branch", "feat/b"], self.wt_b)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("--unroute", r.stdout.decode())

    def test_route_wins_over_the_driving_worktrees_own_cycle(self):
        # Consequence 1: B has its own cycle, so the old code confidently
        # stamped there. The explicit route must outrank it.
        self.cli(["--init", "small-medium", "--branch", "feat/b"], self.wt_b)
        self.init_routed("feat/c", self.wt_b)
        self.skill("grumpy:review", self.wt_b)
        self.assertIn("grumpy-review", self.gates("feat/c"))
        self.assertEqual(self.gates("feat/b"), {})

    def test_two_live_worktrees_no_longer_ambiguous(self):
        # Consequence 2: with feat/b AND feat/c both checked out and both
        # holding cycles, find_active_cycle refused to guess and --worktree
        # stopped working. The route makes it deterministic.
        self.cli(["--init", "small-medium", "--branch", "feat/c"], self.wt_c)
        self.init_routed("feat/a", self.wt_b)
        r = self.skill("grumpy:review", self.wt_b)
        self.assertEqual(r.returncode, 2, r.stderr)  # cross-worktree write, surfaces
        self.assertIn("grumpy-review", self.gates("feat/a"))
        self.assertEqual(self.gates("feat/c"), {})

    def test_full_skill_chain_lands_on_target(self):
        self.init_routed("feat/c", self.wt_b)
        for skill in ("simplify", "grumpy:review", "grumpy:fix", "grumpy:imagine"):
            self.skill(skill, self.wt_b)
        self.assertEqual(
            set(self.gates("feat/c")),
            {"simplify", "grumpy-review", "grumpy-fix-post-review", "grumpy-imagine"},
        )

    def test_route_survives_the_post_gate_reset(self):
        self.init_routed("feat/c", self.wt_b)
        self.skill("grumpy:review", self.wt_b)
        # post-gate code change -> reset, this time WITHOUT re-passing the flag
        self.cli(["--init", "small-medium", "--branch", "feat/c"], self.wt_b)
        self.assertEqual(self.gates("feat/c"), {})
        self.skill("grumpy:review", self.wt_b)
        self.assertIn("grumpy-review", self.gates("feat/c"))

    def test_unroute_returns_gates_to_the_local_branch(self):
        self.init_routed("feat/c", self.wt_b)
        r = self.cli(["--unroute"], self.wt_b)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("feat/c", r.stderr.decode())
        self.cli(["--init", "small-medium", "--branch", "feat/b"], self.wt_b)
        self.skill("grumpy:review", self.wt_b)
        self.assertIn("grumpy-review", self.gates("feat/b"))
        self.assertEqual(self.gates("feat/c"), {})

    def test_dropping_a_route_says_so(self):
        # Revoking moves where this worktree's gates land. Doing that in silence
        # is the failure this channel exists to stop — a live walkthrough put a
        # gate on a different branch than the previous one with no output.
        self.init_routed("feat/c", self.wt_b)
        r = self.cli(["--init", "small-medium", "--branch", "feat/b"], self.wt_b)
        self.assertIn("no longer drives", r.stderr.decode())
        self.assertIn("feat/c", r.stderr.decode())

    def test_reinit_of_a_routed_target_does_not_claim_a_drop(self):
        # The post-gate reset re-establishes the same route; saying "no longer
        # drives feat/c" there would be a lie.
        self.init_routed("feat/c", self.wt_b)
        r = self.init_routed("feat/c", self.wt_b)
        self.assertNotIn("no longer drives", r.stderr.decode())

    def test_init_for_own_branch_drops_a_stale_route(self):
        # B finishes driving C, then starts gating itself. Without this the
        # stale route would keep redirecting B's own skill gates to feat/c.
        self.init_routed("feat/c", self.wt_b)
        self.cli(["--init", "small-medium", "--branch", "feat/b"], self.wt_b)
        self.skill("grumpy:review", self.wt_b)
        self.assertIn("grumpy-review", self.gates("feat/b"))
        self.assertEqual(self.gates("feat/c"), {})

    def test_unroute_with_no_route_is_a_clean_noop(self):
        r = self.cli(["--unroute"], self.wt_b)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not driving", r.stderr.decode())
        # and it does not conjure a store, which would look like an opted-in
        # repo to every hook that reads one
        self.assertFalse(os.path.exists(self.gp))

    def test_self_route_is_not_stored(self):
        # Routing a worktree to the branch it already has checked out buys
        # nothing and would go stale on the next branch switch.
        self.init_routed("feat/b", self.wt_b)
        self.assertNotIn(gs.ROUTE_KEY, read_json(self.gp)["feat/b"])

    def test_routed_branch_with_no_pending_gate_does_not_fall_back(self):
        # A route that yields nothing must report that, NOT quietly stamp the
        # local branch — silent misrouting is the whole bug.
        self.cli(["--init", "small-medium", "--branch", "feat/b"], self.wt_b)
        self.init_routed("feat/c", self.wt_b)
        self.skill("grumpy:review", self.wt_b)
        r = self.skill("grumpy:review", self.wt_b)  # already recorded on feat/c
        # This is the steady state of a routed cycle that already finished —
        # nothing was lost, and it recurs on every later skill run until
        # --unroute. Interrupting here forever is the permanent nag this axis
        # exists to remove, so it stays quiet (exit 0).
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.gates("feat/b"), {})
        self.assertIn("feat/c", r.stderr.decode())

    def test_stale_route_to_a_deleted_branch_does_not_stamp(self):
        # The route never checks its target is still checked out, unlike the
        # heuristic cross-worktree path (which takes active_branches). After
        # `git worktree remove` + `git branch -D`, a stamp must not land on a
        # branch that no longer exists anywhere.
        self.init_routed("feat/c", self.wt_b)
        git(self.main, "worktree", "remove", "-f", self.wt_c)
        git(self.main, "branch", "-D", "feat/c")
        r = self.skill("grumpy:review", self.wt_b)
        self.assertEqual(r.returncode, 2, r.stderr)
        err = r.stderr.decode().lower()
        self.assertIn("feat/c", err)
        self.assertIn("--unroute", err)
        self.assertEqual(self.gates("feat/c"), {})  # stale target: not stamped
        self.assertEqual(self.gates("feat/b"), {})  # no silent fallback either
        # wt_c is gone; don't let tearDown try to remove it again.
        self.wt_c = None

    def test_routed_stamp_fails_closed_when_worktree_list_fails(self):
        # active_branches=None means `git worktree list` itself failed, not
        # that the target is confirmed gone. The routed path must not treat
        # "couldn't check" as "must be fine" — same posture find_active_cycle
        # already takes for the heuristic path.
        data = {}
        gs.init_gates(data, "feat/c", "small-medium")
        gs.set_route(data, "feat/c", "/wt/b")
        res = auto.handle_skill_completion(
            "grumpy:review",
            data,
            branch="feat/b",
            active_branches=None,
            source_root="/wt/b",
        )
        self.assertFalse(res["recorded"])
        self.assertTrue(res.get("surprising"))
        self.assertEqual(data["feat/c"]["gates"], {})

    def test_already_recorded_routed_skip_stays_quiet_even_if_worktree_list_fails(self):
        # A routed cycle that's already finished needs no liveness check at
        # all — nothing is about to be written. A `git worktree list` hiccup
        # must not turn that steady-state no-op into a surprise; that would
        # reintroduce the renag-forever bug this diff removed, just triggered
        # by git flakiness instead of routing.
        data = {}
        gs.init_gates(data, "feat/c", "small-medium")
        gs.record_gate(data, "feat/c", "grumpy-review", authorized=True)
        gs.set_route(data, "feat/c", "/wt/b")
        res = auto.handle_skill_completion(
            "grumpy:review",
            data,
            branch="feat/b",
            active_branches=None,
            source_root="/wt/b",
        )
        self.assertFalse(res["recorded"])
        self.assertFalse(res.get("surprising"))

    def test_payload_cwd_overrides_process_cwd(self):
        # The hook trusts the payload's cwd, so a session reporting B resolves
        # B's route even when the hook process itself was spawned elsewhere.
        self.init_routed("feat/c", self.wt_b)
        r = self.skill("grumpy:review", self.main, payload_cwd=self.wt_b)
        self.assertEqual(r.returncode, 2, r.stderr)  # cross-worktree write, surfaces
        self.assertIn("grumpy-review", self.gates("feat/c"))

    def test_driver_on_detached_head_still_routes(self):
        # The route is looked up before the branch is resolved, so what the
        # driving worktree has checked out — including nothing — is irrelevant
        # to which branch it drives. Unrouted, a detached HEAD gives up here.
        self.init_routed("feat/a", self.wt_b)
        git(self.wt_b, "checkout", "-q", "--detach")
        r = self.skill("grumpy:review", self.wt_b)
        self.assertEqual(r.returncode, 2, r.stderr)  # cross-worktree write, surfaces
        self.assertIn("grumpy-review", self.gates("feat/a"))

    def test_enforce_denial_names_the_route_starving_this_branch(self):
        # B's own branch (feat/b) is starved for gates because B is routing its
        # skill gates to feat/c instead. The denial for feat/b names that route
        # and --unroute, rather than just listing 8 missing gates with no
        # explanation of why they never landed.
        self.cli(["--init", "small-medium", "--branch", "feat/b"], self.wt_b)
        self.init_routed("feat/c", self.wt_b)
        r = self.enforce("gh pr create --head feat/b", self.wt_b)
        self.assertEqual(r.returncode, 2, r.stderr)
        err = r.stderr.decode()
        self.assertIn("feat/c", err)
        self.assertIn("--unroute", err)

    def test_enforce_denial_says_nothing_about_a_route_when_there_is_none(self):
        # The route-naming line must be conditional on an actual route pointing
        # elsewhere — an unrouted worktree with missing gates gets the plain
        # denial, not a false "your gates are routed away" hint.
        self.cli(["--init", "small-medium", "--branch", "feat/b"], self.wt_b)
        r = self.enforce("gh pr create --head feat/b", self.wt_b)
        self.assertEqual(r.returncode, 2, r.stderr)
        err = r.stderr.decode()
        self.assertNotIn("--unroute", err)
        self.assertNotIn("routed", err.lower())

    def test_two_worktrees_driving_one_target_both_record(self):
        # B and C both gate A. Both their skill gates belong on A — and neither
        # may knock the other back onto its own branch.
        self.init_routed("feat/a", self.wt_b)
        self.init_routed("feat/a", self.wt_c)
        self.skill("grumpy:review", self.wt_b)
        self.skill("simplify", self.wt_c)
        self.assertEqual(set(self.gates("feat/a")), {"grumpy-review", "simplify"})
        self.assertNotIn("feat/b", read_json(self.gp))
        self.assertNotIn("feat/c", read_json(self.gp))

    def test_unroute_by_one_driver_leaves_the_other_routed(self):
        self.init_routed("feat/a", self.wt_b)
        self.init_routed("feat/a", self.wt_c)
        self.cli(["--unroute"], self.wt_b)
        self.skill("grumpy:review", self.wt_c)
        self.assertIn("grumpy-review", self.gates("feat/a"))

    def test_bash_gates_still_use_branch_flag(self):
        # The manual half of --worktree keeps working unchanged alongside routing.
        self.init_routed("feat/c", self.wt_b, tier="tiny")
        for g in ("tests", "lint", "typecheck"):
            self.assertEqual(
                self.cli(["--record", g, "--branch", "feat/c"], self.wt_b).returncode, 0
            )
        self.assertEqual(set(self.gates("feat/c")), {"tests", "lint", "typecheck"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
