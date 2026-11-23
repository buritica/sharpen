#!/usr/bin/env python3
"""Tests for scripts/bench-grumpy.py.

Offline and deterministic: no LLM is called. Host runs use a fake CLI on PATH
that writes a canned report, so the runner's plumbing is tested, not a model.
"""

import argparse
import contextlib
import difflib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location(
    "bench_grumpy", os.path.join(ROOT, "scripts", "bench-grumpy.py")
)
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def changed_lines(case, cases_dir=None):
    """{path: set of after/ line numbers that are added or changed}."""
    base = os.path.join(cases_dir or bench.CASES_DIR, case)
    before = bench._tree_files(os.path.join(base, "before"))
    after = bench._tree_files(os.path.join(base, "after"))
    out = {}
    for rel, path in after.items():
        with open(path, encoding="utf-8") as f:
            new = f.read().splitlines()
        old = []
        if rel in before:
            with open(before[rel], encoding="utf-8") as f:
                old = f.read().splitlines()
        lines = set()
        matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
        for tag, _, _, j1, j2 in matcher.get_opcodes():
            if tag in ("replace", "insert"):
                lines.update(range(j1 + 1, j2 + 1))
        if lines:
            out[rel] = lines
    return out


def F(severity, text, file="src/a.py", line=None):
    return {
        "severity": severity,
        "text": text,
        "locations": [{"file": file, "line": line, "end": line}],
    }


KEY = {
    "planted": [
        {
            "id": "swallow",
            "severity": "WARN",
            "file": "src/a.py",
            "lines": [10, 11],
            "keywords": ["swallow", "except"],
        },
        {
            "id": "none-return",
            "severity": "WARN",
            "file": "src/a.py",
            "lines": [14, 14],
            "keywords": ["none"],
        },
        {
            "id": "undefined",
            "severity": "CRIT",
            "file": "src/a.py",
            "lines": [30, 30],
            "keywords": ["undefined"],
        },
    ],
    "acceptable": [
        {
            "id": "log-context",
            "file": "src/a.py",
            "lines": [40, 40],
            "keywords": ["log"],
            "max_severity": "NOTE",
        },
        {
            "id": "backoff",
            "file": "src/a.py",
            "lines": [8, 12],
            "keywords": ["backoff"],
        },
    ],
}


class ParseReportTest(unittest.TestCase):
    REVIEW = textwrap.dedent(
        """\
        # Code Review: billing helpers

        _Somebody wrote a factory for one class._

        ## Critical Issues 🚨

        - [code]: `ChargeResultFormatter` is undefined, NameError on first call [src/billing/charge.py:36]

        ## Serious Concerns ⚠️

        - [errors]: bare except swallows every gateway error [src/billing/charge.py:18-19]
          and the retry loop hides it from the caller.
        - [tests] (judgment): test asserts True [src/billing/test_charge.py:7]

        ## Questionable Decisions 🤔

        - None.

        ## Simplify This ✂️

        - [simplify]: the factory adds nothing [src/billing/charge.py:32]

        ## Strengths

        - [code]: get_last_charge is untouched [src/billing/charge.py:39]

        ## Verdict

        Fix and reship.
        """
    )

    def test_review_sections_map_to_severities(self):
        findings = bench.parse_report(self.REVIEW)
        self.assertEqual(
            [f["severity"] for f in findings], ["CRIT", "WARN", "WARN", "NOTE"]
        )

    def test_continuation_lines_join_the_bullet(self):
        findings = bench.parse_report(self.REVIEW)
        self.assertIn("hides it from the caller", findings[1]["text"])

    def test_locations_and_ranges(self):
        findings = bench.parse_report(self.REVIEW)
        self.assertEqual(
            findings[0]["locations"][0],
            {"file": "src/billing/charge.py", "line": 36, "end": 36},
        )
        self.assertEqual(
            findings[1]["locations"][0],
            {"file": "src/billing/charge.py", "line": 18, "end": 19},
        )

    def test_strengths_and_none_bullets_are_ignored(self):
        texts = " ".join(f["text"] for f in bench.parse_report(self.REVIEW))
        self.assertNotIn("untouched", texts)
        self.assertNotIn("None.", texts)

    def test_imagine_headings_and_detail_sections(self):
        report = textwrap.dedent(
            """\
            # Production Imagination: bulk refresh

            ## 🚨 Bugs Found (Fix These)

            - [concurrency]: 32 workers blow through the 10 req/s rate limit [sync/bulk.py:26]

            ## ⚠️ Serious Concerns (Should Fix)

            - [concurrency]: stats counter race, `+=` across threads [sync/bulk.py:17]

            ## 🤔 Questionable Decisions (Worth Discussing)

            ## Concurrency + Rate Limiting

            ### Rapid-Fire Behavior

            - repeats the rate limit finding [sync/bulk.py:26]

            ## UX & Observability Notes (Not Bugs)

            - log line has no counts [sync/bulk.py:28]
            """
        )
        findings = bench.parse_report(report)
        self.assertEqual([f["severity"] for f in findings], ["CRIT", "WARN"])

    def test_placeholder_bullets_are_not_findings(self):
        report = (
            "## Critical Issues\n\n- No critical issues found.\n\n"
            "## Serious Concerns\n\n- None.\n- Nothing to report\n"
            "- None is returned after retries [a.py:3]\n"
            "- Nothing logs the failure [a.py:5]\n"
        )
        texts = [f["text"][:7] for f in bench.parse_report(report)]
        self.assertEqual(texts, ["None is", "Nothing"])

    def test_deferred_list_alone_ends_the_section(self):
        report = (
            "## Questionable Decisions\n\n- keep [a.py:1]\n\n"
            "Deferred findings honored: 1 \u2014 already marked at the line.\n"
            "- a.py:9: not raised\n"
        )
        self.assertEqual(len(bench.parse_report(report)), 1)

    def test_section_body_lines_counts_what_the_parser_skips(self):
        self.assertEqual(
            bench.section_body_lines("## Serious Concerns\n\n- None.\n"), 0
        )
        self.assertEqual(
            bench.section_body_lines("## Serious Concerns\n\n| a | b |\n"), 1
        )
        prose = "## Questionable Decisions\n\nNone worth raising. `grep -w calc` is clean.\n"
        self.assertEqual(bench.section_body_lines(prose), 0)
        self.assertEqual(
            bench.section_body_lines(
                "## Serious Concerns\n\nThe loop at a.py:3 leaks.\n"
            ),
            1,
        )

    def test_has_severity_sections(self):
        self.assertTrue(bench.has_severity_sections("## Serious Concerns\n"))
        self.assertFalse(bench.has_severity_sections("# Code Review\nAll good.\n"))
        self.assertFalse(bench.has_severity_sections("```\n## Critical Issues\n```\n"))

    def test_held_back_lists_are_not_findings(self):
        report = (
            "## Simplify This\n\n"
            "- [simplify]: delete the factory [src/a.py:3]\n\n"
            "Settled findings held back: 1\n"
            "- src/a_test.py:6: the smoke test tests nothing \u2014 settled as declined: later\n\n"
            "**Deferred findings honored: 1** \u2014 already marked at the line.\n"
            "- src/b.py:9: not raised\n\n"
            "## Strengths\n"
        )
        findings = bench.parse_report(report)
        self.assertEqual([f["text"][:10] for f in findings], ["[simplify]"])

    def test_fenced_blocks_are_skipped(self):
        report = "## Critical Issues\n\n```\n- not a finding [a.py:1]\n```\n- real one [a.py:2]\n"
        findings = bench.parse_report(report)
        self.assertEqual(len(findings), 1)
        self.assertIn("real one", findings[0]["text"])

    def test_file_without_line(self):
        locs = bench.parse_locations(
            "refund has no error handling in `src/billing/charge.py`"
        )
        self.assertEqual(
            locs, [{"file": "src/billing/charge.py", "line": None, "end": None}]
        )

    def test_version_numbers_are_not_files(self):
        self.assertEqual(bench.parse_locations("bumped to v1.2 e.g. later"), [])


