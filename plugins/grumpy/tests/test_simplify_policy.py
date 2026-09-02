#!/usr/bin/env python3
"""
Stdlib tests for simplify_policy.py — the /grumpy:simplify policy engine.

Judgment is pure functions over dicts; LOC measurement runs against a real
temporary git repo so `git show`/`git diff` semantics (renames, deletions,
uncommitted work) are exercised for real.

Run: python3 plugins/grumpy/tests/test_simplify_policy.py
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
SCRIPT = os.path.join(SCRIPTS, "simplify_policy.py")
EXAMPLE = os.path.join(HERE, "fixtures", "simplify-config.example.json")
README = os.path.join(HERE, "..", "README.md")
sys.path.insert(0, SCRIPTS)

import simplify_policy as sp  # noqa: E402


def cfg(**overrides):
    """The no-file defaults, then dict overrides applied on top. Built from
    `default_config()` on purpose: reading this repo's own git root would make
    every judgment test silently follow a `.sharpen/simplify.json` committed
    here later."""
    c = sp.default_config()
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(c.get(key), dict):
            c[key].update(value)
        else:
            c[key] = value
    return c


def judge(**finding):
    return sp.judge_finding(finding, finding.pop("_cfg", None) or cfg())


def lines(n, prefix="line"):
    return "".join("%s %d\n" % (prefix, i) for i in range(n))


class TempRepo:
    """A throwaway git repo with one base commit, then working-tree edits."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="simplify-policy-")
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", self.dir] + list(args),
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def write(self, rel, text):
        full = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)

    def commit(self, msg="base"):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD").strip()

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


# ---------------------------------------------------------------- config ---


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, obj, name="simplify.json"):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as fh:
            if isinstance(obj, str):
                fh.write(obj)
            else:
                json.dump(obj, fh)
        return p

    def test_defaults_when_no_file(self):
        repo = TempRepo()
        try:
            c = sp.load_config(worktree=repo.dir)
        finally:
            repo.cleanup()
        self.assertEqual(c, sp.default_config())
        self.assertEqual(c["thresholds"]["loc_per_file"], [500, 1000, 1500])
        self.assertEqual(c["advisory"], ["halstead", "mutants"])
        self.assertIn("vendor/**", c["exclude"])
        self.assertIsNone(c["_source"])

    def test_explicit_missing_path_is_error(self):
        with self.assertRaises(sp.ConfigError):
            sp.load_config(
                worktree=HERE, explicit_path=os.path.join(self.tmp, "nope.json")
            )

    def test_file_merges_over_defaults(self):
        p = self._write({"thresholds": {"cyclomatic": 30}})
        c = sp.load_config(explicit_path=p)
        self.assertEqual(c["thresholds"]["cyclomatic"], 30)
        self.assertEqual(c["thresholds"]["cognitive"], 22)  # untouched default
        self.assertEqual(c["_source"], p)

    def test_exclude_extends_defaults(self):
        p = self._write({"exclude": ["assets/**", "vendor/**"]})
        c = sp.load_config(explicit_path=p)
        self.assertIn("assets/**", c["exclude"])
        self.assertIn("node_modules/**", c["exclude"])
        self.assertEqual(c["exclude"].count("vendor/**"), 1)
        self.assertEqual(sp.excluded_reason("assets/logo.svg", c), "glob:assets/**")
        self.assertTrue(sp.is_excluded("vendor/x.js", c))

    def test_advisory_replaces_defaults(self):
        p = self._write({"advisory": []})
        c = sp.load_config(explicit_path=p)
        self.assertEqual(c["advisory"], [])

    def test_block_on_estimate_and_test_advisory_replace_defaults(self):
        p = self._write({"block_on_estimate": ["cyclomatic"], "test_advisory": []})
        c = sp.load_config(explicit_path=p)
        self.assertEqual(c["block_on_estimate"], ["cyclomatic"])
        self.assertEqual(c["test_advisory"], [])

    def test_test_patterns_extend_defaults(self):
        p = self._write({"test_patterns": ["*_spec.rb", "Test*.java", "test_*.py"]})
        c = sp.load_config(explicit_path=p)
        self.assertEqual(c["test_patterns"], ["*_spec.rb", "Test*.java"])
        self.assertEqual(sp.file_kind("app/models/user_spec.rb", c), "test")
        self.assertEqual(sp.file_kind("app/models/user_spec.rb"), "production")
        self.assertEqual(sp.file_kind("src/TestFoo.java", c), "test")

    def test_language_override_by_extension(self):
        p = self._write(
            {
                "languages": {
                    "rs": {
                        "thresholds": {"cyclomatic": 30},
                        "tolerance": {"cyclomatic": 5},
                    }
                }
            }
        )
        c = sp.load_config(explicit_path=p)
        self.assertEqual(sp.effective(c, "cyclomatic", "src/main.rs"), (30, 5))
        self.assertEqual(sp.effective(c, "cognitive", "src/main.rs"), (22, 2))
        self.assertEqual(sp.effective(c, "cyclomatic", "src/main.py"), (22, 2))
        self.assertEqual(sp.effective(c, "cyclomatic", "src/MAIN.RS"), (30, 5))

    def test_malformed_json_is_config_error(self):
        p = self._write("{not json")
        with self.assertRaises(sp.ConfigError):
            sp.load_config(explicit_path=p)

    def test_wrong_type_is_config_error(self):
        for bad in (
            {"thresholds": [1, 2]},
            {"thresholds": {"cyclomatic": "22"}},
            {"thresholds": {"loc_per_file": [500, 400, 1500]}},
            {"thresholds": {"loc_per_file": 500}},
            {"thresholds": {"bogus": 1}},
            {"advisory": "halstead"},
            {"advisory": ["bogus"]},
            {"debt": [{"path": "x"}]},
            {"debt": [{"path": "x", "reason": "   "}]},
            {"debt": [{"path": "x", "reason": "r", "metric": "bogus"}]},
            {"languages": {"rs": {"foo": {}}}},
            {"exclude": [1]},
            {"thresholds": {"dead_code": 5}},
            {"tolerance": {"any_unknown": 3}},
            {"block_on_estimate": ["bogus"]},
            {"test_advisory": "loc_per_file"},
            {"test_patterns": [1]},
            {"debt": ["oops"]},
            {"languages": "x"},
        ):
            with self.subTest(bad=bad):
                p = self._write(bad)
                with self.assertRaises(sp.ConfigError):
                    sp.load_config(explicit_path=p)

    def test_unknown_key_warns_not_fails(self):
        p = self._write({"surprise": True})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            c = sp.load_config(explicit_path=p)
        self.assertIn("surprise", err.getvalue())
        self.assertEqual(c["thresholds"]["cyclomatic"], 22)

    def test_example_config_loads(self):
        c = sp.load_config(explicit_path=EXAMPLE)
        self.assertEqual(c["languages"]["rs"]["thresholds"]["cyclomatic"], 30)
        self.assertEqual(len(c["debt"]), 1)
        self.assertIn("assets/**", c["exclude"])

    def test_readme_embeds_example_config_verbatim(self):
        with open(EXAMPLE) as fh:
            example = fh.read().strip()
        with open(README) as fh:
            readme = fh.read()
        self.assertIn(
            example, readme, "README's config example drifted from the fixture"
        )


