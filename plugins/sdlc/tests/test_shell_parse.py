#!/usr/bin/env python3
"""
Stdlib tests for shell_parse — the argv-shaped command parser the SDLC hooks
use instead of regexes over the raw command string.

Two directions matter equally: a command that really runs must be SEEN (else
the gate is bypassable by wrapping it), and a command that is merely quoted
must NOT be seen (else ordinary work gets blocked).

Run: python3 plugins/sdlc/tests/test_shell_parse.py
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)

import shell_parse as sp  # noqa: E402


class InvokesPositiveTest(unittest.TestCase):
    """Forms that DO invoke the command. Each miss here is a free bypass."""

    CASES = [
        ("gh pr create --fill", "gh pr create"),
        ("gh   pr    create", "gh pr create"),  # extra whitespace
        ("cd /tmp && gh pr create", "gh pr create"),
        ("ls -la; gh pr create", "gh pr create"),
        ("ls\ngh pr create", "gh pr create"),  # newline-separated
        ("FOO=1 BAR=2 gh pr create", "gh pr create"),
        ("(gh pr create --fill)", "gh pr create"),  # subshell
        ("{ gh pr create; }", "gh pr create"),  # group
        # shlex groups runs of punctuation, so the separator token here is
        # ";(" / "&&(" — a fixed separator list misses both.
        ("foo;(gh pr create)", "gh pr create"),
        ("foo &&(gh pr create)", "gh pr create"),
        # compound statements: the keyword would otherwise become the head
        ("if true; then gh pr create; fi", "gh pr create"),
        ("for i in 1; do gh pr create; done", "gh pr create"),
        ("while :; do git commit -m x; done", "git commit"),
        ('bash -c "gh pr create --fill"', "gh pr create"),
        ("sh -c 'gh pr create'", "gh pr create"),
        ('zsh -c "cd /tmp && gh pr create"', "gh pr create"),
        ('eval "gh pr create"', "gh pr create"),
        ('bash -c "eval \\"gh pr create\\""', "gh pr create"),  # nested
        ("nohup gh pr create &", "gh pr create"),
        ("timeout 60 gh pr create", "gh pr create"),  # duration isn't the cmd
        ("sudo gh pr create", "gh pr create"),
        ('bash -lc "gh pr create --fill"', "gh pr create"),  # clustered flags
        ('bash -ec "gh pr create"', "gh pr create"),
        ('bash -c"gh pr create"', "gh pr create"),  # no space after -c
        ("> /tmp/out gh pr create", "gh pr create"),  # leading redirect
        ("gh pr create > /tmp/out", "gh pr create"),
        ("env FOO=1 gh pr create", "gh pr create"),
        ("env -u HOME gh pr create", "gh pr create"),  # flag with a value
        ("env -S 'gh pr create'", "gh pr create"),  # value IS a command string
        ("nice -n 5 gh pr create", "gh pr create"),
        ("gh --repo o/r pr create", "gh pr create"),  # value-taking gh flag
        ("git commit -m x", "git commit"),
        ("git -C /tmp commit -m x", "git commit"),
        ("git -c user.name=x commit -m y", "git commit"),
        ("git --no-pager commit -m x", "git commit"),
        ('bash -c "git commit -m x"', "git commit"),
        ("git \\\n  commit -m x", "git commit"),  # line continuation
        ("python3 record-gate.py --record simplify", "record-gate.py"),
        ("/abs/path/to/record-gate.py --record simplify", "record-gate.py"),
        ("./record-gate.py --record simplify", "record-gate.py"),
        ('bash -c "record-gate.py --record simplify"', "record-gate.py"),
    ]

    def test_invokes(self):
        for command, name in self.CASES:
            with self.subTest(command=command):
                self.assertTrue(sp.invokes(command, name), command)


class InvokesNegativeTest(unittest.TestCase):
    """Forms that merely MENTION the command. Each false hit blocks real work."""

    CASES = [
        ('echo "gh pr create"', "gh pr create"),
        ("echo 'gh pr create'", "gh pr create"),
        ('git commit -m "prep for gh pr create"', "gh pr create"),
        ("gh pr list", "gh pr create"),
        ("gh pr view 12", "gh pr create"),
        ("git log --grep commit", "git commit"),  # 'commit' as a flag value
        ('git log --grep "commit"', "git commit"),
        ("git show --stat commit", "git commit"),
        ('echo "run record-gate.py --record simplify"', "record-gate.py"),
        ("grep -r record-gate.py .", "record-gate.py"),
    ]

    def test_does_not_invoke(self):
        for command, name in self.CASES:
            with self.subTest(command=command):
                self.assertFalse(sp.invokes(command, name), command)


class HeredocTest(unittest.TestCase):
    """Heredoc bodies are argument data, not commands (see the module
    docstring for why this differs from the sdlc-guardrails hook)."""

    def test_heredoc_body_is_not_a_command(self):
        cmd = (
            "gh pr create --body-file - <<'EOF'\n"
            "This PR stops you from running record-gate.py --record simplify\n"
            "and from `git commit` on main.\n"
            "EOF"
        )
        self.assertFalse(sp.invokes(cmd, "record-gate.py"))
        self.assertFalse(sp.invokes(cmd, "git commit"))
        # ...but the command that OPENED the heredoc is still seen.
        self.assertTrue(sp.invokes(cmd, "gh pr create"))

    def test_command_after_heredoc_still_seen(self):
        cmd = "cat <<EOF > /tmp/f\ngit commit\nEOF\ngh pr create --fill"
        self.assertTrue(sp.invokes(cmd, "gh pr create"))
        self.assertFalse(sp.invokes(cmd, "git commit"))

    def test_dash_heredoc_and_unquoted_delimiter(self):
        for op, delim in (("<<-", "EOF"), ("<<", "MSG")):
            cmd = f"cat {op} {delim}\ngh pr create\n{delim}\necho done"
            self.assertFalse(sp.invokes(cmd, "gh pr create"), cmd)

    def test_two_heredocs_on_one_line(self):
        cmd = "diff <(cat <<A\ngh pr create\nA\n) <(cat <<B\ngit commit\nB\n)"
        self.assertFalse(sp.invokes(cmd, "gh pr create"))
        self.assertFalse(sp.invokes(cmd, "git commit"))

    def test_quoted_newline_is_not_a_separator(self):
        # A newline INSIDE quotes is data too — one `echo`, not two commands.
        self.assertFalse(sp.invokes('echo "line one\ngh pr create"', "gh pr create"))

    def test_delimiter_with_punctuation_still_strips_the_body(self):
        # `EOF-1` and `EOF.txt` are legal delimiters. Failing to recognize one
        # leaks its body: the text inside would parse as real commands.
        for delim in ("EOF-1", "EOF.txt", '"END OF BODY"'):
            cmd = f"cat <<{delim}\ngh pr create\n{delim.strip(chr(34))}"
            self.assertFalse(sp.invokes(cmd, "gh pr create"), delim)

    def test_shift_operator_is_not_a_heredoc(self):
        # These all contain `<<` without opening a heredoc. Reading one as a
        # heredoc that never terminates would hide every following command.
        for prefix in (
            'echo "a << b"',  # inside quotes
            "cat <<< marker",  # here-string
            "echo $(( 1 << n ))",  # arithmetic shift
            'git commit -m "resolve <<<<<<< HEAD"',  # conflict marker
        ):
            cmd = prefix + "\ngh pr create --fill"
            self.assertTrue(sp.invokes(cmd, "gh pr create"), prefix)

    def test_unterminated_heredoc_consumes_nothing(self):
        # No terminator in the string: dropping to EOF would silently swallow
        # every command after it.
        cmd = "cat <<EOF\nsome text\ngh pr create --fill"
        self.assertTrue(sp.invokes(cmd, "gh pr create"))

    def test_comment_does_not_swallow_the_next_line(self):
        # shlex's default commenters eat through the newline, gluing the next
        # command onto the commented one and hiding it.
        cmd = "git status # check first\ngh pr create --fill"
        self.assertTrue(sp.invokes(cmd, "gh pr create"))
        self.assertFalse(sp.invokes("echo a#b && gh pr list", "gh pr create"))


class SegmentsTest(unittest.TestCase):
    def test_splits_on_operators(self):
        segs = sp.command_segments("ls -la && echo hi | wc -l; true")
        self.assertEqual(segs, [["ls", "-la"], ["echo", "hi"], ["wc", "-l"], ["true"]])

    def test_interpreter_and_env_prefixes_stripped(self):
        self.assertEqual(
            sp.command_segments("FOO=1 python3 -u tool.py --x"),
            [["tool.py", "--x"]],
        )

    def test_empty_command(self):
        self.assertEqual(sp.command_segments(""), [])
        self.assertEqual(sp.command_segments("   "), [])

    def test_recursion_is_bounded(self):
        # Past the cap the wrapper is reported instead of its payload — that's
        # the bound doing its job, and asserting it is what makes removing
        # _MAX_DEPTH a test failure rather than a silent behavior change.
        def wrap(inner, times):
            # Shell-valid nesting: escape the quotes rather than relying on
            # repr(), which produces Python escapes bash never sees.
            for _ in range(times):
                inner = (
                    'bash -c "' + inner.replace("\\", "\\\\").replace('"', '\\"') + '"'
                )
            return inner

        over = wrap("gh pr create", sp._MAX_DEPTH + 2)
        self.assertEqual(sp.command_segments(over)[0][0], "bash")
        self.assertFalse(sp.invokes(over, "gh pr create"))
        # ...and just inside the cap it still unwraps all the way down.
        self.assertTrue(
            sp.invokes(wrap("gh pr create", sp._MAX_DEPTH - 1), "gh pr create")
        )

    def test_unbalanced_quote_degrades_toward_matching(self):
        # Malformed input must not parse to "no commands here" — that reads as
        # 'allow' at every call site. Better to still see the invocation.
        self.assertTrue(sp.invokes('gh pr create --title "unclosed', "gh pr create"))

    def test_tokenizer_fallback_keeps_separators(self):
        # A trailing backslash inside an unclosed quote defeats all three shlex
        # passes and reaches the regex fallback. If that fallback split only on
        # whitespace, "cd /tmp;gh" would be one token and the command would
        # vanish — the fail-open promise broken in the worst direction.
        cmd = 'cd /tmp;gh pr create --title "b\\'
        self.assertTrue(sp.invokes(cmd, "gh pr create"))
        self.assertIn(["cd", "/tmp"], sp.command_segments(cmd))


class TableCoverageTest(unittest.TestCase):
    """Every entry in the lookup tables is a live bypass if it stops working,
    and a hand-written case per entry is how they drifted out of coverage in
    the first place. These iterate the tables themselves, so adding an entry
    without exercising it is not possible."""

    def test_every_git_value_flag_is_skipped(self):
        # A value-taking flag missing from the table eats the subcommand:
        # `git --git-dir /x commit` would stop being a commit.
        for flag in sp._VALUE_FLAGS["git"]:
            cmd = f"git {flag} value commit -m x"
            self.assertTrue(sp.invokes(cmd, "git commit"), cmd)
            self.assertTrue(sp.invokes(f"git {flag}=value commit -m x", "git commit"))

    def test_every_gh_value_flag_is_skipped(self):
        for flag in sp._VALUE_FLAGS["gh"]:
            self.assertTrue(
                sp.invokes(f"gh {flag} o/r pr create", "gh pr create"), flag
            )

    def test_every_shell_unwraps_dash_c(self):
        for shell in sp._SHELLS:
            self.assertTrue(
                sp.invokes(f'{shell} -c "gh pr create"', "gh pr create"), shell
            )

    def test_every_wrapper_is_transparent(self):
        for wrapper in sp._WRAPPERS:
            args = "5 " if wrapper in sp._WRAPPER_POSITIONALS else ""
            cmd = f"{wrapper} {args}gh pr create"
            self.assertTrue(sp.invokes(cmd, "gh pr create"), cmd)

    def test_every_wrapper_value_flag_is_skipped(self):
        for wrapper, flags in sp._WRAPPER_VALUE_FLAGS.items():
            for flag in flags:
                cmd = f"{wrapper} {flag} v gh pr create"
                self.assertTrue(sp.invokes(cmd, "gh pr create"), cmd)

    def test_every_interpreter_prefix_is_stripped(self):
        for interp in (
            "python",
            "python3",
            "python3.12",
            "pypy",
            "node",
            "bun",
            "deno",
            "ruby",
            "perl",
        ):
            self.assertTrue(sp._INTERPRETERS.match(interp), interp)
            cmd = f"{interp} record-gate.py --record simplify"
            self.assertTrue(sp.invokes(cmd, "record-gate.py"), cmd)

    def test_every_keyword_is_stripped(self):
        for kw in sp._KEYWORDS:
            self.assertTrue(sp.invokes(f"{kw} gh pr create", "gh pr create"), kw)


class ScalingTest(unittest.TestCase):
    """These hooks run on every Bash call under a 5–10s timeout, and a timed-out
    PreToolUse hook does not block. So a quadratic in here is not a slow parse,
    it's an ungated PR. Both loops below were quadratic once (a 109KB single
    line took 23.8s); the assertions are loose enough not to flake on a busy
    machine but tight enough that reintroducing the quadratic fails."""

    def _elapsed(self, cmd):
        start = time.monotonic()
        sp.invokes(cmd, "gh pr create")
        return time.monotonic() - start

    def test_many_redirect_operators_on_one_line_stay_linear(self):
        # Quote-state was rescanned from index 0 for every `<<` on the line.
        small = self._elapsed(
            'git commit -m "' + ("a << b " * 2000) + '"\ngh pr create'
        )
        large = self._elapsed(
            'git commit -m "' + ("a << b " * 8000) + '"\ngh pr create'
        )
        self.assertLess(large, 2.0, "109KB of `<<` should parse in well under a second")
        # 4x the input should cost far less than the ~16x a quadratic implies.
        self.assertLess(large, max(small, 0.01) * 10)

    def test_many_unterminated_heredocs_stay_linear(self):
        # Each opener without a terminator used to rescan every remaining line.
        cmd = "\n".join(f"cat <<EOF{i}" for i in range(6000)) + "\ngh pr create"
        self.assertTrue(sp.invokes(cmd, "gh pr create"))  # and still correct
        self.assertLess(self._elapsed(cmd), 2.0)

    def test_realistic_command_is_fast(self):
        # p99 of a real corpus is ~3KB; this is the size that actually matters.
        cmd = "gh pr create --body '" + ("word " * 600) + "'"
        self.assertLess(self._elapsed(cmd), 0.1)


class ResolveWorkdirTest(unittest.TestCase):
    """Which repo a hook inspects. Getting this wrong is a silent bypass: a
    cycle looked up in the wrong repo reads as 'no cycle' → allow. Returning
    None is safe (the caller falls back to its own cwd); returning a confident
    wrong answer is not — which is why real directories are used throughout."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.other = tempfile.mkdtemp()
        self.spaced = os.path.join(self.repo, "a b")
        os.makedirs(self.spaced)

    def tearDown(self):
        for d in (self.repo, self.other):
            shutil.rmtree(d, ignore_errors=True)

    def test_cd_prefix(self):
        self.assertEqual(
            sp.resolve_workdir(f"cd {self.repo} && gh pr create", "gh pr create"),
            self.repo,
        )

    def test_cd_inside_a_wrapper(self):
        # The form the argv parser newly detects — the workdir has to come
        # along with it, or detection fires against the wrong repo.
        self.assertEqual(
            sp.resolve_workdir(
                f'bash -c "cd {self.repo} && gh pr create"', "gh pr create"
            ),
            self.repo,
        )

    def test_cd_with_spaces_and_semicolon(self):
        for cmd in (
            f'cd "{self.spaced}" && gh pr create',
            f"cd {self.repo}; gh pr create",
        ):
            self.assertIsNotNone(sp.resolve_workdir(cmd, "gh pr create"), cmd)

    def test_dash_c_flag_wins_over_cd(self):
        self.assertEqual(
            sp.resolve_workdir(
                f"cd {self.other} && git -C {self.repo} commit", "git commit"
            ),
            self.repo,
        )

    def test_quoted_dash_c_is_not_a_workdir(self):
        # A `-C` inside someone else's argument is data. Following it would let
        # a PR body choose which repo's gate store the hook reads.
        self.assertIsNone(
            sp.resolve_workdir(
                f'gh pr create --body "run git -C {self.other} commit"',
                "gh pr create",
            )
        )

    def test_cd_after_the_command_is_ignored(self):
        self.assertIsNone(
            sp.resolve_workdir(f"gh pr create && cd {self.other}", "gh pr create")
        )

    def test_dash_c_after_the_subcommand_is_not_a_workdir(self):
        # `git commit -C HEAD` reuses that commit's message. Reading it as a
        # directory sends the hook looking for a repo at "HEAD", finds none,
        # and skips — no cycle stamped, gate never engages.
        self.assertIsNone(sp.resolve_workdir("git commit -C HEAD", "git commit"))
        self.assertEqual(
            sp.resolve_workdir(f"git -C {self.repo} commit -C HEAD", "git commit"),
            self.repo,
        )

    def test_unexpandable_words_resolve_to_nothing(self):
        # shlex does not expand these, so the raw word would be handed to
        # subprocess(cwd=...) — it fails, the store isn't found, and "no cycle"
        # silently allows the PR. None puts the caller back on its own cwd.
        for word in (
            "~/repo",
            "$REPO",
            "$(git rev-parse --show-toplevel)",
            "`pwd`",
            "-",
            '""',
            "/no/such/dir",
        ):
            cmd = f"cd {word} && gh pr create"
            self.assertIsNone(sp.resolve_workdir(cmd, "gh pr create"), word)

    def test_tilde_that_does_expand_is_honored(self):
        # ~ is expanded (not left raw), so a real home-relative dir resolves.
        self.assertEqual(
            sp.resolve_workdir("cd ~ && gh pr create", "gh pr create"),
            os.path.expanduser("~"),
        )

    def test_cd_confined_to_a_subshell_does_not_leak(self):
        # The shell runs gh in the ORIGINAL directory here. Following the
        # subshell's cd would point the hook at a real-but-wrong repo — the
        # one failure mode that can't be caught by checking the path exists.
        for cmd in (
            f"(cd {self.other} && echo x) && gh pr create",
            f"if true; then cd {self.other}; fi\ngh pr create",
        ):
            self.assertIsNone(sp.resolve_workdir(cmd, "gh pr create"), cmd)

    def test_cd_in_the_same_subshell_is_honored(self):
        self.assertEqual(
            sp.resolve_workdir(f"(cd {self.repo} && gh pr create)", "gh pr create"),
            self.repo,
        )

    def test_no_workdir(self):
        self.assertIsNone(sp.resolve_workdir("gh pr create", "gh pr create"))
        self.assertIsNone(sp.resolve_workdir("ls", "gh pr create"))


