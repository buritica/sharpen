#!/usr/bin/env python3
"""Tests for this repo's own CI workflow: the ci-pass aggregator, triggers
and the Python floor."""

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
CI_YML = os.path.join(ROOT, ".github", "workflows", "ci.yml")


def _read_ci() -> str:
    with open(CI_YML) as f:
        return f.read()


class TestCiPassJob(unittest.TestCase):
    """Grep-based structural tests for the ci-pass aggregator job."""

    def setUp(self):
        self.content = _read_ci()
        match = re.search(
            r"^\s{2}ci-pass:.*?(?=^\s{2}\w|\Z)", self.content, re.DOTALL | re.MULTILINE
        )
        self.assertIsNotNone(match, "ci-pass job block not found in ci.yml")
        self.block = match.group()

    def test_ci_pass_job_exists(self):
        self.assertIn("ci-pass:", self.content, "ci.yml must contain a ci-pass job")

    def test_ci_pass_if_always(self):
        self.assertIn("if: always()", self.block, "ci-pass must have if: always()")

    def test_ci_pass_blocks_on_failure(self):
        # The script must exit 1 when a job didn't pass — verify the deny branch.
        self.assertIn(
            "exit 1", self.block, "ci-pass script must exit 1 on non-passing jobs"
        )
        # Deny-by-default: every job that is not exactly `success` is a
        # failure, rather than listing the bad results and missing one.
        self.assertIn(
            "grep -v '=success$'",
            self.block,
            "ci-pass must fail on any result other than success",
        )

    def test_ci_pass_does_not_accept_skipped(self):
        # Nothing is path-gated, so a skipped job means something broke.
        self.assertNotIn(
            '"skipped"', self.block, "ci-pass must not treat 'skipped' as passing"
        )
        self.assertIn("success", self.block, "ci-pass must require 'success'")

    def test_ci_pass_needs_validation_jobs(self):
        for job in ("lint", "test", "marketplace", "pi", "gates"):
            pattern = rf"needs:.*?{re.escape(job)}"
            self.assertIsNotNone(
                re.search(pattern, self.block, re.DOTALL), f"ci-pass must need '{job}'"
            )

    def test_ci_pass_failure_message_names_jobs_from_needs(self):
        # A hardcoded job list in the message went stale once already.
        self.assertIn("toJSON(needs)", self.block)


class TestCiTriggers(unittest.TestCase):
    def setUp(self):
        self.content = _read_ci()

    def test_no_path_filter(self):
        # A path filter that misses one file the checks depend on yields a
        # green ci-pass that validated nothing.
        self.assertNotIn("paths-filter", self.content)
        self.assertNotIn("paths:", self.content)

    def test_runs_on_main_weekly_and_by_hand(self):
        self.assertRegex(self.content, r"push:\s*\n\s*branches:\s*\[main\]")
        self.assertIn("schedule:", self.content)
        self.assertIn("workflow_dispatch:", self.content)

    def test_tests_run_on_the_python_floor(self):
        self.assertRegex(self.content, r'python:\s*\[[^\]]*"3\.9"')


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