# ------------------------------------------------------- classification ---


class ClassificationTests(unittest.TestCase):
    def test_is_excluded_glob_default_vendor(self):
        self.assertEqual(sp.excluded_reason("vendor/lib.js", cfg()), "glob:vendor/**")

    def test_is_excluded_nested_vendor(self):
        self.assertTrue(sp.is_excluded("pkg/a/vendor/lib.js", cfg()))
        self.assertTrue(sp.is_excluded("a/b/gen_pb2.py", cfg()))
        self.assertTrue(sp.is_excluded("gen_pb2.py", cfg()))

    def test_not_excluded_plain_source(self):
        self.assertIsNone(sp.excluded_reason("src/main.rs", cfg(), "fn main() {}\n"))

    def test_is_excluded_generated_header(self):
        text = "// @generated by protoc\nfn x() {}\n"
        self.assertEqual(
            sp.excluded_reason("src/gen.rs", cfg(), text), "generated-header"
        )
        deep = lines(10) + "// DO NOT EDIT\n"
        self.assertIsNone(sp.excluded_reason("src/late.rs", cfg(), deep))

    def test_file_kind_test_dirs_and_names(self):
        for p in (
            "tests/test_x.py",
            "src/__tests__/a.ts",
            "a/spec/b.rb",
            "x_test.go",
            "foo.test.ts",
            "foo.spec.js",
            "conftest.py",
            "pkg/testdata/big.json",
        ):
            self.assertEqual(sp.file_kind(p), "test", p)
        for p in (
            "src/main.rs",
            "src/testing_utils.py",
            "contest.py",
            "src/spec_parser.go",
        ):
            self.assertEqual(sp.file_kind(p), "production", p)

    def test_debt_record_glob_and_metric(self):
        c = cfg(
            debt=[
                {"path": "src/main.rs", "metric": "loc_per_file", "reason": "r1"},
                {"path": "legacy/**", "reason": "r2"},
            ]
        )
        self.assertEqual(
            sp.debt_record(c, "src/main.rs", "loc_per_file")["reason"], "r1"
        )
        self.assertIsNone(sp.debt_record(c, "src/main.rs", "cyclomatic"))
        self.assertEqual(
            sp.debt_record(c, "legacy/a/b.py", "cyclomatic")["reason"], "r2"
        )
        self.assertIsNone(sp.debt_record(c, "src/other.rs", "loc_per_file"))

    def test_debt_record_matches_exactly_not_by_suffix(self):
        c = cfg(debt=[{"path": "main.rs", "reason": "r"}])
        self.assertIsNone(sp.debt_record(c, "src/main.rs", "loc_per_file"))
        self.assertIsNotNone(sp.debt_record(c, "main.rs", "loc_per_file"))

    def test_exclude_negation_wins(self):
        c = cfg()
        c["exclude"] = c["exclude"] + ["!apps/foo/build/**"]
        self.assertIsNone(sp.excluded_reason("apps/foo/build/index.ts", c))
        self.assertEqual(
            sp.excluded_reason("apps/bar/build/index.ts", c), "glob:build/**"
        )


# ------------------------------------------------------- value metrics ---


