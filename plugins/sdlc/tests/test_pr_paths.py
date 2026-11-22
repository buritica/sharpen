#!/usr/bin/env python3
"""Every way to open a PR goes through the gate (sharpen audit area 2).

The gate used to match only a Bash call whose argv was `gh pr create`. These
tests drive the real enforce hook as a subprocess, the way Claude Code, Codex
and pi reach it, with an armed, incomplete cycle on `feat/x`, and assert which
paths block. The rows mirror the path matrix in the area 2 audit, including
the paths deliberately left to the git pre-push floor.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, "..", "scripts"))
PLUGIN = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, SCRIPTS)

os.environ["SDLC_CROSS_REPO_ROUTES_PATH"] = os.path.join(
    tempfile.mkdtemp(), "cross-repo-routes.json"
)
# Event-only cycles: these tests are about which calls are PRs, not about
# receipts (test_receipts.py covers staleness on these paths).
os.environ["SDLC_GATE_EVIDENCE"] = "events"

import pr_paths as pp  # noqa: E402

ENFORCE = os.path.join(SCRIPTS, "enforce-sdlc-gates.py")
RECORD = os.path.join(SCRIPTS, "record-gate.py")
PRE_PUSH = os.path.join(SCRIPTS, "pre-push.py")
INSTALL = os.path.join(SCRIPTS, "install-pre-push.py")
BRANCH = "feat/x"


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=check
    ).stdout.strip()


def make_repo(parent=None, remote="https://github.com/o/r.git"):
    repo = os.path.realpath(tempfile.mkdtemp(dir=parent))
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    git(repo, "checkout", "-q", "-b", BRANCH)
    if remote:
        git(repo, "remote", "add", "origin", remote)
    return repo


def run(script, cwd, stdin="", env=None, args=()):
    return subprocess.run(
        ["python3", script, *args],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env or os.environ,
    )


class ArmedRepo(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.store = os.path.join(self.repo, ".sharpen", "data", "gates.json")
        self.env = dict(os.environ, SDLC_GATES_PATH=self.store)
        r = run(RECORD, self.repo, env=self.env, args=("--init", "small-medium"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def enforce(self, tool, tool_input, cwd=None, env=None):
        payload = {"tool_name": tool, "tool_input": tool_input}
        return run(ENFORCE, cwd or self.repo, json.dumps(payload), env or self.env)

    def bash(self, command):
        return self.enforce("Bash", {"command": command})


class ShellPathsTest(ArmedRepo):
    BLOCKED = [
        "gh pr create --fill",
        "gh pr create --web",
        "gh pr create --draft --fill",
        "gh pr new --fill",
        'gh pr create --head "$(git branch --show-current)" --fill',
        "B=feat/x; gh pr create --head $B --fill",
        "gh pr create --head HEAD --fill",
        "gh pr create --head @ --fill",
        "xargs -I{} gh pr create --head {} <<< feat/x",
        "gh api repos/o/r/pulls -f head=feat/x -f base=main -f title=t",
        "gh api --method POST repos/o/r/pulls --input body.json",
        "gh api repos/{owner}/{repo}/pulls -f head=feat/x -f base=main",
        'gh api graphql -f query="mutation{createPullRequest(input:{}){pullRequest{url}}}"',
        "glab mr create --fill",
        "glab mr create --source-branch feat/x --fill",
        "hub pull-request -m t",
        "git push -o merge_request.create origin feat/x",
        "git push --push-option=merge_request.create origin HEAD",
    ]
    # Not PRs at all, or a draft already gated at create (coordinator
    # decision: gh pr ready is not gated).
    ALLOWED = [
        'echo "gh pr create"',
        "gh pr list",
        "gh pr view 12",
        "gh pr ready 12",
        "gh api repos/o/r/pulls",  # a GET: listing, not creating
        "gh api -X GET repos/o/r/pulls -f state=open",
        "git push origin feat/x",
        "glab mr list",
    ]
    # PR paths the tool hook deliberately can't read. The pre-push floor is
    # what meets them; these rows document the boundary so a change in it is
    # a visible decision.
    LEFT_TO_PRE_PUSH = [
        "make pr",
        "./scripts/open-pr.sh",
        "python3 -c \"import subprocess; subprocess.run(['gh','pr','create'])\"",
        "curl -X POST https://api.github.com/repos/o/r/pulls -d @b.json",
        "gh mypr",
        "GH=gh; $GH pr create",
    ]

    def test_blocked_paths(self):
        for cmd in self.BLOCKED:
            with self.subTest(cmd=cmd):
                r = self.bash(cmd)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("feat/x", r.stderr)

    def test_not_a_pr(self):
        for cmd in self.ALLOWED:
            with self.subTest(cmd=cmd):
                self.assertEqual(self.bash(cmd).returncode, 0)

    def test_paths_left_to_the_pre_push_floor(self):
        for cmd in self.LEFT_TO_PRE_PUSH:
            with self.subTest(cmd=cmd):
                self.assertEqual(self.bash(cmd).returncode, 0)

    def test_monitor_tool_runs_shell_and_is_checked(self):
        r = self.enforce("Monitor", {"command": "gh pr create --fill"})
        self.assertEqual(r.returncode, 2, r.stderr)

    def test_expanded_head_says_how_it_was_judged(self):
        r = self.bash('gh pr create --head "$(git branch --show-current)"')
        self.assertIn("expanded by the shell", r.stderr)

    def test_head_of_a_done_branch_is_still_allowed(self):
        # The head fix must not turn a correct literal head into the workdir's.
        git(self.repo, "branch", "done/branch")
        cli = lambda *a: run(RECORD, self.repo, env=self.env, args=a)  # noqa: E731
        cli("--init", "tiny", "--branch", "done/branch")
        for g in ("tests", "lint", "typecheck"):
            cli("--record", g, "--branch", "done/branch")
        self.assertEqual(self.bash("gh pr create --head done/branch").returncode, 0)


class McpPathsTest(ArmedRepo):
    ARGS = {"owner": "o", "repo": "r", "head": BRANCH, "base": "main", "title": "t"}

    def test_github_mcp_create_pull_request_blocks(self):
        for tool in (
            "mcp__github__create_pull_request",  # Claude Code / Codex
            "mcp_github_create_pull_request",  # pi-mcp-extension
            "create_pull_request",
        ):
            with self.subTest(tool=tool):
                r = self.enforce(tool, self.ARGS)
                self.assertEqual(r.returncode, 2, r.stderr)

    def test_draft_pr_through_mcp_is_gated(self):
        # Coordinator decision: ready-for-review isn't gated, because a draft
        # is gated when it's created. This is the half that makes that true.
        r = self.enforce(
            "mcp__github__create_pull_request", {**self.ARGS, "draft": True}
        )
        self.assertEqual(r.returncode, 2, r.stderr)

    def test_gitlab_mcp_create_merge_request_blocks(self):
        r = self.enforce(
            "mcp__gitlab__create_merge_request",
            {"project_id": "o/r", "source_branch": BRANCH, "target_branch": "main"},
        )
        self.assertEqual(r.returncode, 2, r.stderr)

    def test_other_mcp_tools_are_not_prs(self):
        for tool, args in (
            ("mcp__github__update_pull_request", {"pullNumber": 1, "draft": False}),
            ("mcp__github__list_pull_requests", {"owner": "o", "repo": "r"}),
            ("mcp__github__push_files", {"owner": "o", "repo": "r", "branch": BRANCH}),
        ):
            with self.subTest(tool=tool):
                self.assertEqual(self.enforce(tool, args).returncode, 0)

    def test_mcp_pr_for_branch_without_cycle_allows_with_a_note(self):
        r = self.enforce(
            "mcp__github__create_pull_request", {**self.ARGS, "head": "pushed/by-mcp"}
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("No gate cycle", r.stdout)

    def test_mcp_pr_for_a_repo_with_no_checkout_allows_with_a_note(self):
        r = self.enforce(
            "mcp__github__create_pull_request", {**self.ARGS, "owner": "x", "repo": "y"}
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("No local checkout of x/y", r.stdout)


class MultiRepoSessionTest(unittest.TestCase):
    """A cloud session sits in the parent of several checkouts. Judged from
    there, the store lookup used to miss and every PR read as "no cycle"."""

    def setUp(self):
        self.parent = os.path.realpath(tempfile.mkdtemp())
        self.other = make_repo(self.parent, "git@github.com:o/other.git")
        self.repo = make_repo(self.parent, "http://proxy@127.0.0.1:1/git/o/r")
        # The real default store (no SDLC_GATES_PATH): resolved per checkout.
        self.env = {k: v for k, v in os.environ.items() if k != "SDLC_GATES_PATH"}
        r = run(RECORD, self.repo, env=self.env, args=("--init", "small-medium"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def enforce(self, tool, tool_input, cwd):
        payload = {"tool_name": tool, "tool_input": tool_input}
        return run(ENFORCE, cwd, json.dumps(payload), self.env)

    def test_mcp_from_parent_dir_finds_the_checkout(self):
        r = self.enforce(
            "mcp__github__create_pull_request",
            {"owner": "O", "repo": "r", "head": BRANCH, "base": "main"},
            self.parent,
        )
        self.assertEqual(r.returncode, 2, r.stderr)

    def test_mcp_from_a_sibling_checkout_finds_the_right_one(self):
        r = self.enforce(
            "mcp__github__create_pull_request",
            {"owner": "o", "repo": "r", "head": BRANCH, "base": "main"},
            self.other,
        )
        self.assertEqual(r.returncode, 2, r.stderr)

    def test_gh_dash_R_from_parent_dir_finds_the_checkout(self):
        r = self.enforce(
            "Bash", {"command": "gh pr create -R o/r --head feat/x --fill"}, self.parent
        )
        self.assertEqual(r.returncode, 2, r.stderr)


class RepoSlugTest(unittest.TestCase):
    def test_remote_spellings(self):
        for url in (
            "https://github.com/o/r.git",
            "https://github.com/o/r",
            "git@github.com:o/r.git",
            "ssh://git@github.com/o/r.git",
            "http://local_proxy@127.0.0.1:43/git/o/r",
            "https://gitlab.com/group/sub/o/r.git",
        ):
            with self.subTest(url=url):
                self.assertEqual(pp._repo_slug(url), "o/r")


class HookWiringTest(unittest.TestCase):
    """The host manifests route the new paths to the same script."""

    def test_hooks_json_matches_monitor_and_mcp_pr_tools_only(self):
        with open(os.path.join(PLUGIN, "hooks", "hooks.json")) as f:
            pre = json.load(f)["hooks"]["PreToolUse"]
        matchers = [
            e["matcher"]
            for e in pre
            if any("enforce-sdlc-gates.py" in h["command"] for h in e["hooks"])
        ]
        hit = lambda name: any(re.fullmatch(m, name) for m in matchers)  # noqa: E731
        for name in (
            "Bash",
            "Monitor",
            "mcp__github__create_pull_request",
            "mcp__gitlab__create_merge_request",
        ):
            self.assertTrue(hit(name), name)
        for name in ("mcp__github__list_issues", "Read", "mcp__github__push_files"):
            self.assertFalse(hit(name), name)

    def test_pi_extension_filter_matches_python(self):
        with open(os.path.join(PLUGIN, "extensions", "sdlc-hooks.ts")) as f:
            ts = f.read()
        m = re.search(r"const MCP_PR_TOOL = /(.+)/;", ts)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), pp._MCP_PR_TOOL.pattern)


class PrePushTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.store = os.path.join(self.repo, ".sharpen", "data", "gates.json")
        self.env = dict(os.environ, SDLC_GATES_PATH=self.store)
        self.sha = git(self.repo, "rev-parse", "HEAD")

    def push_line(self, branch=BRANCH, sha=None):
        sha = sha or self.sha
        return f"refs/heads/{branch} {sha} refs/heads/{branch} {'0' * 40}\n"

    def pre_push(self, stdin, env=None):
        return run(PRE_PUSH, self.repo, stdin, env or self.env)

    def arm(self):
        run(RECORD, self.repo, env=self.env, args=("--init", "small-medium"))

    def test_no_cycle_is_silent(self):
        r = self.pre_push(self.push_line())
        self.assertEqual((r.returncode, r.stderr), (0, ""))

    def test_warns_by_default(self):
        self.arm()
        r = self.pre_push(self.push_line())
        self.assertEqual(r.returncode, 0)
        self.assertIn("not ready for a PR", r.stderr)

    def test_block_mode_refuses_and_env_lets_one_through(self):
        self.arm()
        git(self.repo, "config", "sharpen.prePush", "block")
        r = self.pre_push(self.push_line())
        self.assertEqual(r.returncode, 1)
        self.assertIn("SDLC_ALLOW_PUSH=1", r.stderr)
        r = self.pre_push(self.push_line(), dict(self.env, SDLC_ALLOW_PUSH="1"))
        self.assertEqual(r.returncode, 0)

    def test_off_mode_is_silent(self):
        self.arm()
        git(self.repo, "config", "sharpen.prePush", "off")
        r = self.pre_push(self.push_line())
        self.assertEqual((r.returncode, r.stderr), (0, ""))

    def test_deletes_and_tags_are_ignored(self):
        self.arm()
        git(self.repo, "config", "sharpen.prePush", "block")
        stdin = (
            f"(delete) {'0' * 40} refs/heads/{BRANCH} {self.sha}\n"
            f"refs/tags/v1 {self.sha} refs/tags/v1 {'0' * 40}\n"
        )
        self.assertEqual(self.pre_push(stdin).returncode, 0)

    def test_corrupt_store_fails_open_with_a_message(self):
        os.makedirs(os.path.dirname(self.store))
        with open(self.store, "w") as f:
            f.write("{ broken")
        r = self.pre_push(self.push_line())
        self.assertEqual(r.returncode, 0)
        self.assertIn("unreadable", r.stderr)

    def test_real_push_runs_the_installed_shim(self):
        bare = os.path.realpath(tempfile.mkdtemp())
        git(bare, "init", "-q", "--bare")
        git(self.repo, "remote", "set-url", "origin", bare)
        r = run(INSTALL, self.repo, env=self.env, args=("--mode", "block"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.arm()
        p = subprocess.run(
            ["git", "-C", self.repo, "push", "-q", "origin", BRANCH],
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("refusing the push", p.stderr)


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.hook = os.path.join(self.repo, ".git", "hooks", "pre-push")

    def install(self, *args):
        return run(INSTALL, self.repo, args=args)

    def test_installs_a_path_free_shim_and_records_the_scripts_dir(self):
        self.assertEqual(self.install().returncode, 0)
        with open(self.hook) as f:
            shim = f.read()
        self.assertNotIn(SCRIPTS, shim)  # moves with the plugin cache
        self.assertEqual(git(self.repo, "config", "sharpen.sdlcScripts"), SCRIPTS)
        self.assertEqual(self.install("--check").returncode, 0)

    def test_idempotent_and_sets_mode(self):
        self.install()
        r = self.install("--mode", "block")
        self.assertIn("already installed", r.stdout)
        self.assertEqual(git(self.repo, "config", "sharpen.prePush"), "block")

    def test_never_overwrites_a_foreign_hook(self):
        os.makedirs(os.path.dirname(self.hook), exist_ok=True)
        with open(self.hook, "w") as f:
            f.write("#!/bin/sh\necho mine\n")
        r = self.install()
        self.assertEqual(r.returncode, 0)
        self.assertIn("managed elsewhere", r.stdout)
        with open(self.hook) as f:
            self.assertEqual(f.read(), "#!/bin/sh\necho mine\n")

    def test_core_hooks_path_gets_a_chain_line(self):
        git(self.repo, "config", "core.hooksPath", ".githooks")
        r = self.install()
        self.assertIn("pre-push.py", r.stdout)
        self.assertFalse(os.path.exists(self.hook))

    def test_shim_fails_open_when_scripts_are_gone(self):
        self.install()
        git(self.repo, "config", "sharpen.sdlcScripts", "/nonexistent/sdlc/scripts")
        p = subprocess.run(
            ["sh", self.hook, "origin", "x"],
            input="",
            capture_output=True,
            text=True,
            cwd=self.repo,
        )
        self.assertEqual(p.returncode, 0)
        self.assertIn("not checked", p.stderr)


if __name__ == "__main__":
    unittest.main()
