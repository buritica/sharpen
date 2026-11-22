#!/usr/bin/env python3
"""Artifact folders keep themselves out of git.

Every folder sharpen writes inside a repo (`.sharpen/data/`, `.claude/sdlc/`,
`.claude/grumpy/`, `.claude/worktrees/`) drops a `.gitignore` holding `*` into
itself when it is created, so a repo that never ran `/sdlc:init` (or ran an
older one) still never sees gate state or reports in `git status`. These
tests run the real writers in throwaway repos with no `.gitignore` of their
own, and pin the prose that does the same in the command files.

Run: python3 plugins/sdlc/tests/test_artifact_ignore.py
"""

import glob
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
PLUGINS = os.path.normpath(os.path.join(HERE, "..", ".."))
REPO_ROOT = os.path.normpath(os.path.join(PLUGINS, ".."))
RECORD = os.path.join(SCRIPTS, "record-gate.py")
RECEIPT = os.path.join(PLUGINS, "grumpy", "scripts", "receipt.py")
INIT_MD = os.path.join(PLUGINS, "sdlc", "commands", "init.md")
PLAN_MD = os.path.join(PLUGINS, "sdlc", "commands", "plan.md")
TEMPLATE = os.path.join(PLUGINS, "sdlc", "templates", "agents-sdlc.md")
sys.path.insert(0, SCRIPTS)

os.environ["SDLC_CROSS_REPO_ROUTES_PATH"] = os.path.join(
    tempfile.mkdtemp(), "cross-repo-routes.json"
)
os.environ.pop("SDLC_GATES_PATH", None)

import gate_store as gs  # noqa: E402
from artifact_dirs import SELF_IGNORE_TEXT  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "session_start", os.path.join(SCRIPTS, "session-start.py")
)
session_start = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(session_start)

BRANCH = "feat/x"

# The one list every layer must agree on: what init appends, what the AGENTS
# block documents, and what this repo ignores.
ARTIFACT_DIRS = (
    ".sharpen/data/",
    ".claude/data/",
    ".claude/sdlc/",
    ".claude/grumpy/",
    ".claude/worktrees/",
)


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, check=check, text=True
    )


def make_repo():
    repo = os.path.realpath(tempfile.mkdtemp())
    # A real default branch, so a cycle resolves a base and writes
    # `.claude/sdlc/<branch>/base.json` too.
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    git(repo, "checkout", "-q", "-b", BRANCH)
    git(repo, "commit", "-q", "--allow-empty", "-m", "work")
    return repo


def untracked(repo):
    """Every path git would offer to `git add -A`."""
    out = git(repo, "status", "--porcelain", "--untracked-files=all").stdout
    return [line[3:] for line in out.splitlines()]


def ignored(repo, path):
    return git(repo, "check-ignore", "-q", path, check=False).returncode == 0


class RepoTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def init_cycle(self, env=None):
        subprocess.run(
            [sys.executable, RECORD, "--init", "tiny", "--branch", BRANCH],
            cwd=self.repo,
            capture_output=True,
            check=True,
            env={**os.environ, **(env or {})},
        )