class ValueMetricTests(unittest.TestCase):
    def cyc(self, base, head, confidence="measured:radon", **kw):
        return judge(
            metric="cyclomatic",
            file="src/a.py",
            symbol="f",
            base=base,
            head=head,
            confidence=confidence,
            **kw,
        )

    def test_compliant_under_threshold(self):
        f = self.cyc(30, 10)
        self.assertEqual(
            (f["status"], f["blocking"], f["severity"]), ("compliant", False, "INFO")
        )

    def test_new_symbol_over_threshold_blocks(self):
        f = self.cyc(None, 25)
        self.assertEqual(
            (f["status"], f["blocking"], f["severity"]), ("new", True, "CRIT")
        )

    def test_existing_pushed_over_is_new(self):
        f = self.cyc(20, 25)
        self.assertEqual((f["status"], f["blocking"]), ("new", True))

    def test_improved(self):
        f = self.cyc(40, 39)
        self.assertEqual(
            (f["status"], f["blocking"], f["severity"]), ("improved", False, "NOTE")
        )

    def test_held_within_tolerance(self):
        self.assertEqual(self.cyc(40, 40)["status"], "held")
        self.assertEqual(self.cyc(40, 42)["status"], "held")

    def test_regressed_beyond_tolerance_blocks(self):
        f = self.cyc(40, 43)
        self.assertEqual(
            (f["status"], f["blocking"], f["severity"]), ("regressed", True, "CRIT")
        )

    def test_estimated_complexity_never_blocks(self):
        f = self.cyc(40, 43, confidence="estimated")
        self.assertEqual(
            (f["status"], f["blocking"], f["severity"]), ("regressed", False, "WARN")
        )
        self.assertEqual(f["blocking_reason"], "estimated, not measured")
        self.assertIn("does not block: estimated, not measured", f["note"])

    def test_blocking_reason_is_null_when_blocking_or_clean(self):
        self.assertIsNone(self.cyc(40, 43)["blocking_reason"])
        self.assertIsNone(self.cyc(40, 41)["blocking_reason"])
        self.assertIsNone(self.cyc(30, 10)["blocking_reason"])

    def test_blocking_reason_lists_every_suppression(self):
        f = judge(
            metric="halstead",
            file="tests/test_a.py",
            symbol="t",
            base=None,
            head=90,
            confidence="estimated",
        )
        self.assertEqual(
            f["blocking_reason"], "advisory metric; test file; estimated, not measured"
        )

    def test_block_on_estimate_is_configurable(self):
        c = cfg(block_on_estimate=["cyclomatic"])
        self.assertTrue(self.cyc(40, 43, confidence="estimated", _cfg=c)["blocking"])
        c = cfg(block_on_estimate=[])
        f = judge(
            metric="any_unknown",
            file="src/a.ts",
            line=1,
            introduced=True,
            confidence="estimated",
            _cfg=c,
        )
        self.assertFalse(f["blocking"])

    def test_unmeasured_never_blocks(self):
        f = self.cyc(None, 99, confidence="unmeasured")
        self.assertEqual((f["status"], f["blocking"]), ("new", False))

    def test_bad_confidence_is_error(self):
        with self.assertRaises(sp.FindingError):
            self.cyc(None, 99, confidence="vibes")

    def test_advisory_metric_warns_not_blocks(self):
        f = judge(
            metric="halstead",
            file="a.py",
            symbol="f",
            base=None,
            head=90,
            confidence="measured:radon",
        )
        self.assertEqual(
            (f["status"], f["blocking"], f["severity"]), ("new", False, "WARN")
        )
        self.assertIn("advisory", f["note"])

    def test_test_file_never_blocks(self):
        f = judge(
            metric="cyclomatic",
            file="tests/test_a.py",
            symbol="t",
            base=None,
            head=30,
            confidence="measured:radon",
        )
        self.assertEqual(
            (f["status"], f["blocking"], f["kind"]), ("new", False, "test")
        )

    def test_explicit_kind_wins(self):
        f = judge(
            metric="cyclomatic",
            file="src/a.py",
            symbol="t",
            base=None,
            head=30,
            confidence="measured:radon",
            kind="test",
        )
        self.assertFalse(f["blocking"])

    def test_debt_record_turns_block_into_excepted(self):
        c = cfg(debt=[{"path": "src/*.py", "reason": "tracked in #9"}])
        f = self.cyc(40, 50, _cfg=c)
        self.assertEqual(
            (f["status"], f["blocking"], f["severity"]), ("excepted", False, "WARN")
        )
        self.assertEqual(f["debt"]["reason"], "tracked in #9")
        self.assertEqual(f["blocking_reason"], "documented debt: tracked in #9")
        self.assertIn("tracked in #9", f["note"])

    def test_debt_record_does_not_touch_non_blocking(self):
        c = cfg(debt=[{"path": "src/*.py", "reason": "r"}])
        self.assertEqual(self.cyc(40, 41, _cfg=c)["status"], "held")
        self.assertEqual(
            self.cyc(40, 43, confidence="estimated", _cfg=c)["status"], "regressed"
        )

    def test_coverage_direction(self):
        def cov(b, h, c="measured:coverage"):
            return judge(
                metric="coverage",
                file="src/a.py",
                symbol="f",
                base=b,
                head=h,
                confidence=c,
            )

        self.assertEqual(cov(100, 100)["status"], "compliant")
        # Fully covered at base, gap at head: the violation did not exist
        # before this diff, so it is `new`, not `regressed`. Both block.
        self.assertEqual(
            (cov(100, 95)["status"], cov(100, 95)["blocking"]), ("new", True)
        )
        self.assertEqual(
            (cov(None, 80)["status"], cov(None, 80)["blocking"]), ("new", True)
        )
        self.assertEqual(cov(70, 80)["status"], "improved")
        self.assertEqual(cov(70, 70)["status"], "held")
        self.assertEqual(cov(70, 69)["status"], "regressed")
        self.assertFalse(cov(None, 80, "estimated")["blocking"])

    def test_language_override_makes_head_compliant(self):
        c = cfg(languages={"rs": {"thresholds": {"cyclomatic": 30}}})
        f = judge(
            metric="cyclomatic",
            file="src/main.rs",
            symbol="f",
            base=None,
            head=25,
            confidence="measured:x",
            _cfg=c,
        )
        self.assertEqual((f["status"], f["threshold"]), ("compliant", 30))

    def test_mutants_zero_is_compliant_and_positive_is_advisory(self):
        def mut(b, h):
            return judge(
                metric="mutants",
                file="a.py",
                symbol="f",
                base=b,
                head=h,
                confidence="measured:mutmut",
            )

        self.assertEqual(mut(0, 0)["status"], "compliant")
        self.assertEqual(mut(None, 0)["status"], "compliant")
        f = mut(None, 3)
        self.assertEqual(
            (f["status"], f["blocking"], f["blocking_reason"]),
            ("new", False, "advisory metric"),
        )
        self.assertTrue(
            mut(
                None,
                3,
            )["severity"]
            == "WARN"
        )
        c = cfg(advisory=[])
        self.assertTrue(
            judge(
                metric="mutants",
                file="a.py",
                symbol="f",
                base=None,
                head=1,
                confidence="measured:mutmut",
                _cfg=c,
            )["blocking"]
        )

    def test_bool_values_are_rejected(self):
        with self.assertRaises(sp.FindingError):
            self.cyc(None, True)
        with self.assertRaises(sp.FindingError):
            self.cyc(False, 30)

    def test_not_applicable_is_compliant_not_debt(self):
        f = judge(
            metric="any_unknown", file="-", applicable=False, note="not applicable: C"
        )
        self.assertEqual(
            (f["status"], f["blocking"], f["severity"]), ("compliant", False, "INFO")
        )
        self.assertIn("not applicable: C", f["note"])
        r = sp.judge_all(
            [{"metric": "any_unknown", "file": "-", "applicable": False}], cfg()
        )
        self.assertEqual(r["summary"]["verdict"], "compliant")

    def test_crap_tolerance(self):
        def crap(b, h):
            return judge(
                metric="crap",
                file="a.py",
                symbol="f",
                base=b,
                head=h,
                confidence="measured:x",
            )["status"]

        self.assertEqual(crap(60, 65), "held")
        self.assertEqual(crap(60, 66), "regressed")

    def test_missing_head_is_error(self):
        with self.assertRaises(sp.FindingError):
            judge(metric="cyclomatic", file="a.py", base=1, confidence="measured:x")

    def test_unknown_metric_is_error(self):
        with self.assertRaises(sp.FindingError):
            judge(metric="vibes", file="a.py", head=1)

    def test_missing_file_is_error(self):
        with self.assertRaises(sp.FindingError):
            judge(metric="cyclomatic", head=1)


