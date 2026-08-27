#!/usr/bin/env python3
"""Tests for scripts/eval-grumpy-findings.py's scoring logic.

Deterministic and offline -- exercises the matcher against fixed
inputs, not a live grumpy/LLM run.
"""

import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODULE_PATH = os.path.join(ROOT, "scripts", "eval-grumpy-findings.py")

spec = importlib.util.spec_from_file_location("eval_grumpy_findings", MODULE_PATH)
eval_grumpy_findings = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eval_grumpy_findings)


GOLDEN = {
    "findings": [
        {
            "id": "swallowed-exception",
            "file": "src/billing/charge.py",
            "keywords": ["except", "pass", "swallow"],
        },
        {
            "id": "fake-test-assertion",
            "file": "src/billing/test_charge.py",
            "keywords": ["assert true", "tests nothing"],
        },
    ]
}


class TestMatches(unittest.TestCase):
    def test_matches_on_file_and_keyword(self):
        finding = GOLDEN["findings"][0]
        candidate = {
            "file": "src/billing/charge.py",
            "text": "bare except with pass swallows the gateway error",
        }
        self.assertTrue(eval_grumpy_findings.matches(finding, candidate))

    def test_no_match_wrong_file(self):
        finding = GOLDEN["findings"][0]
        candidate = {
            "file": "src/billing/other.py",
            "text": "bare except with pass swallows the gateway error",
        }
        self.assertFalse(eval_grumpy_findings.matches(finding, candidate))

    def test_no_match_missing_keyword(self):
        finding = GOLDEN["findings"][0]
        candidate = {
            "file": "src/billing/charge.py",
            "text": "the retry loop could be tighter",
        }
        self.assertFalse(eval_grumpy_findings.matches(finding, candidate))

    def test_matches_is_case_insensitive_both_sides(self):
        finding = {
            "file": "charge.py",
            "keywords": ["SWALLOW"],
        }
        candidate = {"file": "charge.py", "text": "this SWALLOWS the error"}
        self.assertTrue(eval_grumpy_findings.matches(finding, candidate))

    def test_no_match_on_missing_candidate_keys(self):
        finding = GOLDEN["findings"][0]
        self.assertFalse(eval_grumpy_findings.matches(finding, {}))


class TestParsePipeLine(unittest.TestCase):
    def test_parses_a_well_formed_finding_line(self):
        line = "CRIT|charge.py:18|swallows the error|fact|errors"
        result = eval_grumpy_findings.parse_pipe_line(line)
        self.assertEqual(
            result,
            {
                "severity": "CRIT",
                "file": "charge.py",
                "text": "swallows the error",
                "fact": "fact",
                "domain": "errors",
            },
        )

    def test_preserves_a_pipe_character_inside_the_text_field(self):
        line = (
            "WARN|charge.py:10|retries with a a|b regex, never bounded|judgment|errors"
        )
        result = eval_grumpy_findings.parse_pipe_line(line)
        self.assertEqual(result["text"], "retries with a a|b regex, never bounded")
        self.assertEqual(result["fact"], "judgment")
        self.assertEqual(result["domain"], "errors")

    def test_rejects_context_and_handled_lines(self):
        self.assertIsNone(
            eval_grumpy_findings.parse_pipe_line(
                "transport:Slack|API calls: 1|UX: fine|Logs: ok|Metrics: ok"
            )
        )
        self.assertIsNone(
            eval_grumpy_findings.parse_pipe_line(
                "HANDLED|cleanup|always releases locks"
            )
        )

    def test_rejects_blank_and_malformed_lines(self):
        self.assertIsNone(eval_grumpy_findings.parse_pipe_line(""))
        self.assertIsNone(eval_grumpy_findings.parse_pipe_line("   "))
        self.assertIsNone(eval_grumpy_findings.parse_pipe_line("CRIT|only two fields"))

    def test_parse_pipe_findings_skips_non_finding_lines(self):
        raw = "\n".join(
            [
                "transport:Slack|API calls: 1|UX: fine|Logs: ok|Metrics: ok",
                "CRIT|charge.py:18|swallows the error|fact|errors",
                "",
                "HANDLED|cleanup|always releases locks",
                "NOTE|charge.py:30|unnecessary factory|judgment|simplify",
            ]
        )
        findings, raw_line_count = eval_grumpy_findings.parse_pipe_findings(raw)
        self.assertEqual(len(findings), 2)
        self.assertEqual(findings[0]["severity"], "CRIT")
        self.assertEqual(findings[1]["severity"], "NOTE")
        self.assertEqual(raw_line_count, 4)  # blank line excluded, others counted

    def test_parse_pipe_findings_reports_raw_count_even_when_all_fail(self):
        raw = "this is not a pipe line\nneither is this"
        findings, raw_line_count = eval_grumpy_findings.parse_pipe_findings(raw)
        self.assertEqual(findings, [])
        self.assertEqual(raw_line_count, 2)


class TestScore(unittest.TestCase):
    def test_full_recall(self):
        candidates = [
            {"file": "charge.py", "text": "swallow exception with a bare except/pass"},
            {"file": "test_charge.py", "text": "assert true, this tests nothing"},
        ]
        result = eval_grumpy_findings.score(GOLDEN, candidates)
        self.assertEqual(result["recall"], 1.0)
        self.assertEqual(result["misses"], [])

    def test_partial_recall_reports_misses(self):
        candidates = [
            {"file": "charge.py", "text": "swallow exception with a bare except/pass"},
        ]
        result = eval_grumpy_findings.score(GOLDEN, candidates)
        self.assertEqual(result["recall"], 0.5)
        self.assertEqual(result["misses"], ["fake-test-assertion"])

    def test_signal_ratio_penalizes_noise(self):
        candidates = [
            {"file": "charge.py", "text": "swallow exception with a bare except/pass"},
            {"file": "charge.py", "text": "unrelated stylistic nit"},
            {"file": "charge.py", "text": "another unrelated nit"},
        ]
        result = eval_grumpy_findings.score(GOLDEN, candidates)
        self.assertAlmostEqual(result["signal_ratio"], 1 / 3)

    def test_empty_candidates_no_crash(self):
        result = eval_grumpy_findings.score(GOLDEN, [])
        self.assertEqual(result["recall"], 0.0)
        self.assertEqual(result["signal_ratio"], 0.0)

    def test_empty_golden_no_crash(self):
        result = eval_grumpy_findings.score(
            {"findings": []}, [{"file": "x.py", "text": "y"}]
        )
        self.assertEqual(result["recall"], 0.0)
        self.assertEqual(result["misses"], [])


if __name__ == "__main__":
    unittest.main()