class GateStateTest(RepoTest):
    def test_gate_state_never_shows_up_in_git_status(self):
        # The buritica/ai case: no repo .gitignore entry for .sharpen/data/.
        self.init_cycle()
        self.assertTrue(
            os.path.isfile(os.path.join(self.repo, ".sharpen", "data", "gates.json"))
        )
        self.assertEqual(untracked(self.repo), [])
        self.assertTrue(ignored(self.repo, ".sharpen/data/gates.json"))
        with open(os.path.join(self.repo, ".sharpen", "data", ".gitignore")) as f:
            self.assertEqual(f.read(), SELF_IGNORE_TEXT)

    def test_the_base_record_never_shows_up_in_git_status(self):
        # gate_base.write_base_file, called whenever a cycle resolves a base.
        self.init_cycle()
        base = os.path.join(self.repo, ".claude", "sdlc", BRANCH, "base.json")
        self.assertTrue(os.path.isfile(base), "no base resolved; fixture broken")
        self.assertEqual(untracked(self.repo), [])
        self.assertTrue(ignored(self.repo, f".claude/sdlc/{BRANCH}/base.json"))

    def test_an_existing_state_folder_heals_on_its_next_write(self):
        data = os.path.join(self.repo, ".sharpen", "data")
        os.makedirs(data)
        with open(os.path.join(data, "gates.json"), "w") as f:
            json.dump({}, f)
        self.assertEqual(untracked(self.repo), [".sharpen/data/gates.json"])
        self.init_cycle()
        self.assertEqual(untracked(self.repo), [])

    def test_an_existing_gitignore_in_the_folder_is_left_alone(self):
        data = os.path.join(self.repo, ".sharpen", "data")
        os.makedirs(data)
        with open(os.path.join(data, ".gitignore"), "w") as f:
            f.write("gates.json\n")
        self.init_cycle()
        with open(os.path.join(data, ".gitignore")) as f:
            self.assertEqual(f.read(), "gates.json\n")

    def test_simplify_config_stays_tracked(self):
        # .sharpen/ holds the committed simplify policy: it must never be
        # ignored, so no .gitignore goes into .sharpen/ itself.
        os.makedirs(os.path.join(self.repo, ".sharpen"))
        with open(os.path.join(self.repo, ".sharpen", "simplify.json"), "w") as f:
            json.dump({"thresholds": {}}, f)
        self.init_cycle()
        self.assertFalse(
            os.path.exists(os.path.join(self.repo, ".sharpen", ".gitignore"))
        )
        self.assertFalse(ignored(self.repo, ".sharpen/simplify.json"))
        self.assertEqual(untracked(self.repo), [".sharpen/simplify.json"])

    def test_an_overridden_store_path_is_not_touched(self):
        # $SDLC_GATES_PATH can point anywhere; that folder isn't ours to mark.
        elsewhere = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, elsewhere, ignore_errors=True)
        self.init_cycle({"SDLC_GATES_PATH": os.path.join(elsewhere, "gates.json")})
        self.assertTrue(os.path.isfile(os.path.join(elsewhere, "gates.json")))
        self.assertFalse(os.path.exists(os.path.join(elsewhere, ".gitignore")))

    def test_legacy_store_folder_is_not_touched(self):
        legacy = os.path.join(self.repo, ".claude", "data")
        os.makedirs(legacy)
        gs.save_store(os.path.join(legacy, "gates.json"), {})
        self.assertFalse(os.path.exists(os.path.join(legacy, ".gitignore")))

    def test_capability_manifest_lands_in_an_ignored_folder(self):
        path = os.path.join(self.repo, ".sharpen", "data", "capabilities.claude.json")
        session_start.write_manifest(path, {"protocol_version": "1"})
        self.assertEqual(untracked(self.repo), [])


class GrumpyReceiptTest(RepoTest):
    def test_receipt_folder_never_shows_up_in_git_status(self):
        # A receipt needs the pass's non-empty report next to it.
        art = os.path.join(self.repo, ".claude", "grumpy", BRANCH)
        os.makedirs(art)
        with open(os.path.join(art, "review.md"), "w") as f:
            f.write("# Review\n")
        subprocess.run(
            [sys.executable, RECEIPT, "review", "--worktree", self.repo],
            capture_output=True,
            check=True,
        )
        self.assertTrue(
            os.path.isfile(
                os.path.join(
                    self.repo, ".claude", "grumpy", BRANCH, "receipts", "review.json"
                )
            )
        )
        self.assertEqual(untracked(self.repo), [])
        with open(os.path.join(self.repo, ".claude", "grumpy", ".gitignore")) as f:
            self.assertEqual(f.read(), "*\n")


def _command_files():
    return sorted(glob.glob(os.path.join(PLUGINS, "*", "commands", "*.md")))