# ---------------------------------------------------------- loc metric ---


class LocMetricTests(unittest.TestCase):
    def loc(self, base, head, path="src/main.rs", **kw):
        return judge(
            metric="loc_per_file",
            file=path,
            base=base,
            head=head,
            confidence=sp.LOC_CONFIDENCE,
            **kw,
        )

    def test_new_file_over_hard_tier_blocks(self):
        f = self.loc(None, 1600)
        self.assertEqual((f["status"], f["blocking"], f["tier"]), ("new", True, "hard"))

    def test_tier_boundaries_are_inclusive(self):
        self.assertTrue(self.loc(None, 1500)["blocking"])  # == strong blocks
        self.assertFalse(self.loc(None, 1499)["blocking"])
        self.assertTrue(self.loc(900, 1000)["blocking"])  # regressed, == warn blocks
        self.assertFalse(self.loc(900, 999)["blocking"])
        self.assertEqual(self.loc(None, 500)["status"], "new")  # == healthy is over
        self.assertEqual(self.loc(None, 499)["status"], "compliant")

    def test_new_file_strong_warn_tier_does_not_block(self):
        f = self.loc(None, 1200)
        self.assertEqual(
            (f["status"], f["blocking"], f["severity"], f["tier"]),
            ("new", False, "WARN", "strong-warn"),
        )
        self.assertEqual(
            f["blocking_reason"], "below the hard-block tier for a new file"
        )

    def test_new_file_warn_tier_does_not_block(self):
        f = self.loc(None, 600)
        self.assertEqual(
            (f["status"], f["blocking"], f["tier"]), ("new", False, "warn")
        )

    def test_new_small_file_compliant(self):
        f = self.loc(None, 252, path="src/routing.rs")
        self.assertEqual((f["status"], f["severity"]), ("compliant", "INFO"))

    def test_crossing_healthy_is_new_non_blocking(self):
        self.assertEqual(
            (self.loc(490, 520)["status"], self.loc(490, 520)["blocking"]),
            ("new", False),
        )

    def test_crossing_into_hard_tier_blocks(self):
        self.assertTrue(self.loc(490, 1600)["blocking"])

    def test_legacy_reduced_is_improved(self):
        f = self.loc(8471, 8243)
        self.assertEqual(
            (f["status"], f["blocking"], f["severity"]), ("improved", False, "NOTE")
        )
        self.assertIn("(-228)", f["note"])

    def test_legacy_within_tolerance_is_held(self):
        self.assertEqual(self.loc(8471, 8471)["status"], "held")
        self.assertEqual(self.loc(8471, 8481)["status"], "held")

    def test_legacy_growth_at_warn_tier_or_above_blocks(self):
        f = self.loc(8471, 8890)
        self.assertEqual(
            (f["status"], f["blocking"], f["severity"]), ("regressed", True, "CRIT")
        )
        self.assertEqual(self.loc(989, 1000)["status"], "regressed")
        self.assertTrue(self.loc(989, 1000)["blocking"])

    def test_legacy_growth_below_warn_tier_warns_only(self):
        f = self.loc(600, 650)
        self.assertEqual(
            (f["status"], f["blocking"], f["severity"]), ("regressed", False, "WARN")
        )
        self.assertIn("stated reason", f["note"])

    def test_legacy_growth_with_debt_record_is_excepted(self):
        c = cfg(
            debt=[
                {"path": "src/main.rs", "metric": "loc_per_file", "reason": "see #12"}
            ]
        )
        f = self.loc(8471, 8890, _cfg=c)
        self.assertEqual((f["status"], f["blocking"]), ("excepted", False))
        self.assertIn("see #12", f["note"])

    def test_test_file_size_never_blocks(self):
        f = self.loc(None, 1600, path="tests/test_big.py")
        self.assertEqual((f["status"], f["blocking"]), ("new", False))

    def test_language_tiers_override(self):
        c = cfg(languages={"sql": {"thresholds": {"loc_per_file": [2000, 3000, 4000]}}})
        self.assertEqual(
            self.loc(None, 1600, path="db/schema.sql", _cfg=c)["status"], "compliant"
        )