class MatchTest(unittest.TestCase):
    T = KEY["planted"][0]

    def test_keywords_match_at_a_word_start_in_any_case(self):
        t = {"file": "src/a.py", "lines": [5, 5], "keywords": ["log"]}
        self.assertTrue(bench.matches(t, F("WARN", "No LOGGING here", line=5)))
        self.assertFalse(bench.matches(t, F("WARN", "the catalog is off", line=5)))
        f = {"file": "src/a.py", "lines": [5, 5], "keywords": ["factory", "smoke"]}
        self.assertTrue(bench.matches(f, F("NOTE", "drop FormatterFactory", line=5)))
        self.assertTrue(bench.matches(f, F("NOTE", "test_smoke is empty", line=5)))
        self.assertFalse(bench.matches(f, F("NOTE", "SATISFACTORY", line=5)))

    def test_line_window_and_keyword(self):
        self.assertTrue(bench.matches(self.T, F("WARN", "swallows errors", line=13)))
        self.assertFalse(bench.matches(self.T, F("WARN", "swallows errors", line=15)))

    def test_keyword_required_even_on_the_line(self):
        self.assertFalse(
            bench.matches(self.T, F("WARN", "variable name is short", line=10))
        )

    def test_different_file_never_matches(self):
        self.assertFalse(
            bench.matches(self.T, F("WARN", "swallows", file="src/b.py", line=10))
        )

    def test_file_only_reference_matches_on_keyword(self):
        self.assertTrue(bench.matches(self.T, F("WARN", "swallows errors", line=None)))

    def test_basename_and_suffix_paths(self):
        self.assertTrue(
            bench.matches(self.T, F("WARN", "swallows", file="a.py", line=10))
        )
        self.assertTrue(
            bench.matches(self.T, F("WARN", "swallows", file="./src/a.py", line=10))
        )
        self.assertFalse(
            bench.matches(
                self.T, F("WARN", "swallows", file="other/src/b/a.py.bak", line=10)
            )
        )

    def test_old_scorer_false_positive_is_gone(self):
        # The keyword-only scorer let any finding in the file that said "none"
        # claim the silent-None issue. A far-away line no longer does.
        self.assertFalse(
            bench.matches(
                KEY["planted"][1], F("NOTE", "none of the names are clear", line=40)
            )
        )


