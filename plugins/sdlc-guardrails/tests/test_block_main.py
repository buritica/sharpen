#!/usr/bin/env python3
"""
Tests for the block-main-commits hook + guardrails config.

Pure stdlib (unittest) so it runs anywhere python3 exists -- no pip install.
Run: python3 plugins/sdlc-guardrails/tests/test_block_main.py

Each test builds a throwaway git repo (default branch `main`), drives the
real hook script with a synthetic Claude Code hook payload on stdin, and
asserts the exit code (0 = allow, 2 = deny). Commits/pushes are targeted at
the throwaway repo via `git -C <repo>` so results never depend on the branch
the test runner itself happens to be on.

Regression guard for the #14 NameError: committing on a protected main must
DENY with guidance (exit 2), not crash (exit 1).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HOOKS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(HOOKS_DIR, "hooks", "_block-main-commits.py")
CONFIG_CLI = os.path.join(HOOKS_DIR, "hooks", "_guardrails_config.py")

# Build the git token without the literal so running this file through a
# Claude Code session doesn't trip the live hook on our own source text.
_GIT = "g" + "it"
_MAIN = "ma" + "in"
_MASTER = "mas" + "ter"


def _git(repo, *args):
    subprocess.run(
        [_GIT, "-C", repo, *args],
        capture_output=True,
        check=True,
    )


class GuardrailsHookTest(unittest.TestCase):
    def setUp(self):
        self.cfgdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfgdir, ignore_errors=True)
        # GIT_CONFIG_GLOBAL=/dev/null so a developer's global commit.gpgsign
        # or core.hooksPath can't make setUp's `check=True` commit explode and
        # ERROR every test in the file.
        self.env = dict(
            os.environ, CLAUDE_CONFIG_DIR=self.cfgdir, GIT_CONFIG_GLOBAL=os.devnull
        )
        # Throwaway repo whose checked-out branch is `main`.
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        _git(self.repo, "init", "-q", "-b", _MAIN)
        _git(self.repo, "config", "user.email", "t@t")
        _git(self.repo, "config", "user.name", "t")
        # An initial commit so HEAD actually resolves to `main` (an unborn
        # branch reports no abbrev-ref, which the hook correctly treats as
        # "not on main" and allows).
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "init")

    def cmd(self, *parts):
        """A git command string targeting the throwaway repo via -C."""
        return " ".join([_GIT, "-C", self.repo, *parts])

    def run_hook(self, command, tool_name="Bash", extra_env=None):
        payload = json.dumps(
            {"tool_name": tool_name, "tool_input": {"command": command}}
        )
        env = dict(self.env, **(extra_env or {}))
        # cwd=self.repo matters for the allow-direction mirrors. A command with
        # no `git -C` and no `cd` makes resolve_target fall back to the hook's
        # cwd; if that were the test runner's directory (unprotected), main()
        # would exit 0 before reaching any detection logic and the test would
        # pass no matter how broken the guard was.
        proc = subprocess.run(
            [sys.executable, HOOK],
            input=payload.encode(),
            capture_output=True,
            env=env,
            cwd=self.repo,
        )
        reason = ""
        if proc.stderr:
            try:
                reason = json.loads(proc.stderr.decode())["reason"]
            except (ValueError, KeyError):
                reason = proc.stderr.decode()
        return proc.returncode, reason

    def protect(self):
        subprocess.run(
            [sys.executable, CONFIG_CLI, "protect", self.repo],
            capture_output=True,
            env=self.env,
            check=True,
        )

    # --- opt-in: silent until protected -------------------------------------

    def test_commit_on_main_unprotected_allows(self):
        code, _ = self.run_hook(self.cmd("commit", "-m", "wip"))
        self.assertEqual(code, 0)

    def test_push_main_unprotected_allows(self):
        code, _ = self.run_hook(self.cmd("push", "origin", _MAIN))
        self.assertEqual(code, 0)

    # --- protected: deny with actionable guidance ---------------------------

    def test_commit_on_main_protected_denies_with_guidance(self):
        self.protect()
        code, reason = self.run_hook(self.cmd("commit", "-m", "wip"))
        self.assertEqual(code, 2, "commit on protected main must deny, not crash")
        # Regression guard for #14: guidance, never a traceback.
        self.assertNotIn("Traceback", reason)
        self.assertNotIn("NameError", reason)
        self.assertIn("switch -c feat", reason)

    def test_push_main_protected_denies(self):
        self.protect()
        code, reason = self.run_hook(self.cmd("push", "origin", _MAIN))
        self.assertEqual(code, 2)
        self.assertIn("gh pr create", reason)

    def test_push_master_protected_denies(self):
        self.protect()
        code, _ = self.run_hook(self.cmd("push", "origin", _MASTER))
        self.assertEqual(code, 2)

    def test_push_head_colon_main_protected_denies(self):
        self.protect()
        code, _ = self.run_hook(self.cmd("push", "origin", "HEAD:" + _MAIN))
        self.assertEqual(code, 2)

    # --- protected but legitimate: must allow -------------------------------

    def test_push_feature_branch_with_main_in_name_allows(self):
        self.protect()
        code, _ = self.run_hook(
            self.cmd("push", "origin", "feature/" + _MAIN + "-rework")
        )
        self.assertEqual(code, 0, "feature/main-rework is not a push to main")

    def test_non_git_command_allows(self):
        self.protect()
        code, _ = self.run_hook("ls -la && echo done")
        self.assertEqual(code, 0)

    def test_git_status_allows(self):
        self.protect()
        code, _ = self.run_hook(self.cmd("status"))
        self.assertEqual(code, 0)

    def test_non_bash_tool_allows(self):
        self.protect()
        code, _ = self.run_hook(self.cmd("commit", "-m", "wip"), tool_name="Edit")
        self.assertEqual(code, 0)

    def test_commit_on_feature_branch_protected_allows(self):
        self.protect()
        _git(self.repo, "checkout", "-q", "-b", "feat/x")
        code, _ = self.run_hook(self.cmd("commit", "-m", "wip"))
        self.assertEqual(
            code, 0, "protection only blocks main/master, not feature branches"
        )

    # --- escape hatch -------------------------------------------------------

    def test_env_bypass_allows_commit_on_protected_main(self):
        self.protect()
        code, _ = self.run_hook(
            self.cmd("commit", "-m", "wip"), extra_env={"SDLC_ALLOW_MAIN": "1"}
        )
        self.assertEqual(code, 0)

    def test_env_bypass_allows_push_to_protected_main(self):
        self.protect()
        code, _ = self.run_hook(
            self.cmd("push", "origin", _MAIN), extra_env={"SDLC_ALLOW_MAIN": "1"}
        )
        self.assertEqual(code, 0)

    # --- command-boundary discipline ---------------------------------------
    # These are the regressions that motivated this fix: the old regex
    # matched `git commit` / `git push` anywhere in the command string,
    # including inside heredoc bodies and quoted string arguments — so a
    # `gh issue create` whose PR body prose mentioned "git commit" was
    # denied when cwd was on protected main.

    def test_heredoc_body_mentioning_git_commit_allows(self):
        self.protect()
        # A gh invocation whose body-file heredoc happens to contain the
        # text "git commit" is not itself a git commit.
        cmd = (
            "gh issue create --title x --body-file - <<'EOF'\n"
            "This issue is about how git commit interacts with the hook.\n"
            "EOF"
        )
        code, _ = self.run_hook(cmd)
        self.assertEqual(code, 0, "heredoc body must not trigger the hook")

    def test_echoed_string_mentioning_git_push_allows(self):
        self.protect()
        code, _ = self.run_hook('echo "hint: git push origin ' + _MAIN + '"')
        self.assertEqual(code, 0, "quoted string must not trigger the hook")

    def test_chained_real_git_commit_still_denies(self):
        # cd <repo> && git commit — the second segment IS a real invocation.
        self.protect()
        code, reason = self.run_hook(
            "cd " + self.repo + " && " + _GIT + " commit -m wip"
        )
        self.assertEqual(code, 2)
        self.assertIn(_MAIN, reason)

    # --- inline env-var escape hatch ---------------------------------------
    # `SDLC_ALLOW_MAIN=1 git commit` propagates the var only to git's own
    # subprocess, not to the PreToolUse hook (which runs before). The hook
    # must therefore parse the prefix out of the command string itself.

    def test_inline_bypass_allows_commit_on_protected_main(self):
        self.protect()
        code, _ = self.run_hook("SDLC_ALLOW_MAIN=1 " + self.cmd("commit", "-m", "wip"))
        self.assertEqual(code, 0, "inline env-var prefix must bypass the hook")

    def test_inline_bypass_case_insensitive(self):
        self.protect()
        code, _ = self.run_hook(
            "SDLC_ALLOW_MAIN=TRUE " + self.cmd("commit", "-m", "wip")
        )
        self.assertEqual(code, 0)

    # --- shell wrappers must not launder a commit --------------------------
    # Segment-splitting alone only sees git at the textual start of a
    # [;|&] segment, so every wrapper below used to sail through on a
    # protected main. Each must deny; the mirror cases further down prove
    # the fix did not buy that by blocking prose.

    def test_bash_dash_c_commit_denies(self):
        self.protect()
        code, reason = self.run_hook('bash -c "%s"' % self.cmd("commit", "-m", "x"))
        self.assertEqual(code, 2, "bash -c must not launder a commit on main")
        self.assertIn("switch -c feat", reason)

    def test_sh_dash_c_commit_denies(self):
        self.protect()
        code, _ = self.run_hook("sh -c '%s'" % self.cmd("commit", "-m", "x"))
        self.assertEqual(code, 2)

    def test_bash_combined_flags_commit_denies(self):
        self.protect()
        code, _ = self.run_hook("bash -lc '%s'" % self.cmd("commit", "-m", "x"))
        self.assertEqual(code, 2, "-lc bundles the -c flag")

    def test_eval_commit_denies(self):
        self.protect()
        code, _ = self.run_hook("eval '%s'" % self.cmd("commit", "-m", "x"))
        self.assertEqual(code, 2)

    def test_subshell_commit_denies(self):
        self.protect()
        code, _ = self.run_hook("(%s)" % self.cmd("commit", "-m", "x"))
        self.assertEqual(code, 2, "a subshell's ( is a real command boundary")

    def test_env_prefix_commit_denies(self):
        self.protect()
        code, _ = self.run_hook("env " + self.cmd("commit", "-m", "x"))
        self.assertEqual(code, 2)

    def test_env_with_flag_commit_denies(self):
        self.protect()
        code, _ = self.run_hook("env -i " + self.cmd("commit", "-m", "x"))
        self.assertEqual(code, 2)

    def test_env_with_value_taking_flag_commit_denies(self):
        self.protect()
        # `-u NAME` consumes NAME; a peeler that only skips dash-tokens stops
        # on NAME and lets the commit through one flag later.
        code, _ = self.run_hook("env -u FOO " + self.cmd("commit", "-m", "x"))
        self.assertEqual(code, 2)

    def test_nested_wrappers_commit_denies(self):
        self.protect()
        code, _ = self.run_hook('bash -c "env %s"' % self.cmd("commit", "-m", "x"))
        self.assertEqual(code, 2, "unwrapping must recurse")

    def test_wrapped_push_to_main_denies(self):
        self.protect()
        code, reason = self.run_hook('bash -c "%s"' % self.cmd("push", "origin", _MAIN))
        self.assertEqual(code, 2)
        self.assertIn("gh pr create", reason)

    # --- the mirror: wrappers must not create false positives --------------
    # A false positive here blocks legitimate work on every commit, so each
    # deny above is paired with the prose or non-git form it must not catch.

    def test_quoted_mention_of_subshell_commit_allows(self):
        self.protect()
        code, _ = self.run_hook('echo "run (%s) later"' % self.cmd("commit", "-m", "x"))
        self.assertEqual(code, 0, "a quoted mention is argument data, not a command")

    def test_heredoc_body_with_wrapped_commit_allows(self):
        self.protect()
        cmd = (
            "gh issue create --title x --body-file - <<'EOF'\n"
            "Reproduce with bash -c " + _GIT + " commit and watch it fail.\n"
            "EOF"
        )
        code, _ = self.run_hook(cmd)
        self.assertEqual(code, 0, "heredoc prose must not trigger the hook")

    def test_bash_dash_c_non_git_allows(self):
        self.protect()
        code, _ = self.run_hook('bash -c "echo ' + _GIT + ' commit"')
        self.assertEqual(code, 0, "the script echoes the words, it does not run them")

    def test_subshell_git_status_allows(self):
        self.protect()
        code, _ = self.run_hook("(%s)" % self.cmd("status"))
        self.assertEqual(code, 0, "only commit/push are gated")

    def test_wrapped_commit_on_feature_branch_allows(self):
        self.protect()
        _git(self.repo, "checkout", "-q", "-b", "feat/x")
        code, _ = self.run_hook('bash -c "%s"' % self.cmd("commit", "-m", "x"))
        self.assertEqual(code, 0)

    def test_unlexable_command_still_denies_via_regex_floor(self):
        self.protect()
        # An apostrophe makes the whole string unlexable. The regex floor must
        # still catch a bare commit. Asserting `code in (0, 2)` here would be
        # an assertion that cannot fail — it green-lit a real regression.
        code, reason = self.run_hook(self.cmd("commit", "-m", "x") + " && echo can't")
        self.assertEqual(code, 2, "regex floor must catch commands that don't lex")
        self.assertNotIn("Traceback", reason)

    def test_apostrophe_elsewhere_does_not_blind_wrapper_detection(self):
        self.protect()
        # The regex floor does NOT see through wrappers, so if one stray
        # apostrophe blinded the argv pass for the whole command, this would
        # allow. Decomposition-on-lex-failure is what saves it.
        code, _ = self.run_hook(
            'bash -c "%s" ; echo that\'s all' % self.cmd("commit", "-m", "x")
        )
        self.assertEqual(code, 2, "one apostrophe must not disable the argv pass")

    def test_deeply_nested_wrappers_fail_closed(self):
        self.protect()
        cmd = self.cmd("commit", "-m", "x")
        for _ in range(9):
            cmd = "bash -c " + json.dumps(cmd)
        code, _ = self.run_hook(cmd)
        self.assertEqual(code, 2, "past the unwrap cap the hook must deny, not allow")

    # --- shell syntax that precedes a command without being one ------------

    def test_brace_group_commit_denies(self):
        self.protect()
        code, _ = self.run_hook("{ %s ; }" % self.cmd("commit", "-m", "x"))
        self.assertEqual(code, 2)

    def test_then_keyword_commit_denies(self):
        self.protect()
        code, _ = self.run_hook("if true; then %s; fi" % self.cmd("commit", "-m", "x"))
        self.assertEqual(code, 2)

    def test_bang_prefix_commit_denies(self):
        self.protect()
        code, _ = self.run_hook("! " + self.cmd("commit", "-m", "x"))
        self.assertEqual(code, 2)

    def test_bash_flag_bundle_with_trailing_letters_denies(self):
        self.protect()
        # `-cx` still takes the next arg as the script; matching only `*c`
        # meant one letter of flag ordering defeated the whole pass.
        code, _ = self.run_hook("bash -cx '%s'" % self.cmd("commit", "-m", "x"))
        self.assertEqual(code, 2)

    def test_env_split_string_denies(self):
        self.protect()
        # `-S`'s argument IS a command. Listing it as a value-consuming option
        # made the peeler throw the command away and allow the commit.
        code, _ = self.run_hook('env -S "%s"' % self.cmd("commit", "-m", "x"))
        self.assertEqual(code, 2)

    def test_env_split_string_equals_form_denies(self):
        self.protect()
        code, _ = self.run_hook(
            "env --split-string='%s'" % self.cmd("commit", "-m", "x")
        )
        self.assertEqual(code, 2)

    def test_sudo_commit_denies(self):
        self.protect()
        code, _ = self.run_hook("sudo " + self.cmd("commit", "-m", "x"))
        self.assertEqual(code, 2)

    def test_timeout_with_duration_operand_denies(self):
        self.protect()
        code, _ = self.run_hook("timeout 60 " + self.cmd("commit", "-m", "x"))
        self.assertEqual(code, 2)

    def test_xargs_commit_denies(self):
        self.protect()
        code, _ = self.run_hook("echo | xargs -I{} " + self.cmd("commit", "-m", "x"))
        self.assertEqual(code, 2)

    def test_git_exec_path_before_subcommand_denies(self):
        self.protect()
        # `--exec-path`'s value is optional, so treating it as value-consuming
        # swallowed `commit` and returned no subcommand at all.
        code, _ = self.run_hook(
            " ".join([_GIT, "--exec-path", "-C", self.repo, "commit", "-m", "x"])
        )
        self.assertEqual(code, 2)

    # --- heredoc bodies are argument data, never commands -------------------
    # The root fix. Quoting alone does NOT protect these: a heredoc body is
    # unquoted at the lexer level, so before stripping, any `(`, `<` or `)`
    # in prose started a segment whose head was `git`.

    def test_heredoc_body_with_parenthesized_commit_allows(self):
        self.protect()
        cmd = (
            "gh pr create --body-file - <<'EOF'\n"
            "The fix lands when you (" + self.cmd("commit", "-m", "x") + ") later.\n"
            "EOF"
        )
        code, _ = self.run_hook(cmd)
        self.assertEqual(code, 0, "parens in PR-body prose must not deny")

    def test_heredoc_body_with_angle_brackets_allows(self):
        self.protect()
        cmd = (
            "gh pr create --body-file - <<'EOF'\n"
            "Use <" + self.cmd("commit", "-m", "x") + "> to finish.\n"
            "EOF"
        )
        code, _ = self.run_hook(cmd)
        self.assertEqual(code, 0)

    def test_heredoc_body_markdown_list_allows(self):
        self.protect()
        cmd = (
            "gh pr create --body-file - <<'EOF'\n"
            "1) " + self.cmd("commit", "-m", "x") + "\n"
            "EOF"
        )
        code, _ = self.run_hook(cmd)
        self.assertEqual(code, 0, "a markdown list marker is not a subshell")

    def test_real_commit_after_heredoc_is_a_known_gap(self):
        self.protect()
        # KNOWN GAP, pinned deliberately so the boundary lives in the suite
        # and not only in a code comment. Newlines are not token separators,
        # so a command on a later line never reaches head position. Closing
        # this would mean splitting on newlines, which would re-read the
        # continuation lines of quoted multi-line arguments (`gh issue create
        # --body "...\ngit commit...\n..."`) as commands — the exact prose
        # false positive this design exists to prevent.
        cmd = ("gh issue create --body-file - <<'EOF'\nsome prose\nEOF\n") + self.cmd(
            "commit", "-m", "x"
        )
        code, _ = self.run_hook(cmd)
        self.assertEqual(code, 0, "documented gap: newline-separated commands")

    def test_commit_chained_after_heredoc_with_separator_denies(self):
        self.protect()
        # The supported form: an explicit `&&` puts the commit at a segment
        # boundary, and heredoc stripping keeps the prose out of the way.
        cmd = (
            "gh issue create --body-file - <<'EOF'\n(some prose)\nEOF\n&& "
        ) + self.cmd("commit", "-m", "x")
        code, _ = self.run_hook(cmd)
        self.assertEqual(code, 2)

    # --- the escape hatch must be an assignment, not a mention -------------

    def test_commit_message_mentioning_hatch_still_denies(self):
        self.protect()
        # A commit message *about* the escape hatch is not an invocation of
        # it. Textual matching let the message disarm the guard.
        code, _ = self.run_hook(
            self.cmd("commit", "-m", '"SDLC_ALLOW_MAIN=1 is the escape hatch"')
        )
        self.assertEqual(code, 2, "a mention in a commit message must not bypass")

    def test_conventional_commit_scope_mentioning_hatch_denies(self):
        self.protect()
        code, _ = self.run_hook(self.cmd("commit", "-m", '"fix(SDLC_ALLOW_MAIN=1): x"'))
        self.assertEqual(code, 2)

    # --- malformed payloads must not crash the guard open ------------------

    def test_non_string_command_does_not_traceback(self):
        self.protect()
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": 123}})
        proc = subprocess.run(
            [sys.executable, HOOK],
            input=payload.encode(),
            capture_output=True,
            env=self.env,
            cwd=self.repo,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr.decode())

    def test_null_tool_input_does_not_traceback(self):
        self.protect()
        payload = json.dumps({"tool_name": "Bash", "tool_input": None})
        proc = subprocess.run(
            [sys.executable, HOOK],
            input=payload.encode(),
            capture_output=True,
            env=self.env,
            cwd=self.repo,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr.decode())

    # --- allow-direction mirrors for wrappers that only had deny tests -----

    def test_eval_non_git_allows(self):
        self.protect()
        code, _ = self.run_hook("eval 'echo hello'")
        self.assertEqual(code, 0)

    def test_env_assignment_non_git_allows(self):
        self.protect()
        code, _ = self.run_hook("env FOO=1 ls")
        self.assertEqual(code, 0)

    def test_wrapped_push_to_feature_branch_allows(self):
        self.protect()
        code, _ = self.run_hook(
            'bash -c "%s"' % self.cmd("push", "origin", "feature/" + _MAIN + "-rework")
        )
        self.assertEqual(code, 0)

    def test_bypass_inside_wrapper_allows(self):
        self.protect()
        # Now that the wrapper is seen through, the escape hatch must be
        # visible through it too — otherwise this is a deny with no way out.
        code, _ = self.run_hook(
            'bash -c "SDLC_ALLOW_MAIN=1 %s"' % self.cmd("commit", "-m", "x")
        )
        self.assertEqual(code, 0, "escape hatch must survive wrapping")


if __name__ == "__main__":
    unittest.main(verbosity=2)
