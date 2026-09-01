#!/usr/bin/env python3
"""
Tests for scripts/frontmatter.py against every real frontmatter shape found
in plugins/*/commands/*.md (enumerated by hand before writing this parser —
not a general YAML parser, just what this repo's command files actually use).

Run: python3 scripts/tests/test_frontmatter.py
"""

import glob
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import frontmatter as fm  # noqa: E402


class SplitTest(unittest.TestCase):
    def test_splits_frontmatter_from_body(self):
        text = "---\nkey: value\n---\n# Body\ntext\n"
        front, body = fm.split(text)
        self.assertEqual(front, "key: value")
        self.assertEqual(body, "# Body\ntext\n")

    def test_missing_opening_delimiter_is_an_error(self):
        with self.assertRaises(ValueError):
            fm.split("# Body\nno frontmatter here\n")

    def test_unclosed_frontmatter_is_an_error(self):
        with self.assertRaises(ValueError):
            fm.split("---\nkey: value\nno closing delimiter\n")

    def test_a_lone_triple_dash_in_the_body_does_not_confuse_the_split(self):
        # The body may contain a markdown horizontal rule (also `---`) after
        # the frontmatter closes — only the FIRST closing `---` ends it.
        text = "---\nkey: value\n---\n# Body\n\n---\n\nmore text\n"
        front, body = fm.split(text)
        self.assertEqual(front, "key: value")
        self.assertEqual(body, "# Body\n\n---\n\nmore text\n")


class ParseScalarTest(unittest.TestCase):
    def test_quoted_single_line_scalar(self):
        # plugins/sdlc/commands/audit.md's shape.
        result = fm.parse('description: "Read-only drift report."')
        self.assertEqual(result["description"], "Read-only drift report.")

    def test_unquoted_single_line_scalar(self):
        # plugins/sdlc-guardrails/commands/guard.md's `name:`/`model:` shape.
        result = fm.parse("name: guard\nmodel: haiku")
        self.assertEqual(result["name"], "guard")
        self.assertEqual(result["model"], "haiku")

    def test_folded_multiline_scalar_joins_with_spaces(self):
        # plugins/grumpy/commands/review.md's shape.
        text = (
            "description:\n"
            "  Comprehensive code review from a grumpy principal engineer who's "
            "seen too many\n"
            "  production incidents\n"
            'argument-hint: "[--level grumpy|grumpier|linus]"'
        )
        result = fm.parse(text)
        self.assertEqual(
            result["description"],
            "Comprehensive code review from a grumpy principal engineer "
            "who's seen too many production incidents",
        )
        self.assertEqual(result["argument-hint"], "[--level grumpy|grumpier|linus]")

    def test_empty_value_with_no_continuation_is_empty_string(self):
        result = fm.parse("key:\nother: value")
        self.assertEqual(result["key"], "")
        self.assertEqual(result["other"], "value")


class ParseArrayTest(unittest.TestCase):
    def test_inline_array(self):
        result = fm.parse('allowed-tools: ["Bash", "Glob", "Grep"]')
        self.assertEqual(result["allowed-tools"], ["Bash", "Glob", "Grep"])

    def test_multiline_array_with_trailing_comma(self):
        # plugins/grumpy/commands/cleanup.md and fix.md's shape.
        text = (
            "allowed-tools:\n"
            "  [\n"
            '    "Bash",\n'
            '    "Edit",\n'
            '    "AskUserQuestion",\n'
            "  ]"
        )
        result = fm.parse(text)
        self.assertEqual(result["allowed-tools"], ["Bash", "Edit", "AskUserQuestion"])

    def test_bare_matcher_string_is_not_mistaken_for_an_array(self):
        # plugins/sdlc-guardrails/commands/guard.md's shape: not an array,
        # not quoted — a bare Claude Code tool-matcher pattern.
        result = fm.parse("allowed-tools: Bash(python3:*)")
        self.assertEqual(result["allowed-tools"], "Bash(python3:*)")


class ParseFullFileTest(unittest.TestCase):
    def test_review_md_shape_end_to_end(self):
        text = (
            "---\n"
            "description:\n"
            "  Comprehensive code review from a grumpy principal engineer who's "
            "seen too many\n"
            "  production incidents\n"
            'argument-hint: "[--level grumpy|grumpier|linus] [review-aspects] '
            '[--worktree <path>]"\n'
            'allowed-tools: ["Bash", "Glob", "Grep", "Read", "Write", '
            '"TaskCreate", "TaskUpdate", "Agent"]\n'
            "---\n"
            "\n"
            "# Grumpy Review\n"
            "\n"
            "Body text here.\n"
        )
        front_text, body = fm.split(text)
        data = fm.parse(front_text)
        self.assertEqual(
            data["description"],
            "Comprehensive code review from a grumpy principal engineer "
            "who's seen too many production incidents",
        )
        self.assertEqual(
            data["argument-hint"],
            "[--level grumpy|grumpier|linus] [review-aspects] [--worktree <path>]",
        )
        self.assertEqual(
            data["allowed-tools"],
            ["Bash", "Glob", "Grep", "Read", "Write", "TaskCreate", "TaskUpdate", "Agent"],
        )
        self.assertTrue(body.startswith("\n# Grumpy Review\n"))

    def test_unrecognized_line_shape_raises(self):
        with self.assertRaises(ValueError):
            fm.parse("not a key-value line at all")


class RealCommandFilesTest(unittest.TestCase):
    """A direct regression guard, independent of scripts/check-marketplace.py
    (which also exercises this indirectly via check_skills on every CI run):
    every commands/*.md file that actually exists in this repo must parse
    and yield a usable description — a hand-built literal test case proves
    the parser handles a shape in isolation, not that no real file has
    drifted to something it doesn't."""

    def test_every_real_command_file_parses_with_a_description(self):
        repo_root = os.path.abspath(os.path.join(HERE, "..", ".."))
        paths = glob.glob(os.path.join(repo_root, "plugins", "*", "commands", "*.md"))
        self.assertGreater(len(paths), 0, "no command files found — check the glob/path")
        for path in paths:
            with self.subTest(path=path):
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                front_text, _ = fm.split(text)
                data = fm.parse(front_text)
                self.assertTrue(
                    data.get("description"), f"{path} has no usable description"
                )


if __name__ == "__main__":
    unittest.main()