# --------------------------------------------------------- count metrics ---


class CountMetricTests(unittest.TestCase):
    def test_introduced_any_blocks_even_when_estimated(self):
        f = judge(
            metric="any_unknown",
            file="src/a.ts",
            line=12,
            introduced=True,
            confidence="estimated",
        )
        self.assertEqual(
            (f["status"], f["blocking"], f["severity"]), ("new", True, "CRIT")
        )

    def test_default_confidence_for_count_is_estimated(self):
        f = judge(metric="dead_code", file="src/a.ts", line=3)
        self.assertEqual(
            (f["confidence"], f["status"], f["blocking"]), ("estimated", "new", True)
        )

    def test_pre_existing_is_held(self):
        f = judge(metric="any_unknown", file="src/a.ts", line=12, introduced=False)
        self.assertEqual(
            (f["status"], f["blocking"], f["severity"]), ("held", False, "NOTE")
        )

    def test_removed_is_improved(self):
        f = judge(metric="redundant_code", file="src/a.ts", line=12, removed=True)
        self.assertEqual(f["status"], "improved")

    def test_redundant_estimated_does_not_block(self):
        f = judge(
            metric="redundant_code",
            file="src/a.ts",
            line=12,
            introduced=True,
            confidence="estimated",
        )
        self.assertEqual(
            (f["status"], f["blocking"], f["severity"]), ("new", False, "WARN")
        )

    def test_redundant_measured_blocks(self):
        f = judge(
            metric="redundant_code",
            file="src/a.ts",
            line=12,
            introduced=True,
            confidence="measured:jscpd",
        )
        self.assertTrue(f["blocking"])

    def test_pre_existing_in_test_file_is_held_not_new(self):
        f = judge(metric="dead_code", file="tests/x_test.go", line=1, introduced=False)
        self.assertEqual(f["status"], "held")

    def test_introduced_any_in_test_file_still_blocks(self):
        f = judge(
            metric="any_unknown", file="tests/helpers.test.ts", line=1, introduced=True
        )
        self.assertEqual((f["kind"], f["status"], f["blocking"]), ("test", "new", True))

    def test_test_advisory_is_configurable(self):
        c = cfg(test_advisory=["loc_per_file", "cyclomatic", "any_unknown"])
        f = judge(
            metric="any_unknown",
            file="tests/helpers.test.ts",
            line=1,
            introduced=True,
            _cfg=c,
        )
        self.assertEqual(f["blocking_reason"], "test file")
        c = cfg(test_advisory=[])
        f = judge(
            metric="cyclomatic",
            file="tests/test_a.py",
            symbol="t",
            base=None,
            head=30,
            confidence="measured:radon",
            _cfg=c,
        )
        self.assertTrue(f["blocking"])

    def test_count_metrics_carry_no_threshold(self):
        f = judge(metric="dead_code", file="src/a.ts", line=3)
        self.assertNotIn("threshold", f)
        self.assertNotIn("tolerance", f)


# -------------------------------------------------------------- judge_all ---


