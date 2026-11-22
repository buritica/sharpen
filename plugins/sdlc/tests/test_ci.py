#!/usr/bin/env python3
"""Tests for this repo's own CI workflow: the ci-pass aggregator, triggers
and the Python floor."""

import json
import os
import re
import subprocess
import sys
import textwrap
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
        for job in ("lint", "test", "marketplace", "pi"):
            pattern = rf"needs:.*?{re.escape(job)}"
            self.assertIsNotNone(
                re.search(pattern, self.block, re.DOTALL), f"ci-pass must need '{job}'"
            )

    def test_ci_pass_failure_message_names_jobs_from_needs(self):
        # A hardcoded job list in the message went stale once already.
        self.assertIn("toJSON(needs)", self.block)

    def _ci_pass_script(self):
        """The ci-pass `run: |` body, dedented the way Actions runs it."""
        lines = self.block.splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "run: |")
        indent = len(lines[start]) - len(lines[start].lstrip())
        body = []
        for ln in lines[start + 1 :]:
            if ln.strip() and len(ln) - len(ln.lstrip()) <= indent:
                break
            body.append(ln)
        return textwrap.dedent("\n".join(body))

    def _run_ci_pass(self, results):
        needs = {job: {"result": r, "outputs": {}} for job, r in results.items()}
        env = dict(os.environ, NEEDS=json.dumps(needs))
        return subprocess.run(
            ["bash", "-c", self._ci_pass_script()],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_ci_pass_script_passes_only_when_every_job_succeeded(self):
        # Runs the real script, so an inverted grep or a broken parse fails
        # here instead of greening (or blocking) every PR.
        jobs = ("lint", "test", "marketplace", "pi")
        ok = self._run_ci_pass({j: "success" for j in jobs})
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        for bad in ("failure", "cancelled", "skipped"):
            with self.subTest(result=bad):
                results = {j: "success" for j in jobs}
                results["pi"] = bad
                r = self._run_ci_pass(results)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn("pi=" + bad, r.stdout)

    def test_ci_pass_script_fails_on_empty_needs(self):
        r = self._run_ci_pass({})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)


class TestCiTriggers(unittest.TestCase):
    def setUp(self):
        self.content = _read_ci()

    def test_no_path_filter(self):
        # A path filter that misses one file the checks depend on yields a
        # green ci-pass that validated nothing.
        self.assertNotIn("paths-filter", self.content)
        self.assertNotIn("paths:", self.content)
        self.assertNotIn("paths-ignore:", self.content)

    def test_runs_on_main_weekly_and_by_hand(self):
        self.assertRegex(self.content, r"push:\s*\n\s*branches:\s*\[main\]")
        self.assertIn("schedule:", self.content)
        self.assertIn("workflow_dispatch:", self.content)

    def test_tests_run_on_the_python_floor(self):
        self.assertRegex(self.content, r'python:\s*\[[^\]]*"3\.9"')

    def test_no_gate_evidence_job(self):
        # sharpen is pre-PR and local: CI checks the code, never gate
        # evidence. Gate state is gitignored, so a CI job reading it could
        # only ever pass vacuously (the retired ci-validate.py did).
        self.assertNotIn("ci-validate", self.content)
        self.assertNotRegex(self.content, r"(?m)^\s{2}gates:")


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
