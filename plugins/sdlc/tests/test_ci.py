#!/usr/bin/env python3
"""Tests for CI workflow structure — verifies ci-pass aggregator is present."""

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
        # Deny-by-default: the condition must use != (not ==), so failure hits exit 1.
        self.assertIn(
            "!=",
            self.block,
            "ci-pass script must use != (deny-by-default) to catch failures",
        )

    def test_ci_pass_treats_skipped_as_passing(self):
        self.assertIn(
            "skipped", self.block, "ci-pass script must treat 'skipped' as passing"
        )
        self.assertIn(
            "success", self.block, "ci-pass script must treat 'success' as passing"
        )

    def test_ci_pass_needs_validation_jobs(self):
        for job in ("lint", "test", "marketplace", "gates"):
            pattern = rf"needs:.*?{re.escape(job)}"
            self.assertIsNotNone(
                re.search(pattern, self.block, re.DOTALL), f"ci-pass must need '{job}'"
            )


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
