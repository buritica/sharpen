#!/usr/bin/env python3
"""Tests for scripts/bench-grumpy.py.

Offline and deterministic: no LLM is called. Host runs use a fake CLI on PATH
that writes a canned report, so the runner's plumbing is tested, not a model.
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location(
    "bench_grumpy", os.path.join(ROOT, "scripts", "bench-grumpy.py")
)
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


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


class ParsePipeAndJsonTest(unittest.TestCase):
    def test_pipe_lines(self):
        raw = "CRIT|src/a.py:30|name is undefined | really|fact|code\nCONTEXT|area ok\nWARN|bad\n"
        findings = bench.parse_pipe(raw)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["locations"][0]["line"], 30)
        self.assertIn("| really", findings[0]["text"])

    def test_json_old_shape(self):
        findings = bench.normalize_json(
            [{"severity": "WARN", "file": "src/a.py", "text": "swallows"}]
        )
        self.assertEqual(
            findings[0]["locations"][0], {"file": "src/a.py", "line": None, "end": None}
        )


class MatchTest(unittest.TestCase):
    T = KEY["planted"][0]

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
        self.assertEqual(s["precision"], 1.0)

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

    def test_missing_case_is_a_note(self):
        problems, notes = bench.compare(
            _results({"a/review": {}, "b/review": {}}), _results({"a/review": {}})
        )
        self.assertEqual(problems, [])
        self.assertEqual(notes, ["b/review: not in candidate"])


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
            changed = bench.changed_lines(case)
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
            status = subprocess.run(
                ["git", "-C", d, "status", "--porcelain"],
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(status, "")

    def test_artifacts_land_in_the_branch_report_folder(self):
        case = "billing-settled"
        with tempfile.TemporaryDirectory() as d:
            branch = bench.build_repo(case, d)
            fix = os.path.join(d, ".claude", "grumpy", *branch.split("/"), "fix.md")
            with open(fix, encoding="utf-8") as f:
                self.assertIn("## Settled", f.read())
            status = subprocess.run(
                ["git", "-C", d, "status", "--porcelain"],
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(status, "")


FAKE_CLI = """#!/usr/bin/env python3
import os, subprocess, sys
if sys.argv[1:] == ["--version"]:
    print("fake 1.0")
    sys.exit(0)
mode = os.environ.get("FAKE_MODE", "report")
open(os.environ["FAKE_ARGV_LOG"], "a").write(" ".join(sys.argv[1:]) + "\\n")
if mode == "report":
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
    d = os.path.join(".claude", "grumpy", *branch.split("/"))
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
        (run_dir,) = os.listdir(out)
        with open(os.path.join(out, run_dir, "results.json")) as f:
            results = json.load(f)
        self.assertEqual(results["host"], "codex")
        self.assertFalse(results["flags_verified"])
        self.assertEqual(len(results["runs"]), 2)
        self.assertEqual(results["runs"][0]["score"]["hits"], ["undefined-formatter"])
        self.assertAlmostEqual(
            results["summary"]["billing-planted/review"]["recall"], 1 / 7
        )
        self.assertTrue(
            os.path.exists(os.path.join(out, run_dir, "billing-planted-review-1.md"))
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
        (run_dir,) = os.listdir(out)
        with open(os.path.join(out, run_dir, "results.json")) as f:
            run = json.load(f)["runs"][0]
        self.assertEqual(run["status"], "no-report")
        with open(os.path.join(out, run_dir, "clean-refactor-review-1.log")) as f:
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
        (run_dir,) = os.listdir(out)
        with open(os.path.join(out, run_dir, "results.json")) as f:
            run = json.load(f)["runs"][0]
        self.assertEqual(run["status"], "scored-from-output")
        self.assertEqual(run["score"]["hits"], ["undefined-formatter"])

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


if __name__ == "__main__":
    unittest.main()