MKDIR = 'mkdir -p "$ARTIFACT_DIR"'
IGNORE_LINE = re.compile(
    r'^\[ -e "\$GIT_ROOT/\.claude/(grumpy|sdlc)/\.gitignore" \] \|\| '
    r"printf '\*\\n' > \"\$GIT_ROOT/\.claude/\1/\.gitignore\""
)


class CommandProseTest(RepoTest):
    """The commands create their folders in shell, so the same rule lives
    there as one line beside each `mkdir -p`. Pin it, and run it."""

    def test_every_artifact_mkdir_is_followed_by_the_ignore_line(self):
        seen = 0
        for path in _command_files():
            with open(path) as f:
                lines = f.read().splitlines()
            for i, line in enumerate(lines):
                if line.strip() != MKDIR:
                    continue
                seen += 1
                where = f"{os.path.relpath(path, PLUGINS)}:{i + 1}"
                dir_line = next(
                    (
                        prev
                        for prev in reversed(lines[:i])
                        if prev.startswith("ARTIFACT_DIR=")
                    ),
                    "",
                )
                folder = "grumpy" if "/.claude/grumpy/" in dir_line else "sdlc"
                nxt = lines[i + 1] if i + 1 < len(lines) else ""
                m = IGNORE_LINE.match(nxt)
                self.assertIsNotNone(m, f"{where}: no ignore line after mkdir")
                self.assertEqual(m.group(1), folder, f"{where}: wrong folder")
        # 12 grumpy + 2 sdlc today; a refactor that drops them all must fail.
        self.assertGreaterEqual(seen, 10)

    def _distinct_ignore_lines(self):
        lines = set()
        for path in _command_files():
            with open(path) as f:
                for line in f.read().splitlines():
                    if IGNORE_LINE.match(line):
                        lines.add(line)
        return sorted(lines)

    def test_every_ignore_line_works_in_a_real_shell(self):
        snippets = self._distinct_ignore_lines()
        self.assertEqual(len(snippets), 2)  # one for grumpy, one for sdlc
        for folder in ("grumpy", "sdlc"):
            art = os.path.join(self.repo, ".claude", folder, BRANCH)
            os.makedirs(art)
            with open(os.path.join(art, "report.md"), "w") as f:
                f.write("report\n")
        for snippet in snippets:
            for _ in range(2):  # idempotent
                subprocess.run(
                    ["bash", "-c", snippet],
                    env={**os.environ, "GIT_ROOT": self.repo},
                    check=True,
                )
        self.assertEqual(untracked(self.repo), [])

    def test_plan_worktree_folder_ignores_itself(self):
        with open(PLAN_MD) as f:
            lines = f.read().splitlines()
        start = lines.index("mkdir -p .claude/worktrees")
        snippet = "\n".join(lines[start : start + 2])
        self.assertIn(".claude/worktrees/.gitignore", snippet)
        self.assertTrue(lines[start + 2].startswith("git worktree add"))
        subprocess.run(["bash", "-c", snippet], cwd=self.repo, check=True)
        git(self.repo, "worktree", "add", "-q", "-b", "feat/y", ".claude/worktrees/y")
        self.assertEqual(untracked(self.repo), [])


class WorktreeCommandsTest(unittest.TestCase):
    def test_every_command_that_adds_a_worktree_marks_the_folder_first(self):
        # /sdlc:spec and /sdlc:plan both open worktrees under .claude/worktrees/.
        seen = 0
        for path in _command_files():
            with open(path) as f:
                lines = f.read().splitlines()
            for i, line in enumerate(lines):
                if line.startswith("git worktree add") and ".claude/worktrees/" in line:
                    seen += 1
                    self.assertEqual(
                        lines[i - 1],
                        "[ -e .claude/worktrees/.gitignore ] || printf '*\\n' > "
                        ".claude/worktrees/.gitignore  # the folder ignores itself, on any host",
                        f"{os.path.relpath(path, PLUGINS)}:{i + 1}",
                    )
                    self.assertEqual(lines[i - 2], "mkdir -p .claude/worktrees")
        self.assertGreaterEqual(seen, 2)


