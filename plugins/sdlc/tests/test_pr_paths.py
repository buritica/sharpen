#!/usr/bin/env python3
"""Every way to open a PR goes through the gate (sharpen audit area 2).

The gate used to match only a Bash call whose argv was `gh pr create`. These
tests drive the real enforce hook as a subprocess, the way Claude Code, Codex
and pi reach it, with an armed, incomplete cycle on `feat/x`, and assert which
paths block. The rows mirror the path matrix in the area 2 audit, including
the paths deliberately left to the git pre-push floor.
"""

import importlib.util
import json
import os
import re
import shutil
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
    REMOTE = "https://github.com/o/r.git"

    def setUp(self):
        self.repo = make_repo(remote=self.REMOTE)
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
        "gh api -X POST repos/:owner/:repo/pulls -f head=feat/x",
        "gh pr create -R github.com/o/r --fill",
        "gh pr create --repo=https://github.com/o/r --fill",
        # A -R naming a repo this checkout has no remote for (a fork without
        # an `upstream` remote): judged against the checkout, not allowed.
        "gh pr create -R upstream-owner/r --fill",
        'gh api graphql -f query="mutation{createPullRequest(input:{}){pullRequest{url}}}"',
        "glab mr create --fill",
        "glab mr create --source-branch feat/x --fill",
        "glab mr create -R o/r --fill",
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
        # Only from a cwd that is not a checkout: there is no store to read.
        r = self.enforce(
            "mcp__github__create_pull_request",
            {**self.ARGS, "owner": "x", "repo": "y"},
            cwd=os.path.realpath(tempfile.mkdtemp()),
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("No local checkout of x/y", r.stdout)

    def test_mcp_pr_for_an_unmatched_repo_from_a_checkout_judges_it(self):
        r = self.enforce(
            "mcp__github__create_pull_request", {**self.ARGS, "owner": "x", "repo": "y"}
        )
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("judged the checkout at", r.stderr)


class McpHeadFieldsTest(ArmedRepo):
    """Every MCP server spells the PR's source branch its own way. Reading only
    GitHub's `head` judged an Azure DevOps PR on the cwd's branch instead."""

    def setUp(self):
        super().setUp()
        git(self.repo, "checkout", "-q", "main")  # cwd branch has no cycle

    def test_each_alias_names_the_head(self):
        for field in pp.MCP_HEAD_FIELDS:
            with self.subTest(field=field):
                r = self.enforce(
                    "mcp__x__create_pull_request",
                    {"owner": "o", "repo": "r", field: BRANCH},
                )
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn(BRANCH, r.stderr)

    def test_azure_devops_source_ref_name_strips_refs_heads(self):
        r = self.enforce(
            "mcp__ado__repo_create_pull_request",
            {"repositoryId": "r", "sourceRefName": "refs/heads/feat/x"},
        )
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn('branch "feat/x"', r.stderr)

    def test_no_recognized_head_field_says_so(self):
        git(self.repo, "checkout", "-q", BRANCH)
        r = self.enforce("mcp__x__create_pull_request", {"owner": "o", "repo": "r"})
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("named no head field", r.stderr)


class ForkFlowTest(unittest.TestCase):
    """A gated fork checkout next to a clone of the upstream. `-R up/r` names a
    repo only the sibling's remote carries; the sibling's checked-out branch
    has no cycle, so judging it read as allow."""

    def setUp(self):
        self.parent = os.path.realpath(tempfile.mkdtemp())
        self.fork = make_repo(self.parent, "https://github.com/me/r.git")
        self.up = make_repo(self.parent, "https://github.com/up/r.git")
        git(self.up, "checkout", "-q", "main")
        self.env = {k: v for k, v in os.environ.items() if k != "SDLC_GATES_PATH"}
        r = run(RECORD, self.fork, env=self.env, args=("--init", "small-medium"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def enforce(self, tool, tool_input, cwd=None):
        payload = {"tool_name": tool, "tool_input": tool_input}
        return run(ENFORCE, cwd or self.fork, json.dumps(payload), self.env)

    def test_gh_dash_R_upstream_from_the_gated_fork_blocks(self):
        for cmd in (
            "gh pr create -R up/r --fill",
            "gh pr create -R up/r --head me:feat/x --fill",
        ):
            with self.subTest(cmd=cmd):
                r = self.enforce("Bash", {"command": cmd})
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn(BRANCH, r.stderr)

    def test_mcp_upstream_from_the_gated_fork_blocks(self):
        for args in (
            {"owner": "up", "repo": "r", "head": f"me:{BRANCH}", "base": "main"},
            {"owner": "up", "repo": "r", "base": "main"},  # no head: cwd branch
        ):
            with self.subTest(args=args):
                r = self.enforce("mcp__github__create_pull_request", args)
                self.assertEqual(r.returncode, 2, r.stderr)

    def test_fork_without_a_cycle_still_finds_the_upstream_clone(self):
        # Only when the cwd checkout has no cycle for the branch does the scan
        # go looking: arm the upstream clone instead and push from the fork.
        git(self.up, "checkout", "-q", BRANCH)
        run(RECORD, self.up, env=self.env, args=("--init", "small-medium"))
        git(self.fork, "checkout", "-q", "main")
        r = self.enforce(
            "mcp__github__create_pull_request",
            {"owner": "up", "repo": "r", "head": BRANCH, "base": "main"},
        )
        self.assertEqual(r.returncode, 2, r.stderr)


class ScanCapTest(unittest.TestCase):
    def test_child_scan_reads_at_most_max_scan_entries(self):
        parent = os.path.realpath(tempfile.mkdtemp())
        for i in range(5):
            os.makedirs(os.path.join(parent, f"d{i}", ".git"))
        real, pp.MAX_SCAN = pp.MAX_SCAN, 3
        try:
            self.assertEqual(len(pp._child_checkouts(parent)), 3)
        finally:
            pp.MAX_SCAN = real


class NoRemoteCheckoutTest(ArmedRepo):
    """A checkout with no remote at all still blocks on its own cycle, whatever
    repo a hint names: no remote can match, and that must not mean allow."""

    REMOTE = None

    def test_dash_R_blocks(self):
        for cmd in ("gh pr create -R o/r --fill", "glab mr create -R o/r --fill"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.bash(cmd).returncode, 2)

    def test_mcp_blocks(self):
        r = self.enforce(
            "mcp__github__create_pull_request",
            {"owner": "o", "repo": "r", "head": BRANCH, "base": "main"},
        )
        self.assertEqual(r.returncode, 2, r.stderr)


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
        for spec in (
            "-R o/r",
            "-R github.com/o/r",
            "--repo=https://github.com/o/r",
            "--repo https://github.com/o/r.git",
        ):
            with self.subTest(spec=spec):
                r = self.enforce(
                    "Bash",
                    {"command": f"gh pr create {spec} --head feat/x --fill"},
                    self.parent,
                )
                self.assertEqual(r.returncode, 2, r.stderr)

    def test_glab_dash_R_from_parent_dir_finds_the_checkout(self):
        r = self.enforce(
            "Bash",
            {"command": "glab mr create -R o/r --source-branch feat/x --fill"},
            self.parent,
        )
        self.assertEqual(r.returncode, 2, r.stderr)


class GitlabNestedGroupTest(unittest.TestCase):
    def test_mcp_project_id_with_nested_group_finds_the_checkout(self):
        parent = os.path.realpath(tempfile.mkdtemp())
        make_repo(parent, "git@github.com:o/other.git")
        repo = make_repo(parent, "https://gitlab.com/group/sub/r.git")
        env = {k: v for k, v in os.environ.items() if k != "SDLC_GATES_PATH"}
        r = run(RECORD, repo, env=env, args=("--init", "small-medium"))
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = {
            "tool_name": "mcp__gitlab__create_merge_request",
            "tool_input": {
                "project_id": "group/sub/r",
                "source_branch": BRANCH,
                "target_branch": "main",
            },
        }
        r = run(ENFORCE, parent, json.dumps(payload), env)
        self.assertEqual(r.returncode, 2, r.stderr)


class RepoMatchTest(unittest.TestCase):
    def test_remote_spellings_match_owner_repo(self):
        for url in (
            "https://github.com/o/r.git",
            "https://github.com/O/R",
            "git@github.com:o/r.git",
            "ssh://git@github.com/o/r.git",
            "http://local_proxy@127.0.0.1:43/git/o/r",
        ):
            with self.subTest(url=url):
                self.assertTrue(pp._spec_matches(url, "o/r"))

    def test_nested_groups_match_by_suffix(self):
        url = "https://gitlab.com/group/sub/r.git"
        self.assertTrue(pp._spec_matches(url, "group/sub/r"))
        self.assertTrue(pp._spec_matches(url, "Group/Sub/R.git"))
        self.assertTrue(pp._spec_matches(url, "sub/r"))
        self.assertFalse(pp._spec_matches(url, "other/sub/r"))
        self.assertFalse(pp._spec_matches(url, "r"))  # one segment never matches

    def test_hints_each_path_passes(self):
        seen = []

        def fake(workdir, hint, head=None):
            seen.append(hint)
            return workdir, True, []

        real, pp.resolve_checkout = pp.resolve_checkout, fake
        try:
            for cmd, want in (
                ("gh pr create -R github.com/o/r", "o/r"),
                ("gh pr create --repo=https://github.com/o/r.git", "o/r"),
                ("glab mr create -R group/sub/r", "group/sub/r"),
                ("glab mr create --repo=group/sub/r", "group/sub/r"),
                ("gh api -X POST repos/:owner/:repo/pulls -f head=x", None),
                ("gh api -X POST repos/{owner}/{repo}/pulls -f head=x", None),
                ("gh api -X POST repos/o/r/pulls -f head=x", "o/r"),
                ("hub pull-request -m t", None),
            ):
                with self.subTest(cmd=cmd):
                    seen.clear()
                    self.assertEqual(len(pp.shell_pr_opens(cmd)), 1)
                    self.assertEqual(seen, [want])
        finally:
            pp.resolve_checkout = real


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
        for name in (
            "mcp__github__list_issues",
            "Read",
            "mcp__github__push_files",
            # A remote Copilot agent's PR: no local branch to judge, and
            # pr_paths' `$`-anchored pattern doesn't take it either.
            "mcp__github__create_pull_request_with_copilot",
        ):
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
        # Even in block mode: a push hook must never wedge a push.
        git(self.repo, "config", "sharpen.prePush", "block")
        os.makedirs(os.path.dirname(self.store))
        with open(self.store, "w") as f:
            f.write("{ broken")
        r = self.pre_push(self.push_line())
        self.assertEqual(r.returncode, 0)
        self.assertIn("unreadable", r.stderr)

    def test_unexpected_error_fails_open_with_a_message(self):
        git(self.repo, "config", "sharpen.prePush", "block")
        os.makedirs(self.store)  # a directory where the store file should be
        r = self.pre_push(self.push_line())
        self.assertEqual(r.returncode, 0)
        self.assertIn("unexpected error, not checked", r.stderr)

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


class PrePushHeadRefspecTest(unittest.TestCase):
    """`git push -u origin HEAD` is how /sdlc:ship pushes. git reports its local
    ref as the literal `HEAD`, which the floor used to skip."""

    def setUp(self):
        self.repo = make_repo()
        self.store = os.path.join(self.repo, ".sharpen", "data", "gates.json")
        self.env = dict(os.environ, SDLC_GATES_PATH=self.store)
        self.bare = os.path.realpath(tempfile.mkdtemp())
        git(self.bare, "init", "-q", "--bare")
        git(self.repo, "remote", "set-url", "origin", self.bare)
        r = run(INSTALL, self.repo, env=self.env, args=("--mode", "block"))
        self.assertEqual(r.returncode, 0, r.stderr)
        run(RECORD, self.repo, env=self.env, args=("--init", "small-medium"))

    def push(self, *refspec):
        return subprocess.run(
            ["git", "-C", self.repo, "push", "-q", "-u", "origin", *refspec],
            capture_output=True,
            text=True,
            env=self.env,
        )

    def test_real_push_of_HEAD_is_refused_in_block_mode(self):
        p = self.push("HEAD")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("refusing the push", p.stderr)
        self.assertIn(BRANCH, p.stderr)

    def test_detached_head_falls_back_to_the_remote_ref(self):
        sha = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-q", "--detach")
        p = self.push(f"{sha}:refs/heads/{BRANCH}")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("refusing the push", p.stderr)


def load_installer():
    spec = importlib.util.spec_from_file_location("install_pre_push", INSTALL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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

    def test_core_hooks_path_check_says_it_cannot_tell(self):
        git(self.repo, "config", "core.hooksPath", ".githooks")
        r = self.install("--check")
        self.assertEqual(r.returncode, 0)
        self.assertIn("can't tell whether the chain line is present", r.stdout)

    def run_chain_hook(self, stdin="", env=None, sh=("sh",), prelude=""):
        """The printed chain line as the LAST line of someone else's hook,
        which is where people paste an 'add this line' snippet."""
        path = os.path.join(tempfile.mkdtemp(), "pre-push")
        with open(path, "w") as f:
            f.write(f"#!/bin/sh\n{prelude}echo hi\n{load_installer().chain_line()}\n")
        return subprocess.run(
            [*sh, path, "origin", "x"],
            input=stdin,
            capture_output=True,
            text=True,
            cwd=self.repo,
            env=env or os.environ,
        )

    def test_chain_line_exits_0_when_sdlc_is_not_configured(self):
        git(self.repo, "config", "--unset", "sharpen.sdlcScripts", check=False)
        self.assertEqual(self.run_chain_hook().returncode, 0)
        git(self.repo, "config", "sharpen.sdlcScripts", "/nonexistent/sdlc/scripts")
        self.assertEqual(self.run_chain_hook().returncode, 0)

    def test_chain_line_exits_0_under_sh_e_and_set_e(self):
        # husky v9 runs hooks as `sh -e`; hand-written hooks often `set -e`.
        # `git config --get` exits 1 on an unset key, which used to exit there.
        for kw in ({"sh": ("sh", "-e")}, {"prelude": "set -e\n"}):
            with self.subTest(**kw):
                git(self.repo, "config", "--unset", "sharpen.sdlcScripts", check=False)
                r = self.run_chain_hook(**kw)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stderr, "")
                git(self.repo, "config", "sharpen.sdlcScripts", "/nonexistent/sdlc")
                r = self.run_chain_hook(**kw)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stderr.count("skipped"), 1, r.stderr)

    def test_lefthook_gets_a_yml_snippet_with_use_stdin(self):
        os.makedirs(os.path.dirname(self.hook), exist_ok=True)
        with open(self.hook, "w") as f:
            f.write("#!/bin/sh\n# lefthook generated\nlefthook run pre-push\n")
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("lefthook.yml", r.stdout)
        self.assertIn("use_stdin: true", r.stdout)
        self.assertIn("pre-push.py", r.stdout)

    def test_shim_is_written_without_leftover_temp_files(self):
        self.install()
        left = [
            n
            for n in os.listdir(os.path.dirname(self.hook))
            if n.startswith(".pre-push.")
        ]
        self.assertEqual(left, [])
        self.assertTrue(os.access(self.hook, os.X_OK))

    def test_shim_fails_open_without_python3_on_path(self):
        self.install()
        bindir = tempfile.mkdtemp()
        os.symlink(shutil.which("git"), os.path.join(bindir, "git"))
        p = subprocess.run(
            [shutil.which("sh"), self.hook, "origin", "x"],
            input="",
            capture_output=True,
            text=True,
            cwd=self.repo,
            env=dict(os.environ, PATH=bindir),
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("python3 not found", p.stderr)

    def test_chain_line_still_propagates_a_block(self):
        store = os.path.join(self.repo, ".sharpen", "data", "gates.json")
        env = dict(os.environ, SDLC_GATES_PATH=store)
        run(RECORD, self.repo, env=env, args=("--init", "small-medium"))
        git(self.repo, "config", "sharpen.sdlcScripts", SCRIPTS)
        git(self.repo, "config", "sharpen.prePush", "block")
        sha = git(self.repo, "rev-parse", "HEAD")
        line = f"refs/heads/{BRANCH} {sha} refs/heads/{BRANCH} {'0' * 40}\n"
        r = self.run_chain_hook(line, env)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("refusing the push", r.stderr)

    def test_an_outdated_shim_of_ours_is_updated(self):
        os.makedirs(os.path.dirname(self.hook), exist_ok=True)
        mod = load_installer()
        with open(self.hook, "w") as f:
            f.write(f"#!/bin/sh\n{mod.MARKER} (old)\nexit 0\n")
        self.assertEqual(self.install("--check").returncode, 1)  # stale
        r = self.install()
        self.assertIn("updated", r.stdout)
        with open(self.hook) as f:
            self.assertEqual(f.read(), mod.SHIM)
        self.assertEqual(self.install("--check").returncode, 0)

    def test_common_dir_without_path_format_support(self):
        # git < 2.31 rejects --path-format; the fallback resolves the plain
        # --git-common-dir against --root.
        mod = load_installer()
        real = mod.subprocess.run

        def old_git(cmd, *a, **kw):
            if "--path-format=absolute" in cmd:
                return subprocess.CompletedProcess(cmd, 129, "", "unknown option")
            return real(cmd, *a, **kw)

        mod.subprocess.run = old_git
        try:
            self.assertEqual(mod.common_dir(self.repo), os.path.join(self.repo, ".git"))
        finally:
            mod.subprocess.run = real

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