class ScoreTest(unittest.TestCase):
    def test_perfect_run(self):
        s = bench.score(
            KEY,
            [
                F("WARN", "except swallows", line=10),
                F("WARN", "returns None silently", line=14),
                F("CRIT", "undefined name", line=30),
            ],
        )
        self.assertEqual(s["recall"], 1.0)
        self.assertEqual(s["precision"], 1.0)
        self.assertTrue(s["clean"])

    def test_one_finding_cannot_claim_two_issues(self):
        both = F("WARN", "except swallows and returns none", line=12)
        s = bench.score(KEY, [both])
        self.assertEqual(len(s["hits"]), 1)

    def test_matching_is_maximal_not_greedy(self):
        # The first finding could match either issue; the second only one.
        # A greedy pass would give the first to `swallow` and lose `none-return`.
        s = bench.score(
            KEY,
            [F("WARN", "except ... none", line=12), F("WARN", "swallows", line=10)],
        )
        self.assertEqual(s["hits"], ["none-return", "swallow"])

    def test_second_finding_for_a_claimed_issue_is_a_duplicate(self):
        s = bench.score(
            KEY,
            [
                F("CRIT", "undefined", line=30),
                F("WARN", "undefined name again", line=31),
            ],
        )
        self.assertEqual(s["duplicates"], 1)
        self.assertEqual(s["noise"], 0)
        # A duplicate is neither a true nor a false positive.
        self.assertEqual(s["blocking"], 1)
        self.assertEqual(s["precision"], 1.0)
        summary = bench.summarize([{"case": "c", "pass": "review", "score": s}])
        self.assertEqual(summary["c/review"]["duplicates"], 1)

    def test_finding_without_a_file_is_unlocated_not_noise(self):
        s = bench.score(KEY, [F("WARN", "nothing logs anything", file="", line=None)])
        self.assertEqual(s["unlocated"], 1)
        self.assertEqual(s["noise"], 0)
        self.assertIsNone(s["precision"])

    def test_noise_counts_against_precision(self):
        s = bench.score(
            KEY,
            [
                F("CRIT", "undefined", line=30),
                F("WARN", "rename this variable", line=50),
            ],
        )
        self.assertEqual(s["noise_blocking"], 1)
        self.assertEqual(s["precision"], 0.5)
        self.assertFalse(s["clean"])

    def test_acceptable_is_not_noise(self):
        s = bench.score(KEY, [F("WARN", "no backoff between retries", line=9)])
        self.assertEqual(s["noise"], 0)
        self.assertEqual(s["precision"], 1.0)

    def test_acceptable_above_its_cap_is_overrated(self):
        s = bench.score(KEY, [F("WARN", "log line lacks context", line=40)])
        self.assertEqual(s["overrated"], ["log-context"])
        self.assertEqual(s["precision"], 0.0)
        self.assertFalse(s["clean"])
        s = bench.score(KEY, [F("NOTE", "log line lacks context", line=40)])
        self.assertTrue(s["clean"])

    def test_pairing_prefers_matching_severity(self):
        # Both findings mention the factory and the missing class. The CRIT
        # should pair with the CRIT issue, not leave it "underrated".
        key = {
            "planted": [
                {
                    "id": "undefined",
                    "severity": "CRIT",
                    "file": "src/a.py",
                    "lines": [36, 36],
                    "keywords": ["doesn't exist", "nameerror"],
                },
                {
                    "id": "factory",
                    "severity": "NOTE",
                    "file": "src/a.py",
                    "lines": [32, 35],
                    "keywords": ["factory"],
                },
            ]
        }
        s = bench.score(
            key,
            [
                F(
                    "CRIT",
                    "FormatterFactory returns a class that doesn't exist: NameError",
                    line=34,
                ),
                F(
                    "NOTE",
                    "factory returns a class that doesn't exist, delete it",
                    line=33,
                ),
            ],
        )
        self.assertEqual(s["hits"], ["factory", "undefined"])
        self.assertEqual(s["underrated"], [])

    def test_underrated_planted_issue(self):
        s = bench.score(KEY, [F("NOTE", "undefined", line=30)])
        self.assertEqual(s["underrated"], ["undefined"])

    def test_notes_do_not_affect_precision(self):
        s = bench.score(KEY, [F("NOTE", "style nit", line=50)])
        self.assertIsNone(s["precision"])
        self.assertEqual(s["noise"], 1)
        self.assertTrue(s["clean"])

    def test_empty_report_on_clean_case(self):
        s = bench.score({"planted": [], "acceptable": []}, [])
        self.assertIsNone(s["recall"])
        self.assertTrue(s["clean"])

    def test_expect_absent_is_left_out_of_recall(self):
        key = dict(KEY, expect_absent=["undefined"])
        s = bench.score(
            key,
            [
                F("WARN", "except swallows", line=10),
                F("WARN", "returns None silently", line=14),
            ],
        )
        self.assertEqual(s["recall"], 1.0)
        self.assertEqual(s["misses"], [])
        self.assertEqual(s["present"], [])
        self.assertTrue(s["clean"])

    def test_expect_absent_finding_that_shows_up_fails(self):
        key = dict(KEY, expect_absent=["undefined"])
        s = bench.score(
            key,
            [
                F("WARN", "except swallows", line=10),
                F("WARN", "returns None silently", line=14),
                F("CRIT", "undefined name", line=30),
            ],
        )
        self.assertEqual(s["recall"], 1.0)
        self.assertEqual(s["present"], ["undefined"])
        self.assertEqual(s["precision"], 2 / 3)
        self.assertFalse(s["clean"])
        summary = bench.summarize([{"case": "c", "pass": "review", "score": s}])
        self.assertEqual(summary["c/review"]["held_back_failures"], 1)


def _results(summary_overrides):
    base = {
        "runs": 3,
        "failed_runs": 0,
        "recall": 1.0,
        "recall_spread": 0.0,
        "precision": 0.9,
        "noise_blocking": 0.0,
    }
    return {"summary": {k: dict(base, **v) for k, v in summary_overrides.items()}}


