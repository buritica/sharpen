#!/usr/bin/env python3
"""oneshot.py: grumpy's review with no agent, against a fake chat server.

The extraction tests read the real commands/review.md. They are the drift
guard: if review.md changes in a way this path can no longer build its
prompts from, they fail here instead of the one-shot path quietly sending
half a prompt.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, "..", "scripts"))
SCRIPT = os.path.join(SCRIPTS, "oneshot.py")
REVIEW_MD = os.path.normpath(os.path.join(HERE, "..", "commands", "review.md"))
MODELS = os.path.normpath(os.path.join(HERE, "..", "models.yaml"))
sys.path.insert(0, SCRIPTS)

import oneshot  # noqa: E402

BRANCH = "feature/x"
KEY = "sk-test-0123456789abcdef"
REPORT = """# Code Review: a thing

_Model: whatever the model claims_

_Fine._

## Critical Issues 🚨

- [errors]: swallows the error [app.py:2]

## Serious Concerns ⚠️

## Questionable Decisions 🤔

## Simplify This ✂️

## Strengths

## The Uncomfortable Questions

## Verdict

Fix and reship.
"""


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class ExtractionTest(unittest.TestCase):
    def test_real_review_md_extracts(self):
        parts = oneshot.load_command("review")
        self.assertIn("grumpy principal engineer", parts["persona"])
        self.assertEqual(
            [n for n, _ in parts["aspects"]],
            ["code", "errors", "tests", "types", "comments", "simplify", "docs"],
        )
        for slot in ("[ASPECT]", "[DIFF_BASE]", "[DIFF_CONTENT]"):
            self.assertIn(slot, parts["pass_template"])
        self.assertIn("outside-in", parts["audit"])
        self.assertIn("## Verdict", parts["step4"])
        # Nothing from the steps this script does itself leaks into a prompt.
        for step in ("Step 5", "Step 6", "Step 7", "receipt.py"):
            self.assertNotIn(step, parts["step4"] + parts["audit"])

    def test_severities_are_grumpys(self):
        # The pass severities come from review.md; the report sections they
        # land in are the template's own headings.
        parts = oneshot.extract_review(read(REVIEW_MD))
        self.assertEqual(parts["severities"], list(oneshot.SEVERITY_SECTIONS))
        for heading in oneshot.SEVERITY_SECTIONS.values():
            self.assertIn(heading, parts["step4"])

    def test_drift_fails_loudly(self):
        # Each break is written to a plugin root of its own and loaded the way
        # the runner loads it, so a review.md edit this path can't follow
        # stops the run instead of sending half a prompt.
        text = read(REVIEW_MD)
        breaks = {
            "persona rewritten": text.replace("grumpy principal engineer", "reviewer"),
            "code aspect renamed": text.replace("- **code** -", "- **general** -"),
            "audit section renamed": text.replace(
                "## Audit discipline", "## Audit rules"
            ),
            "placeholder renamed": text.replace("[DIFF_CONTENT]", "[THE_DIFF]"),
            "severity sentence gone": text.replace("SEVERITY is one of", "Use"),
            "severity renamed": text.replace("WARN (will cause", "MAJOR (will cause"),
            "template moved": text.replace("Example agent prompt structure", "Example"),
            "aspects heading gone": text.replace("### Available Review Aspects", ""),
            "settled line renamed": text.replace("Settled findings held back", "Held"),
        }
        for name, broken in breaks.items():
            with self.subTest(name):
                root = tempfile.mkdtemp()
                self.addCleanup(shutil.rmtree, root, True)
                os.makedirs(os.path.join(root, "commands"))
                with open(os.path.join(root, "commands", "review.md"), "w") as f:
                    f.write(broken)
                with self.assertRaises(oneshot.ExtractionError):
                    oneshot.load_command("review", plugin_root=root)

    def test_fenced_headings_stay_in_their_section(self):
        text = "## A\nx\n```\n## Not a heading\n```\ny\n## B\nz"
        self.assertIn("## Not a heading", oneshot.section(text, "A"))
        self.assertNotIn("z", oneshot.section(text, "A"))


class ParseTest(unittest.TestCase):
    def test_outside_in_keeps_an_embedded_pipe(self):
        lines, errored = oneshot.parse_pass_lines(
            "CRIT|a.py:3|greps for a|b and misses c|fact|errors\nnot a finding"
        )
        self.assertFalse(errored)
        self.assertEqual(lines, ["CRIT|a.py:3|greps for a|b and misses c|fact|errors"])

    def test_unknown_severity_and_lineless_location_are_dropped(self):
        lines, _ = oneshot.parse_pass_lines(
            "critical|a.py:3|x|fact|code\nWARN|a.py|x|fact|code\nNOTE|a.py:9|x|judgment|simplify"
        )
        self.assertEqual(lines, ["NOTE|a.py:9|x|judgment|simplify"])

    def test_prose_is_errored_but_a_short_nothing_is_not(self):
        self.assertEqual(oneshot.parse_pass_lines("Nothing found."), ([], False))
        self.assertTrue(oneshot.parse_pass_lines("This code is " + "fine " * 40)[1])

    def test_model_line_is_ours(self):
        out = oneshot.finish_report(
            "```markdown\n" + REPORT + "```", "_Model: m via p_"
        )
        self.assertEqual(out.split("\n")[2], "_Model: m via p_")
        self.assertNotIn("whatever the model claims", out)
        with self.assertRaises(ValueError):
            oneshot.finish_report("Looks good to me!", "_Model: m_")

    def test_models_map(self):
        roles = oneshot.parse_models(read(MODELS))
        self.assertTrue(roles["analysis"]["openai"])
        self.assertNotEqual(roles["analysis"]["openai"], "gpt-4o-mini")


class HelperTest(unittest.TestCase):
    def test_frontmatter_and_fences(self):
        self.assertEqual(oneshot._strip_frontmatter("no front"), "no front")
        self.assertEqual(oneshot._strip_frontmatter("---\nunclosed"), "---\nunclosed")
        with self.assertRaises(oneshot.ExtractionError):
            oneshot._fence_after("text", "missing")
        with self.assertRaises(oneshot.ExtractionError):
            oneshot._fence_after("marker and no fence", "marker")

    def test_parse_models_rejects_other_shapes(self):
        self.assertEqual(oneshot.parse_models("x:\n  y: z\n"), {})
        for bad in ("roles:\n  r\n", "roles:\n    c: m\n"):
            with self.subTest(bad), self.assertRaises(ValueError):
                oneshot.parse_models(bad)

    def test_model_override(self):
        wt = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, wt, True)
        default = oneshot.parse_models(read(MODELS))["analysis"]["openai"]
        self.assertEqual(oneshot.model_from_map(wt), (default, ""))
        os.makedirs(os.path.join(wt, ".sharpen"))
        path = os.path.join(wt, ".sharpen", "models.yaml")
        with open(path, "w") as f:
            f.write("roles:\n  analysis:\n    openai: gpt-mine\n")
        model, note = oneshot.model_from_map(wt)
        self.assertEqual(model, "gpt-mine")
        self.assertIn("override", note)
        with open(path, "w") as f:
            f.write("roles:\n  analysis\n")
        model, note = oneshot.model_from_map(wt)
        self.assertEqual(model, default)
        self.assertIn("ignored", note)

    def test_content_shapes(self):
        for bad in ({}, {"choices": []}, {"choices": [{"message": {"content": 3}}]}):
            with self.subTest(bad), self.assertRaises(ValueError):
                oneshot._content(bad)
        parts = {
            "choices": [{"message": {"content": [{"text": "a"}, {"text": "b"}, "x"]}}]
        }
        self.assertEqual(oneshot._content(parts), "ab")

    def test_client_setup_errors(self):
        with self.assertRaises(ValueError):
            oneshot.ChatClient("compatible", env={})
        with self.assertRaises(ValueError):
            oneshot.ChatClient("openai", env={})
        url = oneshot.ChatClient(
            "compatible", env={"SHARPEN_LLM_URL": "http://h/v1/"}
        ).url
        self.assertEqual(url, "http://h/v1/chat/completions")

    def test_unreachable_server_retries_then_raises(self):
        client = oneshot.ChatClient(
            "compatible",
            env={
                "SHARPEN_LLM_URL": "http://127.0.0.1:1/v1",
                "SHARPEN_LLM_API_KEY": KEY,
            },
        )
        with mock.patch.object(oneshot.time, "sleep") as sleep:
            with self.assertRaises(oneshot.ProviderError) as caught:
                client.chat("m", [], timeout=2)
        self.assertEqual(sleep.call_count, 1)
        self.assertIn("URLError", str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))

    def test_usage_errors(self):
        for argv in (
            ["review", "--model"],
            ["review", "--bogus"],
            ["review", "x"],
            ["imagine"],
        ):
            with self.subTest(argv), self.assertRaises(ValueError):
                oneshot._parse_args(argv)
        self.assertEqual(oneshot.main(["--bogus"], env={}), 2)

    def test_failed_call_is_named_to_the_aggregator(self):
        parts = oneshot.load_command("review")
        messages = oneshot.aggregate_messages(
            parts, {"code": ([], False, "HTTP 500")}, [], "_Model: m_"
        )
        self.assertIn(
            "[code] errored: the call failed (HTTP 500)", messages[1]["content"]
        )

    def test_select_aspects(self):
        parts = oneshot.load_command("review")
        with self.assertRaises(oneshot.Stop) as caught:
            oneshot.select_aspects(parts, "bogus")
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(
            len(oneshot.select_aspects(parts, None)), len(parts["aspects"])
        )


class Handler(BaseHTTPRequestHandler):
    calls = []
    status = 200
    reply = None  # callable(prompt) -> content

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Handler.calls.append({"auth": self.headers.get("Authorization"), "body": body})
        if Handler.status != 200:
            payload = json.dumps({"error": f"Incorrect API key provided: {KEY}"})
        else:
            content = Handler.reply(body["messages"][-1]["content"])
            payload = json.dumps({"choices": [{"message": {"content": content}}]})
        self.send_response(Handler.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload.encode())

    def log_message(self, *args):
        pass


def default_reply(prompt):
    if "You are the aggregator" in prompt:
        return REPORT
    return "CRIT|app.py:2|swallows the error|fact|errors"


class _Repo(unittest.TestCase):
    def setUp(self):
        Handler.calls, Handler.status, Handler.reply = [], 200, default_reply
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.repo = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        for args in (
            ("init", "-q", "-b", "main"),
            ("config", "user.email", "t@t"),
            ("config", "user.name", "t"),
        ):
            self.git(*args)
        self.commit("README.md", "hi\n", "init")
        self.git("checkout", "-q", "-b", BRANCH)
        self.commit("app.py", "try:\n    x()\nexcept Exception:\n    pass\n", "add app")

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", self.repo, *args], capture_output=True, check=True, text=True
        ).stdout

    def commit(self, name, text, msg):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)
        self.git("add", name)
        self.git("commit", "-q", "-m", msg)

    def run_oneshot(self, *args, env=None):
        full = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("SHARPEN_LLM", "OPENAI"))
        }
        full["PATH"] = "/usr/bin:/bin"  # no gh: diff_base falls back to main
        full.update(
            {
                "SHARPEN_LLM_URL": f"http://127.0.0.1:{self.server.server_address[1]}/v1",
                "SHARPEN_LLM_API_KEY": KEY,
            }
        )
        full.update(env or {})
        return subprocess.run(
            [sys.executable, SCRIPT, "review", "--worktree", self.repo, *args],
            capture_output=True,
            text=True,
            env=full,
        )

    def artifact(self, *parts):
        return os.path.join(self.repo, ".sharpen", "grumpy", BRANCH, *parts)


class RunTest(_Repo):
    def test_review_writes_report_and_receipt(self):
        r = self.run_oneshot(
            "--provider", "compatible", "--model", "local-m", "--aspects", "errors,code"
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        report = read(self.artifact("review.md"))
        self.assertIn("_Model: local-m via compatible (one-shot, no agent)_", report)
        rec = json.loads(read(self.artifact("receipts", "review.json")))
        self.assertEqual(
            rec["executor"],
            {"host": "oneshot", "provider": "compatible", "model": "local-m"},
        )
        self.assertEqual(rec["head"], self.git("rev-parse", "HEAD").strip())
        # Two finding passes and one aggregation, each built from review.md.
        self.assertEqual(len(Handler.calls), 3)
        prompts = [c["body"]["messages"][-1]["content"] for c in Handler.calls]
        self.assertTrue(any("focusing on errors" in p for p in prompts))
        self.assertTrue(all("app.py" in p for p in prompts))
        self.assertTrue(
            all(set(c["body"]) == {"model", "messages"} for c in Handler.calls)
        )
        self.assertEqual(Handler.calls[0]["auth"], f"Bearer {KEY}")
        # The key reaches the server and nothing else.
        for text in (report, json.dumps(rec), r.stderr, r.stdout):
            self.assertNotIn(KEY, text)

    def test_provider_error_is_redacted_and_earns_nothing(self):
        Handler.status = 401
        r = self.run_oneshot(
            "--provider", "compatible", "--model", "m", "--aspects", "code"
        )
        self.assertEqual(r.returncode, 1)
        self.assertNotIn(KEY, r.stderr)
        self.assertIn("[redacted]", r.stderr)
        self.assertFalse(os.path.exists(self.artifact("review.md")))
        self.assertFalse(os.path.exists(self.artifact("receipts", "review.json")))

    def test_unusable_report_earns_nothing(self):
        Handler.reply = lambda p: "Looks fine to me." if "aggregator" in p else ""
        r = self.run_oneshot(
            "--provider", "compatible", "--model", "m", "--aspects", "code"
        )
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.exists(self.artifact("receipts", "review.json")))

    def test_compatible_needs_a_model(self):
        r = self.run_oneshot("--provider", "compatible")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no model", r.stderr)
        self.assertEqual(Handler.calls, [])

    def test_openai_needs_its_key_and_defaults_from_the_map(self):
        r = self.run_oneshot("--provider", "openai", env={"OPENAI_API_KEY": ""})
        self.assertEqual(r.returncode, 2)
        self.assertIn("OPENAI_API_KEY is not set", r.stderr)
        r = self.run_oneshot("--provider", "openai", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        model = oneshot.parse_models(read(MODELS))["analysis"]["openai"]
        self.assertIn(f"to openai {model}", r.stdout)

    def test_dirty_tree_is_refused_before_any_call(self):
        with open(os.path.join(self.repo, "app.py"), "a") as f:
            f.write("# wip\n")
        r = self.run_oneshot("--provider", "compatible", "--model", "m")
        self.assertEqual(r.returncode, 1)
        self.assertIn("uncommitted", r.stderr)
        self.assertEqual(Handler.calls, [])

    def test_settled_entries_reach_the_aggregator(self):
        os.makedirs(self.artifact(), exist_ok=True)
        with open(self.artifact("fix.md"), "w") as f:
            f.write(
                "# Fix\n\n## Settled\n- app.py:2 | swallows | declined | demo only\n\n## Fixed\n- other\n"
            )
        r = self.run_oneshot(
            "--provider", "compatible", "--model", "m", "--aspects", "code"
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        agg = Handler.calls[-1]["body"]["messages"][-1]["content"]
        self.assertIn("- app.py:2 | swallows | declined | demo only", agg)
        self.assertNotIn("- other", agg)

    def test_errored_pass_is_named_to_the_aggregator(self):
        Handler.reply = lambda p: REPORT if "aggregator" in p else "Prose " * 50
        r = self.run_oneshot(
            "--provider", "compatible", "--model", "m", "--aspects", "code"
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        agg = Handler.calls[-1]["body"]["messages"][-1]["content"]
        self.assertTrue(
            re.search(r"\[code\] errored: the response was not pipe lines", agg)
        )


class MainTest(_Repo):
    """Exits of the in-process runner the subprocess cases don't reach."""

    def main(self, *args, env=None):
        full = {
            "SHARPEN_LLM_URL": f"http://127.0.0.1:{self.server.server_address[1]}/v1",
            "SHARPEN_LLM_MODEL": "m",
            **(env or {}),
        }
        return oneshot.main(
            [
                "review",
                "--provider",
                "compatible",
                "--worktree",
                self.repo,
                "--aspects",
                "code",
                *args,
            ],
            env=full,
        )

    def test_unknown_provider(self):
        self.assertEqual(oneshot.main(["review", "--provider", "nope"], env={}), 2)

    def test_extraction_error_is_a_setup_error(self):
        with mock.patch.object(
            oneshot, "load_command", side_effect=oneshot.ExtractionError("x")
        ):
            self.assertEqual(self.main(), 2)

    def test_empty_diff_earns_nothing(self):
        with mock.patch.object(oneshot, "resolve_diff", return_value=("main", "", [])):
            self.assertEqual(self.main(), 1)
        self.assertEqual(Handler.calls, [])

    def test_unknown_aspect(self):
        self.assertEqual(self.main("--aspects", "bogus"), 2)
        self.assertEqual(Handler.calls, [])

    def test_receipt_refused_keeps_the_report(self):
        with mock.patch.object(
            oneshot.receipt, "write_receipt", side_effect=ValueError("no")
        ):
            self.assertEqual(self.main(), 1)
        self.assertTrue(os.path.exists(self.artifact("review.md")))

    def test_every_pass_failing_earns_nothing(self):
        Handler.status = 500
        with mock.patch.object(oneshot.time, "sleep"):
            self.assertEqual(self.main(), 1)
        self.assertFalse(os.path.exists(self.artifact("review.md")))

    def test_no_base_falls_back_to_previous_commit(self):
        with mock.patch.object(
            oneshot.diff_base, "resolve", return_value=(None, "none")
        ):
            base, diff, names = oneshot.resolve_diff(self.repo)
        self.assertEqual((base, names), ("HEAD~1", ["app.py"]))

    def test_changed_files_skip_deleted_and_respect_the_cap(self):
        self.assertEqual(oneshot.changed_files_text(self.repo, ["gone.py"]), "")
        with mock.patch.object(oneshot, "FILES_CAP", 1):
            self.assertIn("omitted", oneshot.changed_files_text(self.repo, ["app.py"]))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