class InitProbeTest(RepoTest):
    """init dedupes its ignore block with `git check-ignore -v`. The folder's
    own self-ignore file also matches that probe, so init has to look at the
    source of the match, or it would never add the repo-level line."""

    def test_self_ignore_matches_the_probe_from_its_own_file(self):
        self.init_cycle()
        out = git(self.repo, "check-ignore", "-v", ".sharpen/data/probe").stdout
        self.assertTrue(out.startswith(".sharpen/data/.gitignore:"), out)

    def test_a_repo_level_entry_is_reported_from_the_repo_gitignore(self):
        self.init_cycle()
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write(".sharpen/data/\n")
        out = git(self.repo, "check-ignore", "-v", ".sharpen/data/probe").stdout
        self.assertTrue(out.startswith(".gitignore:1:"), out)

    def test_init_tells_the_agent_to_discount_the_folders_own_file(self):
        with open(INIT_MD) as f:
            text = " ".join(f.read().split())
        self.assertIn("git check-ignore -v", text)
        self.assertIn("folder's own `.gitignore`", text)
        self.assertIn("append the line anyway", text)


class WriteFailureTest(RepoTest):
    def test_a_failed_write_leaves_no_empty_ignore_file(self):
        import artifact_dirs
        from unittest import mock

        data = os.path.join(self.repo, ".sharpen", "data")
        os.makedirs(data)
        with mock.patch.object(
            artifact_dirs.os, "fdopen", side_effect=OSError("disk full")
        ):
            artifact_dirs.ensure_self_ignored(data)
        self.assertFalse(os.path.exists(os.path.join(data, ".gitignore")))
        artifact_dirs.ensure_self_ignored(data)  # the next write heals it
        with open(os.path.join(data, ".gitignore")) as f:
            self.assertEqual(f.read(), SELF_IGNORE_TEXT)

    def test_only_sharpens_own_folders_get_one(self):
        import artifact_dirs

        for rel in (".sharpen", ".claude/data", "custom/data", ".claude/sdlc/x"):
            folder = os.path.join(self.repo, rel)
            os.makedirs(folder, exist_ok=True)
            artifact_dirs.ensure_self_ignored(folder)
            self.assertFalse(os.path.exists(os.path.join(folder, ".gitignore")), rel)
        for rel in (".sharpen/data", ".claude/sdlc", ".claude/grumpy"):
            folder = os.path.join(self.repo, rel)
            os.makedirs(folder, exist_ok=True)
            artifact_dirs.ensure_self_ignored(folder)
            self.assertTrue(os.path.isfile(os.path.join(folder, ".gitignore")), rel)


class LayersAgreeTest(unittest.TestCase):
    """init's ignore block, the AGENTS.md artifacts list and this repo's own
    .gitignore name the same folders."""

    def test_init_block_lists_every_artifact_folder(self):
        with open(INIT_MD) as f:
            text = f.read()
        block = text.split("# Agent scratch", 1)[1].split("```", 1)[0]
        listed = [ln.strip() for ln in block.splitlines()[1:] if ln.strip()]
        self.assertEqual(sorted(listed), sorted(ARTIFACT_DIRS))
        self.assertNotIn(".sharpen/\n", block)

    def test_agents_template_documents_every_written_folder(self):
        with open(TEMPLATE) as f:
            section = f.read().split("### Artifacts", 1)[1]
        for folder in (
            ".sharpen/data/",
            ".claude/sdlc/",
            ".claude/grumpy/",
            ".claude/worktrees/",
        ):
            self.assertIn(folder, section)

    def test_this_repo_ignores_every_artifact_folder(self):
        if git(REPO_ROOT, "rev-parse", "--git-dir", check=False).returncode != 0:
            self.skipTest("not running from a git checkout")
        for folder in ARTIFACT_DIRS:
            self.assertTrue(ignored(REPO_ROOT, folder + "probe"), folder)
        self.assertFalse(ignored(REPO_ROOT, ".sharpen/simplify.json"))


if __name__ == "__main__":
    unittest.main()