class SummaryCompareTest(unittest.TestCase):
    def test_summarize_counts_failed_runs_and_spread(self):
        runs = [
            {
                "case": "c",
                "pass": "review",
                "score": bench.score(KEY, [F("CRIT", "undefined", line=30)]),
            },
            {"case": "c", "pass": "review", "score": bench.score(KEY, [])},
            {"case": "c", "pass": "review", "status": "no-report"},
        ]
        s = bench.summarize(runs)["c/review"]
        self.assertEqual(s["failed_runs"], 1)
        self.assertAlmostEqual(s["recall"], 1 / 6)
        self.assertAlmostEqual(s["recall_spread"], 1 / 3)

    def test_identical_runs_pass(self):
        r = _results({"a/review": {}})
        self.assertEqual(bench.compare(r, r)[0], [])

    def test_recall_drop_and_noise_rise_are_regressions(self):
        problems, _ = bench.compare(
            _results({"a/review": {}}),
            _results({"a/review": {"recall": 0.75, "noise_blocking": 2.0}}),
        )
        self.assertEqual(len(problems), 2)

    def test_thresholds(self):
        problems, _ = bench.compare(
            _results({"a/review": {}}),
            _results({"a/review": {"noise_blocking": 0.5}}),
            noise_rise=1.0,
        )
        self.assertEqual(problems, [])

    def test_new_failed_runs_are_regressions(self):
        problems, _ = bench.compare(
            _results({"a/review": {}}), _results({"a/review": {"failed_runs": 1}})
        )
        self.assertEqual(len(problems), 1)

    def test_re_raised_settled_finding_is_a_regression(self):
        problems, _ = bench.compare(
            _results({"a/review": {}}),
            _results({"a/review": {"held_back_failures": 1}}),
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("settled", problems[0])

    def test_rescore_rewrites_scores_from_saved_reports(self):
        with tempfile.TemporaryDirectory() as d:
            report = os.path.join(d, "billing-planted-review-1.md")
            with open(report, "w", encoding="utf-8") as f:
                f.write(
                    "## Serious Concerns\n\n"
                    "- except swallows the error [src/billing/charge.py:18]\n"
                )
            results = os.path.join(d, "results.json")
            run = {"case": "billing-planted", "pass": "review"}
            run["label"] = "billing-planted-review-1"
            unsaved = dict(run, label="billing-planted-review-2", status="no-report")
            with open(results, "w", encoding="utf-8") as f:
                json.dump({"runs": [run, unsaved], "summary": {}}, f)
            with contextlib.redirect_stdout(io.StringIO()):
                bench.main(["rescore", results])
            with open(results, encoding="utf-8") as f:
                meta = json.load(f)
            self.assertEqual(meta["runs"][0]["score"]["hits"], ["swallowed-exception"])
            self.assertNotIn("score", meta["runs"][1])
            self.assertIn("billing-planted/review", meta["summary"])

    def test_case_missing_from_candidate_is_a_regression(self):
        problems, notes = bench.compare(
            _results({"a/review": {}, "b/review": {}}), _results({"a/review": {}})
        )
        self.assertEqual(problems, ["b/review: in baseline, not in candidate"])

    def test_empty_candidate_is_a_regression(self):
        problems, _ = bench.compare(_results({"a/review": {}}), {"summary": {}})
        self.assertIn("candidate has no runs", problems)

    def test_failed_runs_compare_as_rates(self):
        problems, _ = bench.compare(
            _results({"a/review": {"runs": 1, "failed_runs": 1}}),
            _results({"a/review": {"runs": 3, "failed_runs": 1}}),
        )
        self.assertEqual(problems, [])

    def test_underrated_and_interruptions_are_regressions_save_failures_a_note(self):
        cand = _results({"a/review": {"underrated": 1.0, "from_output": 3}})
        cand["interrupted"] = True
        problems, notes = bench.compare(
            _results({"a/review": {"underrated": 0.0, "from_output": 0}}), cand
        )
        self.assertEqual(len(problems), 2)
        self.assertIn("without saving", notes[0])

    def test_overrated_and_duplicate_rises_are_regressions(self):
        problems, _ = bench.compare(
            _results({"a/review": {"overrated": 0.0, "duplicates": 0.0}}),
            _results({"a/review": {"overrated": 1.0, "duplicates": 2.0}}),
        )
        self.assertEqual(len(problems), 2)

    def test_precision_drop_boundary(self):
        problems, _ = bench.compare(
            _results({"a/review": {"precision": 0.9}}),
            _results({"a/review": {"precision": 0.8}}),
            precision_drop=0.1 + 1e-9,
        )
        self.assertEqual(problems, [])
        problems, _ = bench.compare(
            _results({"a/review": {}}),
            _results({"a/review": {"recall": 0.9}}),
            recall_drop=0.2,
        )
        self.assertEqual(problems, [])

    def test_new_case_in_candidate_is_a_note(self):
        problems, notes = bench.compare(
            _results({"a/review": {}}), _results({"a/review": {}, "b/review": {}})
        )
        self.assertEqual(problems, [])
        self.assertEqual(notes, ["b/review: new in candidate"])

    def test_precision_drop_is_a_regression(self):
        problems, _ = bench.compare(
            _results({"a/review": {}}), _results({"a/review": {"precision": 0.5}})
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("precision 90% -> 50%", problems[0])


class CommandTest(unittest.TestCase):
    def test_claude_native(self):
        argv, stdin = bench.build_command(
            "claude", "review", "claude-opus-5-5", grumpy="/g"
        )
        self.assertEqual(argv[:4], ["claude", "-p", "--plugin-dir", "/g"])
        self.assertIn("claude-opus-5-5", argv)
        self.assertEqual(argv[-1], "/grumpy:review")
        self.assertIsNone(stdin)

    def test_no_model_drops_the_flag(self):
        for host in bench.HOSTS:
            argv, _ = bench.build_command(host, "review")
            self.assertNotIn("--model", argv, host)
            self.assertNotIn("", argv, host)

    def test_inline_hosts_send_the_skill_on_stdin(self):
        for host in ("codex", "pi"):
            argv, stdin = bench.build_command(host, "imagine", "m")
            self.assertIn("m", argv)
            self.assertIn("Grumpy Imagine", stdin)
            self.assertNotIn("$ARGUMENTS", stdin)
            self.assertFalse(stdin.lstrip().startswith("---"))

    def test_template_override(self):
        argv, _ = bench.build_command(
            "codex", "review", "x", template="mycodex run -m {model}"
        )
        self.assertEqual(argv, ["mycodex", "run", "-m", "x"])

    def test_unknown_invocation(self):
        with self.assertRaises(ValueError):
            bench.build_command("codex", "review", invoke="native")

    def test_oneshot_runs_this_checkouts_script(self):
        argv, stdin = bench.build_command(
            "oneshot", "imagine", "m", grumpy="/g", provider="compatible"
        )
        self.assertEqual(
            argv,
            [
                sys.executable,
                "/g/scripts/oneshot.py",
                "imagine",
                "--provider",
                "compatible",
                "--model",
                "m",
            ],
        )
        self.assertIsNone(stdin)
        # No provider, no flag: oneshot.py picks SHARPEN_LLM_PROVIDER itself.
        argv, _ = bench.build_command("oneshot", "review", "m", grumpy="/g")
        self.assertNotIn("--provider", argv)
        with self.assertRaises(ValueError):
            bench.build_command("oneshot", "review", invoke="inline")

    def test_bare_placeholders_drop_without_a_value(self):
        argv, _ = bench.build_command(
            "codex", "review", template="x {model} --p {provider} y"
        )
        self.assertEqual(argv, ["x", "y"])

    def test_oneshot_version_names_grumpy_or_nothing(self):
        self.assertRegex(bench.oneshot_version(), r"^grumpy \d+\.\d+\.\d+ oneshot\.py$")
        self.assertIsNone(bench.oneshot_version(grumpy="/nonexistent"))

    def test_oneshot_prompt_hash_covers_the_script(self):
        self.assertNotEqual(
            bench.prompt_hash("review"), bench.prompt_hash("review", host="oneshot")
        )

    def test_oneshot_prompt_hash_covers_the_prompt_extraction(self):
        # The extractors live in oneshot_prompts.py: an edit there changes
        # the prompts, so it must change the hash compare reads.
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        os.makedirs(os.path.join(root, "scripts"))
        for name in ("oneshot.py", "oneshot_prompts.py"):
            with open(os.path.join(root, "scripts", name), "w") as f:
                f.write("x = 1\n")
        before = bench.prompt_hash("review", grumpy=root, host="oneshot")
        with open(os.path.join(root, "scripts", "oneshot_prompts.py"), "a") as f:
            f.write("y = 2\n")
        self.assertNotEqual(
            before, bench.prompt_hash("review", grumpy=root, host="oneshot")
        )


class FixtureTest(unittest.TestCase):
    """Every shipped case is internally consistent."""

    def test_cases_exist_for_both_passes(self):
        cases = bench.list_cases()
        self.assertGreaterEqual(len(cases), 6)
        passes = {p for c in cases for p in bench.load_key(c)["passes"]}
        self.assertEqual(passes, set(bench.PASSES))
        self.assertTrue(
            any(not bench.load_key(c)["planted"] for c in cases),
            "need a clean case for noise",
        )

    def test_keys_point_at_real_changed_lines(self):
        for case in bench.list_cases():
            key = bench.load_key(case)
            changed = changed_lines(case)
            after = os.path.join(bench.CASES_DIR, case, "after")
            ids = [t["id"] for t in key["planted"] + key["acceptable"]]
            self.assertEqual(len(ids), len(set(ids)), case)
            basenames = [os.path.basename(p) for p in bench._tree_files(after)]
            self.assertEqual(
                len(basenames), len(set(basenames)), f"{case}: duplicate basenames"
            )
            for t in key["planted"]:
                self.assertIn(t["severity"], bench.RANK, f"{case}/{t['id']}")
                self.assertTrue(t["keywords"], f"{case}/{t['id']}")
                lo, hi = t["lines"]
                self.assertTrue(
                    changed.get(t["file"], set()) & set(range(lo, hi + 1)),
                    f"{case}/{t['id']} does not touch a changed line",
                )
            for t in key["acceptable"]:
                with open(os.path.join(after, t["file"]), encoding="utf-8") as f:
                    n = len(f.read().splitlines())
                self.assertLessEqual(t["lines"][1], n, f"{case}/{t['id']}")
                if "max_severity" in t:
                    self.assertIn(t["max_severity"], bench.RANK)

    def test_an_ideal_report_scores_perfectly(self):
        for case in bench.list_cases():
            key = bench.load_key(case)
            bullets = {"CRIT": [], "WARN": [], "NOTE": []}
            for t in key["planted"]:
                if t["id"] in key["expect_absent"]:
                    continue
                bullets[t["severity"]].append(
                    f"- [x]: {t['keywords'][0]} [{t['file']}:{t['lines'][0]}]"
                )
            report = (
                "## Critical Issues\n\n"
                + "\n".join(bullets["CRIT"])
                + "\n\n## Serious Concerns\n\n"
                + "\n".join(bullets["WARN"])
                + "\n\n## Questionable Decisions\n\n"
                + "\n".join(bullets["NOTE"])
                + "\n"
            )
            s = bench.score(key, bench.parse_report(report))
            if key["planted"]:
                self.assertEqual(s["recall"], 1.0, f"{case}: {s['misses']}")
            self.assertTrue(s["clean"], case)

    def assert_clean_worktree(self, repo):
        status = subprocess.run(
            ["git", "-C", repo, "status", "--porcelain"],
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(status, "")

    def test_list_cases_without_a_cases_dir_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(bench.list_cases(os.path.join(d, "missing")), [])

    def test_no_cases_flag_selects_every_case(self):
        self.assertEqual(bench._selected_cases(None), bench.list_cases())

    def test_build_repo_commits_the_change_on_a_branch(self):
        case = "billing-planted"
        with tempfile.TemporaryDirectory() as d:
            branch = bench.build_repo(case, d)
            self.assertEqual(branch, "bench/billing-planted")
            names = subprocess.run(
                ["git", "-C", d, "diff", "--name-only", "main...HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.split()
            self.assertEqual(
                sorted(names), ["src/billing/charge.py", "src/billing/test_charge.py"]
            )
            self.assert_clean_worktree(d)

    def test_build_repo_deletes_files_the_change_removed(self):
        with tempfile.TemporaryDirectory() as cases, tempfile.TemporaryDirectory() as d:
            case = os.path.join(cases, "drop")
            for side, names in (
                ("before", ["keep.py", "gone.py"]),
                ("after", ["keep.py"]),
            ):
                os.makedirs(os.path.join(case, side))
                for name in names:
                    with open(os.path.join(case, side, name), "w") as f:
                        f.write(f"# {side}\n")
            with open(os.path.join(case, "key.json"), "w") as f:
                json.dump({"planted": []}, f)
            bench.build_repo("drop", d, cases)
            self.assertFalse(os.path.exists(os.path.join(d, "gone.py")))
            self.assertTrue(os.path.exists(os.path.join(d, "keep.py")))
            self.assert_clean_worktree(d)

    def test_artifacts_land_in_the_branch_report_folder(self):
        case = "billing-settled"
        with tempfile.TemporaryDirectory() as d:
            branch = bench.build_repo(case, d)
            fix = os.path.join(d, ".sharpen", "grumpy", *branch.split("/"), "fix.md")
            with open(fix, encoding="utf-8") as f:
                self.assertIn("## Settled", f.read())
            self.assert_clean_worktree(d)

    def test_find_report_reads_only_the_branch_folder(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(bench.find_report(d, "bench/x", "review"))
            odd = os.path.join(d, ".sharpen", "grumpy", "elsewhere")
            os.makedirs(odd)
            with open(os.path.join(odd, "review.md"), "w") as f:
                f.write("# Code Review\n")
            self.assertIsNone(bench.find_report(d, "bench/x", "review"))
            right = os.path.join(d, ".sharpen", "grumpy", "bench", "x")
            os.makedirs(right)
            with open(os.path.join(right, "review.md"), "w") as f:
                f.write("# Code Review\n")
            self.assertEqual(
                bench.find_report(d, "bench/x", "review"),
                os.path.join(right, "review.md"),
            )

    def test_bad_keys_are_refused(self):
        good = {"planted": [{"id": "a", "severity": "WARN"}], "acceptable": []}
        good.update(expect_absent=[], passes=["review"])
        bench.validate_key(good)
        for change in (
            {"expect_absent": ["typo"]},
            {"planted": [{"id": "a", "severity": "CRITICAL"}]},
            {"acceptable": [{"id": "a"}]},
            {"acceptable": [{"id": "b", "max_severity": "HIGH"}]},
            {"passes": ["reveiw"]},
        ):
            with self.assertRaises(ValueError, msg=change):
                bench.validate_key(dict(good, **change))

    def test_cli_version_of_a_missing_binary_is_none(self):
        self.assertIsNone(bench.cli_version("definitely-not-a-binary-xyz"))


FAKE_CLI = """#!/usr/bin/env python3
import os, subprocess, sys
if sys.argv[1:] == ["--version"]:
    print("fake 1.0")
    sys.exit(0)
mode = os.environ.get("FAKE_MODE", "report")
open(os.environ["FAKE_ARGV_LOG"], "a").write(" ".join(sys.argv[1:]) + "\\n")
if mode == "report":
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
    d = os.path.join(".sharpen", "grumpy", *branch.split("/"))
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "review.md"), "w").write(
        "## Critical Issues\\n\\n- [code]: ChargeResultFormatter is undefined [src/billing/charge.py:36]\\n"
    )
elif mode == "print":
    print("# Code Review: x\\n\\n## Critical Issues\\n\\n- ChargeResultFormatter is undefined [src/billing/charge.py:36]")
print("done")
"""


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        for name in ("claude", "codex", "pi"):
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(FAKE_CLI.replace("/usr/bin/env python3", sys.executable))
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        self.log = os.path.join(self.tmp, "argv.log")
        self.env = {
            "PATH": self.bin + os.pathsep + os.environ["PATH"],
            "FAKE_ARGV_LOG": self.log,
        }
        self.old_env = {k: os.environ.get(k) for k in [*self.env, "FAKE_MODE"]}
        os.environ.update(self.env)

    def tearDown(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = bench.main(list(args))
        return code, out.getvalue()

    def results(self, out):
        """(run_dir, parsed results.json) of the single run under `out`."""
        (run_dir,) = os.listdir(out)
        with open(os.path.join(out, run_dir, "results.json")) as f:
            return os.path.join(out, run_dir), json.load(f)

    def test_run_scores_the_written_report(self):
        out = os.path.join(self.tmp, "results")
        code, stdout = self.run_cli(
            "run",
            "--host",
            "codex",
            "--model",
            "m1",
            "--cases",
            "billing-planted",
            "--repeat",
            "2",
            "--out",
            out,
        )
        self.assertEqual(code, 0, stdout)
        run_dir, results = self.results(out)
        self.assertEqual(results["host"], "codex")
        self.assertFalse(results["flags_verified"])
        self.assertEqual(len(results["runs"]), 2)
        self.assertEqual(results["runs"][0]["score"]["hits"], ["undefined-formatter"])
        self.assertAlmostEqual(
            results["summary"]["billing-planted/review"]["recall"], 1 / 7
        )
        self.assertTrue(
            os.path.exists(os.path.join(run_dir, "billing-planted-review-1.md"))
        )
        with open(self.log) as f:
            self.assertIn("exec --model m1", f.read())

    def test_only_passes_the_case_declares_run(self):
        out = os.path.join(self.tmp, "results")
        self.run_cli(
            "run",
            "--host",
            "pi",
            "--cases",
            "billing-planted",
            "--repeat",
            "1",
            "--out",
            out,
        )
        with open(self.log) as f:
            self.assertEqual(len(f.read().splitlines()), 1)

    def test_no_report_is_a_failed_run(self):
        os.environ["FAKE_MODE"] = "silent"
        out = os.path.join(self.tmp, "results")
        code, _ = self.run_cli(
            "run",
            "--host",
            "claude",
            "--cases",
            "clean-refactor",
            "--repeat",
            "1",
            "--out",
            out,
        )
        self.assertEqual(code, 1)
        run_dir, results = self.results(out)
        self.assertEqual(results["runs"][0]["status"], "no-report")
        with open(os.path.join(run_dir, "clean-refactor-review-1.log")) as f:
            self.assertIn("done", f.read())

    def test_report_printed_but_not_saved_is_scored_from_output(self):
        os.environ["FAKE_MODE"] = "print"
        out = os.path.join(self.tmp, "results")
        code, _ = self.run_cli(
            "run",
            "--host",
            "claude",
            "--cases",
            "billing-planted",
            "--repeat",
            "1",
            "--out",
            out,
        )
        self.assertEqual(code, 0)
        run = self.results(out)[1]["runs"][0]
        self.assertEqual(run["status"], "scored-from-output")
        self.assertEqual(run["score"]["hits"], ["undefined-formatter"])

    def test_timeout_and_missing_binary_are_recorded(self):
        hang = [sys.executable, "-c", "import time; time.sleep(10)"]
        r = bench.run_one(
            "clean-refactor", "review", hang, None, self.tmp, "t", 0.5, False
        )
        self.assertEqual(r["status"], "timeout")
        self.assertIsNone(r["exit_code"])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "t.log")))
        # Partial output is decoded, and a printed report is not scored.
        slow_print = [
            sys.executable,
            "-c",
            "import sys, time; print('# Code Review\\n\\n## Critical Issues\\n\\n- x [a.py:1]');"
            " sys.stdout.flush(); time.sleep(10)",
        ]
        r = bench.run_one(
            "clean-refactor", "review", slow_print, None, self.tmp, "t2", 1.5, False
        )
        self.assertEqual(r["status"], "timeout")
        self.assertNotIn("score", r)
        with open(os.path.join(self.tmp, "t2.log"), encoding="utf-8") as f:
            self.assertTrue(f.read().startswith("# Code Review"))
        r = bench.run_one(
            "clean-refactor",
            "review",
            ["definitely-not-a-binary-xyz"],
            None,
            self.tmp,
            "m",
            5,
            False,
        )
        self.assertEqual(r["status"], "cli-missing")
        self.assertIn("not found on PATH", r["error"])

    def test_dropped_bullets_make_a_run_unparsed(self):
        report = "# Code Review\\n\\n## Serious Concerns\\n\\n    - indented [a.py:1]"
        out = [sys.executable, "-c", f"print('{report}')"]
        r = bench.run_one(
            "clean-refactor", "review", out, None, self.tmp, "d", 5, False
        )
        self.assertEqual(r["status"], "unparsed")
        self.assertIn("report", r)

    def test_nonzero_exit_is_a_failed_run(self):
        out = [
            sys.executable,
            "-c",
            "import sys; print('# Code Review\\n\\n## Critical Issues\\n\\n- x [a.py:1]'); sys.exit(3)",
        ]
        r = bench.run_one(
            "clean-refactor", "review", out, None, self.tmp, "n", 5, False
        )
        self.assertEqual(r["status"], "exit-nonzero")
        self.assertEqual(r["exit_code"], 3)

    def test_bad_invocation_fails_before_any_run_or_directory(self):
        out = os.path.join(self.tmp, "res")
        code, _ = self.run_cli(
            "run",
            "--host",
            "codex",
            "--invoke",
            "native",
            "--repeat",
            "1",
            "--out",
            out,
        )
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(out))

    def test_same_second_runs_get_their_own_directory(self):
        base = os.path.join(self.tmp, "stamp")
        self.assertEqual(bench._new_dir(base), base)
        self.assertEqual(bench._new_dir(base), base + "-2")

    def test_unparseable_report_is_a_failed_run(self):
        out = [sys.executable, "-c", "print('# Code Review\\n\\nAll good.')"]
        r = bench.run_one(
            "clean-refactor", "review", out, None, self.tmp, "u", 5, False
        )
        self.assertEqual(r["status"], "unparsed")
        self.assertNotIn("score", r)

    def test_host_markers_are_not_inherited(self):
        probe = [
            sys.executable,
            "-c",
            "import os; print('CC=' + os.environ.get('CLAUDECODE', '-'),"
            " 'CPR=' + os.environ.get('CLAUDE_PLUGIN_ROOT', '-'),"
            " 'GPR=' + str(bool(os.environ.get('GRUMPY_PLUGIN_ROOT'))))",
        ]
        old = {k: os.environ.get(k) for k in ("CLAUDECODE", "CLAUDE_PLUGIN_ROOT")}
        os.environ.update(CLAUDECODE="1", CLAUDE_PLUGIN_ROOT="/x")
        try:
            bench.run_one(
                "clean-refactor", "review", probe, None, self.tmp, "e", 5, False
            )
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        with open(os.path.join(self.tmp, "e.log"), encoding="utf-8") as f:
            self.assertEqual(f.read().split()[:3], ["CC=-", "CPR=-", "GPR=True"])

    def test_a_run_that_raises_is_recorded_not_fatal(self):
        args = argparse.Namespace(timeout=5, keep=False)
        r = bench._run_labelled(
            "no-such-case", "review", ["x"], None, self.tmp, "z", args
        )
        self.assertEqual(r["status"], "error")
        self.assertIn("error", r)

    def test_analysis_pin_is_written_where_the_passes_read_it(self):
        probe = [sys.executable, "-c", "print(open('.sharpen/models.yaml').read())"]
        bench.run_one(
            "clean-refactor",
            "review",
            probe,
            None,
            self.tmp,
            "p",
            5,
            False,
            analysis="sonnet",
        )
        with open(os.path.join(self.tmp, "p.log"), encoding="utf-8") as f:
            self.assertIn("analysis:\n    native: sonnet", f.read())

    def test_models_map_is_consistent(self):
        path = os.path.join(bench.GRUMPY, "models.yaml")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        roles = dict(
            (m.group(1), m.group(2))
            for m in re.finditer(r"^  ([a-z-]+):[^\n]*\n((?:    .*\n)+)", text, re.M)
        )
        self.assertEqual(set(roles), {"audit", "analysis", "exec", "exec-trivial"})
        for name, body in roles.items():
            cols = dict(re.findall(r"^    (\w+): (\S+)", body, re.M))
            self.assertEqual(set(cols), {"claude", "native", "codex", "openai"}, name)
            self.assertIn(cols["native"], {"haiku", "sonnet", "opus"}, name)
        ladder = re.search(r"^escalation: \[(.*)\]", text, re.M).group(1)
        self.assertEqual(
            [r.strip() for r in ladder.split(",")], ["exec-trivial", "exec", "analysis"]
        )

    def test_keep_leaves_the_repo_behind(self):
        quiet = [sys.executable, "-c", "pass"]
        r = bench.run_one(
            "clean-refactor", "review", quiet, None, self.tmp, "k", 5, True
        )
        try:
            self.assertTrue(os.path.isdir(os.path.join(r["repo"], ".git")))
        finally:
            shutil.rmtree(r["repo"], ignore_errors=True)

    def test_missing_cli(self):
        os.environ["PATH"] = self.tmp
        code, _ = self.run_cli(
            "run", "--host", "codex", "--repeat", "1", "--out", self.tmp
        )
        self.assertEqual(code, 2)

    def test_unknown_case(self):
        with self.assertRaises(SystemExit):
            self.run_cli("run", "--host", "codex", "--cases", "nope", "--out", self.tmp)

    def test_score_and_compare_subcommands(self):
        report = os.path.join(self.tmp, "r.md")
        with open(report, "w") as f:
            f.write(
                "## Serious Concerns\n\n- bare except swallows errors [src/billing/charge.py:18]\n"
            )
        code, stdout = self.run_cli("score", report, "--case", "billing-planted")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["hits"], ["swallowed-exception"])

        a, b = os.path.join(self.tmp, "a.json"), os.path.join(self.tmp, "b.json")
        with open(a, "w") as f:
            json.dump(_results({"x/review": {}}), f)
        with open(b, "w") as f:
            json.dump(_results({"x/review": {"recall": 0.5}}), f)
        self.assertEqual(self.run_cli("compare", a, a)[0], 0)
        code, stdout = self.run_cli("compare", a, b)
        self.assertEqual(code, 1)
        self.assertIn("REGRESSION", stdout)

    def test_compare_prints_notes_and_prompt_drift(self):
        a, b = os.path.join(self.tmp, "a.json"), os.path.join(self.tmp, "b.json")
        with open(a, "w") as f:
            json.dump(
                dict(_results({"x/review": {}}), prompt_hashes={"review": "1"}), f
            )
        with open(b, "w") as f:
            json.dump(
                dict(
                    _results({"x/review": {}, "y/review": {}}),
                    prompt_hashes={"review": "2"},
                ),
                f,
            )
        code, stdout = self.run_cli("compare", a, b)
        self.assertEqual(code, 0)
        self.assertIn("prompts differ", stdout)
        self.assertIn("note: y/review: new in candidate", stdout)
        self.assertIn("no regressions", stdout)

    def test_commands_and_cases_subcommands(self):
        code, stdout = self.run_cli("commands", "--host", "claude", "--pass", "review")
        self.assertEqual(code, 0)
        self.assertTrue(stdout.startswith("review: claude -p"), stdout)
        self.assertNotIn("unverified", stdout)
        code, stdout = self.run_cli("commands", "--host", "codex")
        self.assertEqual(code, 0)
        self.assertIn("imagine: codex exec", stdout)
        self.assertIn("SKILL.md as the prompt", stdout)
        self.assertIn("unverified", stdout)
        # The same refusal `run` gives, not a silently dropped flag.
        code, _ = self.run_cli("commands", "--host", "pi", "--provider", "openai")
        self.assertEqual(code, 2)
        code, stdout = self.run_cli("cases")
        self.assertEqual(code, 0)
        self.assertIn("billing-planted", stdout)
        self.assertIn("planted=", stdout)


class FindReportTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def _write(self, *parts):
        path = os.path.join(self.repo, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").write("report\n")
        return path

    def test_finds_the_report_at_the_new_root(self):
        path = self._write(".sharpen", "grumpy", "bench", "x", "review.md")
        self.assertEqual(bench.find_report(self.repo, "bench/x", "review"), path)

    def test_a_report_under_claude_is_not_read(self):
        # The bench runs this checkout's grumpy, which writes only under
        # .sharpen/; a report under .claude/ is a contract miss.
        self._write(".claude", "grumpy", "bench", "x", "review.md")
        self.assertIsNone(bench.find_report(self.repo, "bench/x", "review"))


class _ChatStub(BaseHTTPRequestHandler):
    """An OpenAI-compatible server that answers every pass with one finding
    line and the aggregation with a report naming the counter race."""

    calls = 0
    report = textwrap.dedent(
        """\
        # Production Imagination: bulk refresh

        _It will page someone._

        ## 🚨 Bugs Found (Fix These)

        - [concurrency]: the shared counter race loses updates with `+=` from 32 threads [sync/bulk.py:21]

        ## ⚠️ Serious Concerns (Should Fix)

        ## 🤔 Questionable Decisions (Worth Discussing)

        ## Strengths

        ## Verdict

        Fix and reship.
        """
    )

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).calls += 1
        prompt = body["messages"][-1]["content"]
        if "You are the aggregator" in prompt:
            content = self.report
        else:
            content = "CRIT|sync/bulk.py:21|counter race|fact|concurrency"
        data = json.dumps(
            {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class OneshotHostTest(unittest.TestCase):
    """The oneshot host end to end: the bench runs this checkout's oneshot.py
    in a fixture repo against a local stub server and scores what it saved."""

    def setUp(self):
        _ChatStub.calls = 0
        self.server = HTTPServer(("127.0.0.1", 0), _ChatStub)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        keep = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("SHARPEN_LLM", "OPENAI"))
        }
        # No gh on PATH: diff_base falls back to the fixture's main.
        keep.update(PATH="/usr/bin:/bin", SHARPEN_LLM_URL=url)
        patcher = mock.patch.dict(os.environ, keep, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bench.main(list(args))
        return code, out.getvalue() + err.getvalue()

    def test_imagine_on_a_compatible_server_is_scored(self):
        out = os.path.join(self.tmp, "results")
        code, text = self.run_cli(
            "run",
            "--host",
            "oneshot",
            "--provider",
            "compatible",
            "--model",
            "stub-m",
            "--cases",
            "imagine-concurrency",
            "--repeat",
            "1",
            "--out",
            out,
        )
        self.assertEqual(code, 0, text)
        (run_dir,) = os.listdir(out)
        self.assertIn("-oneshot-compatible-stub-m", run_dir)
        with open(os.path.join(out, run_dir, "results.json")) as f:
            results = json.load(f)
        self.assertEqual(results["host"], "oneshot")
        self.assertEqual(results["provider"], "compatible")
        self.assertTrue(results["flags_verified"])
        self.assertTrue(results["cli_version"].startswith("grumpy "))
        (run,) = results["runs"]
        self.assertEqual(run["status"], "scored", run)
        self.assertEqual(run["score"]["hits"], ["counter-race"])
        # Five simulation passes and one aggregation per run.
        self.assertEqual(_ChatStub.calls, 6)
        with open(os.path.join(out, run_dir, "imagine-concurrency-imagine-1.md")) as f:
            self.assertIn("_Model: stub-m via compatible (one-shot", f.read())

    def test_setup_problems_fail_before_any_run(self):
        out = os.path.join(self.tmp, "results")
        base = [
            "run",
            "--host",
            "oneshot",
            "--cases",
            "imagine-concurrency",
            "--out",
            out,
        ]
        cases = {
            "no model": (["--provider", "compatible"], {}, "needs --model"),
            "no key": (["--provider", "openai"], {}, "OPENAI_API_KEY"),
            "no url": (
                ["--provider", "compatible", "--model", "m"],
                {"SHARPEN_LLM_URL": ""},
                "SHARPEN_LLM_URL",
            ),
            "analysis pin": (
                ["--provider", "compatible", "--model", "m", "--analysis", "opus"],
                {},
                "--analysis",
            ),
        }
        for name, (extra, env, says) in cases.items():
            with self.subTest(name), mock.patch.dict(os.environ, env):
                code, text = self.run_cli(*base, *extra)
                self.assertEqual(code, 2)
                self.assertIn(says, text)
        with mock.patch.dict(os.environ, {"SHARPEN_LLM_PROVIDER": "bogus"}):
            code, text = self.run_cli(*base, "--model", "m")
        self.assertEqual(code, 2)
        self.assertIn("unknown provider 'bogus'", text)
        code, text = self.run_cli(
            "run", "--host", "pi", "--provider", "openai", "--out", out
        )
        self.assertEqual(code, 2)
        self.assertIn("--provider is for --host oneshot", text)
        self.assertFalse(os.path.exists(out))
        self.assertEqual(_ChatStub.calls, 0)


if __name__ == "__main__":
    unittest.main()
