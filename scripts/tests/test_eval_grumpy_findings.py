#!/usr/bin/env python3
"""Tests for scripts/eval-grumpy-findings.py's scoring logic.

Deterministic and offline -- exercises the matcher against fixed
inputs, not a live grumpy/LLM run.
"""

import importlib.util
import os
import sys
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


if __name__ == "__main__":
    unittest.main()