class JudgeAllTests(unittest.TestCase):
    def test_compliant_verdict(self):
        r = sp.judge_all(
            [
                {
                    "metric": "cyclomatic",
                    "file": "a.py",
                    "head": 3,
                    "confidence": "measured:x",
                }
            ],
            cfg(),
        )
        self.assertEqual(r["summary"]["verdict"], "compliant")
        self.assertEqual(r["summary"]["debt"], [])
        self.assertEqual(r["summary"]["counts"]["compliant"], 1)

    def test_passes_with_debt_verdict(self):
        r = sp.judge_all(
            [
                {
                    "metric": "loc_per_file",
                    "file": "src/main.rs",
                    "base": 8471,
                    "head": 8243,
                    "confidence": sp.LOC_CONFIDENCE,
                },
                {
                    "metric": "any_unknown",
                    "file": "src/a.ts",
                    "line": 4,
                    "introduced": False,
                },
            ],
            cfg(),
        )
        self.assertEqual(r["summary"]["verdict"], "passes-with-debt")
        self.assertEqual(
            r["summary"]["debt"],
            [
                "src/main.rs loc_per_file 8471→8243 improved",
                "src/a.ts any_unknown line 4 held",
            ],
        )

    def test_blocking_beats_debt(self):
        r = sp.judge_all(
            [
                {
                    "metric": "loc_per_file",
                    "file": "src/main.rs",
                    "base": 8471,
                    "head": 8243,
                    "confidence": sp.LOC_CONFIDENCE,
                },
                {
                    "metric": "cyclomatic",
                    "file": "src/a.py",
                    "symbol": "f",
                    "base": None,
                    "head": 30,
                    "confidence": "measured:radon",
                },
            ],
            cfg(),
        )
        self.assertEqual(r["summary"]["verdict"], "blocked")
        self.assertEqual(r["summary"]["blocking"], 1)
        self.assertEqual(len(r["summary"]["debt"]), 1)

    def test_empty_is_unmeasured_not_compliant(self):
        r = sp.judge_all([], cfg())
        self.assertEqual(r["summary"]["verdict"], "unmeasured")
        self.assertEqual(
            r["summary"]["confidence"], {"measured": 0, "estimated": 0, "unmeasured": 0}
        )

    def test_summary_carries_provenance(self):
        c = cfg()
        c["_source"] = "/repo/.sharpen/simplify.json"
        r = sp.judge_all(
            [
                {
                    "metric": "cyclomatic",
                    "file": "a.py",
                    "head": 3,
                    "confidence": "measured:x",
                }
            ],
            c,
            base="abc123",
        )
        self.assertEqual(r["summary"]["config_source"], "/repo/.sharpen/simplify.json")
        self.assertEqual(r["summary"]["base"], "abc123")
        self.assertEqual(r["summary"]["excluded"], [])
        self.assertRegex(r["summary"]["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(r["summary"]["confidence"]["measured"], 1)
        self.assertEqual(
            sp.judge_all([], cfg())["summary"]["config_source"], "defaults"
        )

    def test_agent_finding_on_excluded_path_is_not_judged(self):
        r = sp.judge_all(
            [
                {
                    "metric": "cyclomatic",
                    "file": "vendor/lib.go",
                    "symbol": "f",
                    "base": None,
                    "head": 90,
                    "confidence": "measured:gocyclo",
                },
                {"metric": "any_unknown", "file": ".claude/scratch/x.ts", "line": 1},
                {
                    "metric": "cyclomatic",
                    "file": "src/a.py",
                    "symbol": "f",
                    "base": None,
                    "head": 30,
                    "confidence": "measured:radon",
                },
            ],
            cfg(),
        )
        self.assertEqual([f["file"] for f in r["findings"]], ["src/a.py"])
        self.assertEqual(
            [(e["file"], e["reason"], e["metric"]) for e in r["summary"]["excluded"]],
            [
                ("vendor/lib.go", "glob:vendor/**", "cyclomatic"),
                (".claude/scratch/x.ts", "glob:.claude/**", "any_unknown"),
            ],
        )
        self.assertEqual(r["summary"]["verdict"], "blocked")

    def test_count_metric_note_carries_line(self):
        f = judge(metric="dead_code", file="src/a.ts", line=44)
        self.assertTrue(f["note"].startswith("src/a.ts:44:"), f["note"])

    def test_error_names_finding_index(self):
        with self.assertRaises(sp.FindingError) as ctx:
            sp.judge_all(
                [
                    {"metric": "cyclomatic", "file": "a", "head": 1},
                    {"metric": "nope", "file": "a", "head": 1},
                ],
                cfg(),
            )
        self.assertIn("finding 2", str(ctx.exception))


# ------------------------------------------------------- loc measurement ---


class LocMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()
        self.repo.write("src/main.rs", lines(8471, "main"))
        self.repo.write("src/lib.rs", lines(10, "lib"))
        self.base = self.repo.commit()
        self.cfg = sp.load_config(worktree=self.repo.dir)

    def tearDown(self):
        self.repo.cleanup()

    def report(self):
        return sp.loc_report(self.repo.dir, self.base, self.cfg)

    def by_file(self, report):
        return {f["file"]: f for f in report["findings"]}

    def test_archivaldo_extraction_passes_with_debt(self):
        self.repo.write("src/main.rs", lines(8243, "main"))
        self.repo.write("src/routing.rs", lines(252, "routing"))
        r = self.report()
        files = self.by_file(r)
        self.assertEqual(files["src/main.rs"]["status"], "improved")
        self.assertEqual(
            (files["src/main.rs"]["base"], files["src/main.rs"]["head"]), (8471, 8243)
        )
        self.assertEqual(files["src/routing.rs"]["status"], "compliant")
        self.assertIsNone(files["src/routing.rs"]["base"])
        self.assertEqual(r["summary"]["verdict"], "passes-with-debt")
        self.assertEqual(r["summary"]["base"], self.base)

    def test_archivaldo_growth_is_blocked(self):
        self.repo.write("src/main.rs", lines(8890, "main"))
        r = self.report()
        self.assertEqual(r["summary"]["verdict"], "blocked")
        self.assertTrue(self.by_file(r)["src/main.rs"]["blocking"])

    def test_growth_with_debt_record_is_excepted(self):
        self.repo.write(
            ".sharpen/simplify.json",
            json.dumps({"debt": [{"path": "src/main.rs", "reason": "tracked"}]}),
        )
        self.repo.write("src/main.rs", lines(8890, "main"))
        c = sp.load_config(worktree=self.repo.dir)
        r = sp.loc_report(self.repo.dir, self.base, c)
        self.assertEqual(self.by_file(r)["src/main.rs"]["status"], "excepted")
        self.assertEqual(r["summary"]["verdict"], "passes-with-debt")

    def test_vendor_and_generated_are_excluded(self):
        self.repo.write("vendor/big.js", lines(2000))
        self.repo.write("src/gen.rs", "// @generated\n" + lines(2000))
        r = self.report()
        self.assertEqual(r["findings"], [])
        self.assertEqual(
            sorted((e["file"], e["reason"]) for e in r["summary"]["excluded"]),
            [("src/gen.rs", "generated-header"), ("vendor/big.js", "glob:vendor/**")],
        )
        self.assertEqual(r["summary"]["verdict"], "unmeasured")  # nothing in scope

    def test_agent_scratch_under_dot_claude_is_not_source(self):
        self.repo.write(".claude/grumpy/main/scratch.rs", lines(3000))
        r = self.report()
        self.assertEqual(r["findings"], [])
        self.assertEqual(r["summary"]["excluded"][0]["reason"], "glob:.claude/**")

    def test_big_test_file_does_not_block(self):
        self.repo.write("tests/test_big.py", lines(1600))
        f = self.by_file(self.report())["tests/test_big.py"]
        self.assertEqual(
            (f["status"], f["blocking"], f["kind"]), ("new", False, "test")
        )

    def test_deleted_file_has_no_finding(self):
        os.remove(os.path.join(self.repo.dir, "src/lib.rs"))
        self.assertNotIn("src/lib.rs", self.by_file(self.report()))

    def test_rename_reads_base_under_old_name(self):
        self.repo.git("mv", "src/main.rs", "src/app.rs")
        self.repo.write("src/app.rs", lines(8400, "main"))
        files = self.by_file(self.report())
        self.assertNotIn("src/main.rs", files)
        self.assertEqual(
            (files["src/app.rs"]["base"], files["src/app.rs"]["status"]),
            (8471, "improved"),
        )

    def test_committed_only_diff_is_measured(self):
        self.repo.write("src/main.rs", lines(8243, "main"))
        self.repo.commit("shrink")
        self.assertEqual(
            self.by_file(self.report())["src/main.rs"]["status"], "improved"
        )

    def test_binary_file_is_skipped(self):
        with open(os.path.join(self.repo.dir, "src", "blob.bin"), "wb") as fh:
            fh.write(b"\x00\x01" * 4000)
        r = self.report()
        self.assertNotIn("src/blob.bin", self.by_file(r))
        self.assertIn(
            {"file": "src/blob.bin", "reason": "binary"}, r["summary"]["excluded"]
        )

    def test_unchanged_files_are_not_reported(self):
        self.repo.write("src/lib.rs", lines(11, "lib"))
        self.assertEqual(list(self.by_file(self.report())), ["src/lib.rs"])

    def test_subdirectory_worktree_still_reads_base(self):
        self.repo.write("src/main.rs", lines(8243, "main"))
        sub = os.path.join(self.repo.dir, "src")
        r = sp.loc_report(sub, self.base, sp.load_config(worktree=sub))
        f = self.by_file(r)["src/main.rs"]
        self.assertEqual((f["base"], f["head"], f["status"]), (8471, 8243, "improved"))

    def test_non_ascii_path_is_measured(self):
        self.repo.write("src/café.rs", lines(1600, "x"))
        r = self.report()
        self.assertEqual(self.by_file(r)["src/café.rs"]["status"], "new")
        self.assertEqual(r["summary"]["verdict"], "blocked")

    def test_base_equal_to_head_with_untracked_only(self):
        self.repo.write("src/routing.rs", lines(252))
        head = self.repo.git("rev-parse", "HEAD").strip()
        r = sp.loc_report(self.repo.dir, head, self.cfg)
        self.assertEqual(list(self.by_file(r)), ["src/routing.rs"])

    def test_gitignored_untracked_file_is_not_measured(self):
        self.repo.write(".gitignore", "scratch/\n")
        self.repo.commit("ignore")
        self.repo.write("scratch/huge.py", lines(3000))
        self.assertNotIn("scratch/huge.py", self.by_file(self.report()))

    def test_pure_rename_is_held(self):
        self.repo.git("mv", "src/main.rs", "src/app.rs")
        f = self.by_file(self.report())["src/app.rs"]
        self.assertEqual((f["base"], f["head"], f["status"]), (8471, 8471, "held"))

    def test_unreadable_path_is_listed_as_excluded(self):
        os.symlink(
            "/nonexistent/target", os.path.join(self.repo.dir, "src", "dangling.rs")
        )
        r = self.report()
        self.assertNotIn("src/dangling.rs", self.by_file(r))
        self.assertIn(
            {"file": "src/dangling.rs", "reason": "unreadable"},
            r["summary"]["excluded"],
        )

    def test_bad_base_is_not_mistaken_for_new_files(self):
        self.assertIsNone(sp.base_text(self.repo.dir, self.base, "src/nope.rs"))
        with self.assertRaises(RuntimeError):
            sp.loc_report(self.repo.dir, "0" * 40, self.cfg)
        with self.assertRaises(RuntimeError):
            sp.loc_report(self.repo.dir, "not-a-ref", self.cfg)

    def test_count_lines_semantics(self):
        self.assertEqual(sp.count_lines(""), 0)
        self.assertEqual(sp.count_lines("a\n"), 1)
        self.assertEqual(sp.count_lines("a\nb"), 2)
        self.assertIsNone(sp.count_lines("a\x00b"))
        self.assertIsNone(sp.count_lines(None))


# --------------------------------------------------------------------- CLI ---


class CliTests(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()
        self.repo.write("src/main.rs", lines(8471))
        self.base = self.repo.commit()

    def tearDown(self):
        self.repo.cleanup()

    def run_cli(self, *args, stdin=""):
        return subprocess.run(
            [sys.executable, SCRIPT] + list(args),
            input=stdin,
            capture_output=True,
            text=True,
        )

    def test_loc_prints_json(self):
        self.repo.write("src/main.rs", lines(8243))
        p = self.run_cli("loc", "--worktree", self.repo.dir, "--base", self.base)
        self.assertEqual(p.returncode, 0, p.stderr)
        d = json.loads(p.stdout)
        self.assertEqual(d["summary"]["verdict"], "passes-with-debt")

    def test_loc_blocked_still_exits_zero(self):
        self.repo.write("src/main.rs", lines(9000))
        p = self.run_cli("loc", "--worktree", self.repo.dir, "--base", self.base)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout)["summary"]["verdict"], "blocked")

    def test_judge_json_lines(self):
        stdin = "\n".join(
            [
                json.dumps(
                    {
                        "metric": "cyclomatic",
                        "file": "a.py",
                        "symbol": "f",
                        "base": 40,
                        "head": 41,
                        "confidence": "measured:radon",
                    }
                ),
                "",
                json.dumps(
                    {
                        "metric": "any_unknown",
                        "file": "a.ts",
                        "line": 1,
                        "introduced": True,
                    }
                ),
            ]
        )
        p = self.run_cli("judge", "--worktree", self.repo.dir, stdin=stdin)
        self.assertEqual(p.returncode, 0, p.stderr)
        d = json.loads(p.stdout)
        self.assertEqual([f["status"] for f in d["findings"]], ["held", "new"])
        self.assertEqual(d["summary"]["verdict"], "blocked")

    def test_judge_json_array(self):
        stdin = json.dumps(
            [
                {
                    "metric": "cyclomatic",
                    "file": "a.py",
                    "head": 1,
                    "confidence": "measured:x",
                }
            ]
        )
        p = self.run_cli("judge", "--worktree", self.repo.dir, stdin=stdin)
        self.assertEqual(json.loads(p.stdout)["summary"]["verdict"], "compliant")

    def test_judge_empty_stdin_is_unmeasured(self):
        p = self.run_cli("judge", "--worktree", self.repo.dir, stdin="")
        self.assertEqual(json.loads(p.stdout)["summary"]["verdict"], "unmeasured")

    def test_judge_loc_folds_in_loc_report(self):
        self.repo.write("src/main.rs", lines(8243))
        self.repo.write("vendor/big.js", lines(2000))
        # Outside the repo: an untracked loc.json inside it would itself be
        # measured as a new file.
        loc_path = os.path.join(tempfile.mkdtemp(), "loc.json")
        with open(loc_path, "w") as fh:
            fh.write(
                self.run_cli(
                    "loc", "--worktree", self.repo.dir, "--base", self.base
                ).stdout
            )
        stdin = json.dumps(
            {
                "metric": "cyclomatic",
                "file": "src/main.rs",
                "symbol": "f",
                "base": 40,
                "head": 41,
                "confidence": "measured:x",
            }
        )
        p = self.run_cli(
            "judge", "--worktree", self.repo.dir, "--loc", loc_path, stdin=stdin
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        d = json.loads(p.stdout)
        self.assertEqual(
            sorted((f["metric"], f["status"]) for f in d["findings"]),
            [("cyclomatic", "held"), ("loc_per_file", "improved")],
        )
        self.assertEqual(d["summary"]["base"], self.base)
        self.assertEqual(
            d["summary"]["excluded"],
            [{"file": "vendor/big.js", "reason": "glob:vendor/**"}],
        )
        self.assertEqual(d["summary"]["verdict"], "passes-with-debt")

    def test_judge_loc_bad_file_exits_2(self):
        p = self.run_cli(
            "judge", "--worktree", self.repo.dir, "--loc", "/nonexistent.json", stdin=""
        )
        self.assertEqual(p.returncode, 2)
        self.assertIn("--loc", p.stderr)

    def test_judge_bad_metric_exits_2_with_line(self):
        stdin = (
            json.dumps({"metric": "cyclomatic", "file": "a", "head": 1})
            + "\n"
            + json.dumps({"metric": "nope", "file": "a", "head": 1})
        )
        p = self.run_cli("judge", "--worktree", self.repo.dir, stdin=stdin)
        self.assertEqual(p.returncode, 2)
        self.assertIn("finding 2", p.stderr)
        self.assertIn("nope", p.stderr)

    def test_judge_non_json_line_exits_2(self):
        p = self.run_cli("judge", "--worktree", self.repo.dir, stdin="CRIT|a.py|x")
        self.assertEqual(p.returncode, 2)
        self.assertIn("line 1", p.stderr)

    def test_config_prints_effective_and_source(self):
        p = self.run_cli("config", "--worktree", self.repo.dir)
        d = json.loads(p.stdout)
        self.assertEqual(d["source"], "defaults")
        self.assertEqual(d["thresholds"]["cyclomatic"], 22)
        self.assertNotIn("_source", d)

    def test_config_override_path(self):
        p = self.run_cli("config", "--worktree", self.repo.dir, "--config", EXAMPLE)
        d = json.loads(p.stdout)
        self.assertEqual(d["source"], EXAMPLE)
        self.assertEqual(d["languages"]["go"]["thresholds"]["cyclomatic"], 30)

    def test_bad_config_exits_2(self):
        self.repo.write(".sharpen/simplify.json", "{oops")
        p = self.run_cli("config", "--worktree", self.repo.dir)
        self.assertEqual(p.returncode, 2)
        self.assertIn("simplify.json", p.stderr)

    def test_missing_git_binary_exits_2(self):
        env = dict(os.environ, PATH="/nonexistent")
        p = subprocess.run(
            [sys.executable, SCRIPT, "config", "--worktree", self.repo.dir],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(p.returncode, 2)
        self.assertNotIn("Traceback", p.stderr)

    def test_loc_bad_base_exits_2(self):
        p = self.run_cli("loc", "--worktree", self.repo.dir, "--base", "deadbeef")
        self.assertEqual(p.returncode, 2)


if __name__ == "__main__":
    unittest.main()