def _parse_only(command, name):
    """invokes() with the pre-filter bypassed — the oracle it must agree with."""
    return any(sp._argv_invokes(a, name.split()) for a in sp.command_segments(command))


class PreFilterTest(unittest.TestCase):
    """The pre-filter exists to skip work, so what matters is where it can
    change an answer. Agreement over the main corpus is nearly free (the filter
    barely engages there), so the cases below are chosen to make it engage."""

    def test_prefilter_agrees_with_full_parse(self):
        for command, name in InvokesPositiveTest.CASES + InvokesNegativeTest.CASES:
            self.assertEqual(
                sp.invokes(command, name), _parse_only(command, name), command
            )

    def test_filter_engages_and_still_agrees(self):
        # Inputs where the head word IS absent, so the filter short-circuits:
        # it must reach the same verdict the parser would.
        for command in ("ls -la", "cat foo.txt", "rg pattern src/", ""):
            self.assertFalse(sp._mentions(command, ["gh"]), command)
            self.assertEqual(
                sp.invokes(command, "gh pr create"),
                _parse_only(command, "gh pr create"),
                command,
            )

    def test_known_limitation_head_split_across_quotes(self):
        # `g"h" pr create` runs gh, and the tokenizer sees it — but the filter
        # doesn't, so invokes() says no. Pinned deliberately: the module claims
        # to be a lexer, not a security boundary, and this is the price of the
        # filter. If someone makes the filter smarter, this test should change
        # on purpose rather than quietly.
        adversarial = 'g"h" pr create'
        self.assertTrue(_parse_only(adversarial, "gh pr create"))
        self.assertFalse(sp.invokes(adversarial, "gh pr create"))


class FlagValueTest(unittest.TestCase):
    def test_space_and_equals_forms(self):
        self.assertEqual(sp.flag_value(["x", "--head", "feat/a"], "--head"), "feat/a")
        self.assertEqual(sp.flag_value(["x", "--head=feat/a"], "--head"), "feat/a")
        self.assertEqual(sp.flag_value(["x", "-H", "feat/a"], "--head", "-H"), "feat/a")
        self.assertIsNone(sp.flag_value(["x", "--head"], "--head"))  # no value
        self.assertIsNone(sp.flag_value(["x"], "--head"))

    def test_reads_flags_off_the_matched_segment_only(self):
        # The --head belongs to a different command; the pr-create segment has
        # none. Reading flags off the raw string would wrongly pick it up.
        argv = sp.matching_segments(
            "gh pr list --head feat/other && gh pr create --fill", "gh pr create"
        )[0]
        self.assertIsNone(sp.flag_value(argv, "--head", "-H"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
