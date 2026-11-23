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
import time
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, "..", "scripts"))
SCRIPT = os.path.join(SCRIPTS, "oneshot.py")
REVIEW_MD = os.path.normpath(os.path.join(HERE, "..", "commands", "review.md"))
IMAGINE_MD = os.path.normpath(os.path.join(HERE, "..", "commands", "imagine.md"))
MODELS = os.path.normpath(os.path.join(HERE, "..", "models.yaml"))
sys.path.insert(0, SCRIPTS)

import oneshot  # noqa: E402

BRANCH = "feature/x"
# Not shaped like a key, so only the explicit redaction of the configured key
# can hide it, never the key-shape regex.
KEY = "Zq9plainsecret0123456789"
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
IMAGINE_REPORT = """# Production Imagination: a thing

_Analyzed: whatever the model claims_

_Model: whatever the model claims_

_It will page someone._

## 🚨 Bugs Found (Fix These)

- [HTTP]: swallows the error [app.py:2]

## ⚠️ Serious Concerns (Should Fix)

## 🤔 Questionable Decisions (Worth Discussing)

## Strengths

- cleanup: fine

## Verdict

Fix and reship.
"""
IMAGINE_PASS = (
    "transport:HTTP|API calls: 1|UX: fine|Logs: thin|Metrics: none\n"
    "CRIT|app.py:2|swallows the error|fact|transport:HTTP\n"
    "HANDLED|cleanup|the handle is closed"
)


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

    def test_real_imagine_md_extracts(self):
        parts = oneshot.load_command("imagine")
        self.assertIn("paged at 3am", parts["persona"])
        self.assertEqual(
            [n for n, _, _ in parts["units"]],
            ["happy-path", "state", "concurrency", "observability", "docs"],
        )
        for name, title, prompt in parts["units"]:
            with self.subTest(name):
                self.assertTrue(title.startswith("Agent "))
                for slot in ("[DIFF_BASE]", "[DIFF_CONTENT]", "SEVERITY|file:line"):
                    self.assertIn(slot, prompt)
        self.assertIn("HAPPY PATH", parts["units"][0][2])
        self.assertIn("DOCUMENTATION COVERAGE", parts["units"][4][2])
        self.assertEqual(parts["severities"], list(oneshot.SEVERITY_SECTIONS))
        self.assertEqual(
            parts["context_labels"],
            [
                "cleanup",
                "concurrency",
                "docs",
                "rate-limit",
                "state-transition",
                "transport",
            ],
        )
        headings = [h for h, _ in parts["aggregate"]]
        self.assertEqual(
            headings,
            [
                "## Audit Discipline",
                "## Step 3: Aggregate and Deliver the Simulation Report",
                "## Personality Guidelines",
            ],
        )
        step3 = parts["aggregate"][1][1]
        for anchor in ("# Production Imagination:", "_Analyzed:", "## Verdict"):
            self.assertIn(anchor, step3)
        # Nothing from the steps this script does itself leaks into a prompt.
        sent = "".join(t for _, t in parts["aggregate"])
        for step in ("Step 4", "Step 5", "Step 6", "receipt.py"):
            self.assertNotIn(step, sent)

    def test_imagine_prompt_checks(self):
        import oneshot_prompts as op

        agents = "".join(
            f"### {start} x\n\n```\n`ctx|a|b`\n[DIFF_BASE] [DIFF_CONTENT] "
            "SEVERITY|file:line\nSEVERITY is CRIT (a), WARN (b), or NOTE (c).\nend\n```\n\n"
            for _, start in op.IMAGINE_AGENTS
        )
        units, labels = op._imagine_units(agents)
        self.assertEqual(len(units), 5)
        self.assertEqual(op._imagine_severities(units), ["CRIT", "WARN", "NOTE"])
        broken = {
            "no fence": agents.replace(
                agents[agents.index("```") : agents.index("```\n\n") + 4],
                "no prompt here\n",
                1,
            ),
            "no context line": agents.replace("`ctx|a|b`", "ctx"),
        }
        for name, text in broken.items():
            with self.subTest(name), self.assertRaises(op.ExtractionError):
                op._imagine_units(text)
        no_sentence = [("a", "A", "CRIT WARN NOTE")]
        with self.assertRaises(op.ExtractionError):
            op._imagine_severities(no_sentence)
        missing = [units[0], ("b", "B", "only CRIT and WARN here")]
        with self.assertRaisesRegex(op.ExtractionError, "NOTE"):
            op._imagine_severities(missing)

    def test_imagine_drift_fails_loudly(self):
        text = read(IMAGINE_MD)
        sixth = text.replace(
            "## Audit Discipline",
            "### Agent 6: Cost\n\n```\n[DIFF_BASE] [DIFF_CONTENT] SEVERITY|file:line\n```\n\n## Audit Discipline",
        )
        breaks = {
            "persona rewritten": text.replace("grumpy principal engineer", "reviewer"),
            "agent renamed": text.replace(
                "### Agent 3: Concurrency", "### Agent 3: Races"
            ),
            "sixth agent": sixth,
            "placeholder renamed": text.replace("[DIFF_CONTENT]", "[THE_DIFF]", 1),
            "severity renamed": text.replace(
                "WARN (will cause", "MAJOR (will cause", 1
            ),
            "audit heading renamed": text.replace(
                "## Audit Discipline", "## Audit rules"
            ),
            "analyzed line gone": text.replace("_Analyzed:", "_Scope:"),
            "settled line renamed": text.replace("Settled findings held back", "Held"),
        }
        for name, broken in breaks.items():
            with self.subTest(name):
                root = tempfile.mkdtemp()
                self.addCleanup(shutil.rmtree, root, True)
                os.makedirs(os.path.join(root, "commands"))
                with open(os.path.join(root, "commands", "imagine.md"), "w") as f:
                    f.write(broken)
                with self.assertRaises(oneshot.ExtractionError):
                    oneshot.load_command("imagine", plugin_root=root)


class ParseTest(unittest.TestCase):
    def test_outside_in_keeps_an_embedded_pipe(self):
        lines, errored, dropped = oneshot.parse_pass_lines(
            "CRIT|a.py:3|greps for a|b and misses c|fact|errors\nnot a finding"
        )
        self.assertFalse(errored)
        self.assertEqual(lines, ["CRIT|a.py:3|greps for a|b and misses c|fact|errors"])
        self.assertEqual(dropped, 0)  # prose without pipes isn't a lost finding

    def test_unknown_severity_and_lineless_location_are_dropped_and_counted(self):
        lines, errored, dropped = oneshot.parse_pass_lines(
            "critical|a.py:3|x|fact|code\nWARN|a.py|x|fact|code\n"
            "WARN|a.py:4|x|fact\nNOTE|a.py:9|x|judgment|simplify"
        )
        self.assertEqual(lines, ["NOTE|a.py:9|x|judgment|simplify"])
        self.assertFalse(errored)
        self.assertEqual(dropped, 3)

    def test_prose_and_empty_are_errored_but_a_short_nothing_is_not(self):
        self.assertEqual(oneshot.parse_pass_lines("Nothing found."), ([], False, 0))
        self.assertTrue(oneshot.parse_pass_lines("This code is " + "fine " * 40)[1])
        # A provider answering "" (a full queue, say) reviewed nothing.
        self.assertTrue(oneshot.parse_pass_lines("")[1])
        self.assertTrue(oneshot.parse_pass_lines("  \n")[1])

    def test_imagine_context_and_handled_lines_are_kept(self):
        labels = oneshot.load_command("imagine")["context_labels"]
        lines, errored, dropped = oneshot.parse_pass_lines(
            "transport:Slack|API calls: 2|UX: janky|Logs: none|Metrics: none\n"
            "cleanup | Cleanup guarantees: never | User-facing error: cryptic\n"
            "HANDLED|retries|bounded with backoff\n"
            "WARN|a.py:3|counter race|fact|concurrency\n"
            "concurrency|too short\n"
            "transcript:Slack|a|b|c|d",
            labels,
        )
        self.assertEqual(
            lines,
            [
                "transport:Slack|API calls: 2|UX: janky|Logs: none|Metrics: none",
                "cleanup|Cleanup guarantees: never|User-facing error: cryptic",
                "HANDLED|retries|bounded with backoff",
                "WARN|a.py:3|counter race|fact|concurrency",
            ],
        )
        self.assertFalse(errored)
        self.assertEqual(dropped, 2)
        # Context lines alone are a pass that ran: nothing to find, not errored.
        self.assertFalse(
            oneshot.parse_pass_lines(
                "docs|User-facing surface changed: none|Documented: yes|Stale docs found: no",
                labels,
            )[1]
        )
        # Review keeps no context lines: a label there is just a malformed line.
        self.assertEqual(oneshot.parse_pass_lines("cleanup|a|b")[2], 1)

    def test_backticks_are_stripped_per_line(self):
        # imagine.md shows CONTEXT lines in backticks; a model that copies
        # that wraps every line, not just the reply.
        labels = oneshot.load_command("imagine")["context_labels"]
        lines, errored, dropped = oneshot.parse_pass_lines(
            "`cleanup|Cleanup guarantees: never|User-facing error: cryptic`\n"
            "`transport:Slack|API calls: 2|UX: janky|Logs: none|Metrics: none`\n"
            "`WARN|a.py:3|counter race|fact|concurrency`",
            labels,
        )
        self.assertEqual(
            lines,
            [
                "cleanup|Cleanup guarantees: never|User-facing error: cryptic",
                "transport:Slack|API calls: 2|UX: janky|Logs: none|Metrics: none",
                "WARN|a.py:3|counter race|fact|concurrency",
            ],
        )
        self.assertFalse(errored)
        self.assertEqual(dropped, 0)
        # And on review, a backticked finding line is still a finding.
        self.assertEqual(
            oneshot.parse_pass_lines("`CRIT|a.py:3|x|fact|errors`")[0],
            ["CRIT|a.py:3|x|fact|errors"],
        )

    def test_review_does_not_accept_handled_lines(self):
        # Review's template never asks for HANDLED: a reply of only that is
        # a malformed line, not "answered, nothing found".
        self.assertEqual(
            oneshot.parse_pass_lines("HANDLED|retries|bounded with backoff"),
            ([], False, 1),
        )

    def test_model_line_is_ours(self):
        out = oneshot.finish_report(
            "```markdown\n" + REPORT + "```", "_Model: m via p_"
        )
        self.assertEqual(out.split("\n")[2], "_Model: m via p_")
        self.assertNotIn("whatever the model claims", out)
        with self.assertRaises(ValueError):
            oneshot.finish_report("Looks good to me!", "_Model: m_")

    def test_preamble_and_trailing_note_are_dropped_and_stamps_are_ours(self):
        wrapped = "Here is the report:\n\n```markdown\n" + REPORT + "```\nLet me know!"
        out = oneshot.finish_report(wrapped, "_Model: m_", "_Base: b, from s_")
        self.assertTrue(
            out.startswith("# Code Review: a thing\n\n_Model: m_\n_Base: b, from s_\n")
        )
        self.assertNotIn("Here is the report", out)
        self.assertNotIn("Let me know", out)
        self.assertNotIn("```", out)
        self.assertTrue(out.rstrip().endswith("Fix and reship."))
        # A `_Base:` line the model invented goes, like its `_Model:` line.
        invented = REPORT.replace("_Fine._", "_Base: whatever, from nowhere_")
        out = oneshot.finish_report(invented, "_Model: m_", "_Base: b, from s_")
        self.assertNotIn("nowhere", out)
        self.assertEqual(out.count("_Base:"), 1)

    def test_imagine_report_keeps_its_own_title_and_stamps(self):
        out = oneshot.finish_report(
            IMAGINE_REPORT,
            "_Model: m_",
            "_Analyzed: b, from s — HEAD at abc1234_",
            title="# Production Imagination",
        )
        self.assertTrue(
            out.startswith(
                "# Production Imagination: a thing\n\n_Model: m_\n"
                "_Analyzed: b, from s — HEAD at abc1234_\n"
            )
        )
        self.assertEqual(out.count("_Analyzed:"), 1)
        self.assertNotIn("whatever the model claims", out)
        # A review report is not an imagine report, and the other way round.
        with self.assertRaises(ValueError):
            oneshot.finish_report(
                REPORT, "_Model: m_", title="# Production Imagination"
            )
        with self.assertRaises(ValueError):
            oneshot.finish_report(IMAGINE_REPORT, "_Model: m_")

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
            ["audit"],
        ):
            with self.subTest(argv), self.assertRaises(ValueError):
                oneshot._parse_args(argv)
        self.assertEqual(oneshot.main(["--bogus"], env={}), 2)

    def test_failed_call_and_dropped_lines_are_named_to_the_aggregator(self):
        parts = oneshot.load_command("review")
        results = {
            "code": oneshot.PassResult([], False, "HTTP 500", 0),
            "errors": oneshot.PassResult(["CRIT|a.py:1|x|fact|errors"], False, None, 2),
        }
        prompt = oneshot.aggregate_messages(parts, results, [], "_Model: m_")[1][
            "content"
        ]
        self.assertIn("[code] errored: the call failed (HTTP 500)", prompt)
        self.assertIn(
            "[errors]\nCRIT|a.py:1|x|fact|errors\n(the runner dropped 2", prompt
        )

    def test_retry_after_is_honored_and_capped(self):
        self.assertEqual(oneshot._retry_after({"Retry-After": "7"}), 7)
        self.assertEqual(
            oneshot._retry_after({"Retry-After": "9999"}), oneshot.RETRY_WAIT_CAP
        )
        self.assertIsNone(
            oneshot._retry_after({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
        )
        self.assertIsNone(oneshot._retry_after({}))
        self.assertIsNone(oneshot._retry_after(None))

    def test_meta_keeps_usage_served_model_and_request_id(self):
        data = {"usage": {"total_tokens": 42}, "model": "served-m"}
        self.assertEqual(
            oneshot._meta(data, {"x-request-id": "r1"}), (42, "served-m", "r1")
        )
        self.assertEqual(oneshot._meta({}, {}), (None, None, None))
        self.assertEqual(
            oneshot._meta({"usage": "junk", "model": ""}), (None, None, None)
        )

    def test_git_failures_carry_gits_reason(self):
        wt = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, wt, True)
        subprocess.run(["git", "-C", wt, "init", "-q"], check=True)
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            oneshot._git(wt, "rev-parse", "--verify", "nope^{commit}")
        self.assertNotIn("returned non-zero exit status", str(caught.exception))
        self.assertIn("fatal", str(caught.exception).lower())

    def test_select_aspects(self):
        parts = oneshot.load_command("review")
        with self.assertRaises(oneshot.Stop) as caught:
            oneshot.select_aspects(parts, "bogus")
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(
            len(oneshot.select_aspects(parts, None)), len(parts["aspects"])
        )


# ponytail: a single-threaded fake, so parallel passes are served one at a time;
# switch to http.server.ThreadingHTTPServer when a case needs simultaneous
# calls in flight (four 429s retrying at once, a 400 while three are queued)
class Handler(BaseHTTPRequestHandler):
    calls = []
    status = 200  # or callable(prompt, call number) -> status
    reply = None  # callable(prompt) -> content
    # Not `finish`: that name is StreamRequestHandler's own method, and a
    # string there made every request end in a traceback on stderr.
    finish_reason = "stop"
    truncate = 0  # this many calls get a body shorter than Content-Length
    retry_after = None  # sent as Retry-After on a non-200 answer
    delay = 0  # seconds each answer takes

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Handler.calls.append({"auth": self.headers.get("Authorization"), "body": body})
        prompt = body["messages"][-1]["content"]
        status = Handler.status
        if callable(status):
            status = status(prompt, len(Handler.calls))
        if status != 200:
            payload = json.dumps({"error": f"Incorrect API key provided: {KEY}"})
        else:
            choice = {
                "message": {"content": Handler.reply(prompt)},
                "finish_reason": Handler.finish_reason,
            }
            payload = json.dumps(
                {"choices": [choice], "usage": {"total_tokens": 7}, "model": "served"}
            )
        data = payload.encode()
        if Handler.delay:
            time.sleep(Handler.delay)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if status != 200 and Handler.retry_after is not None:
            self.send_header("Retry-After", str(Handler.retry_after))
        if Handler.truncate > 0:
            Handler.truncate -= 1
            self.send_header("Content-Length", str(len(data) + 100))
            self.end_headers()
            self.wfile.write(data[:10])
            return
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def default_reply(prompt):
    if "You are the aggregator" in prompt:
        return REPORT
    return "CRIT|app.py:2|swallows the error|fact|errors"


class _Repo(unittest.TestCase):
    def setUp(self):
        Handler.calls, Handler.status, Handler.reply = [], 200, default_reply
        Handler.finish_reason, Handler.truncate = "stop", 0
        Handler.retry_after, Handler.delay = None, 0
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

    def run_oneshot(self, *args, env=None, pass_name="review"):
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
            [sys.executable, SCRIPT, pass_name, "--worktree", self.repo, *args],
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
        self.assertIn(
            "_Model: local-m via compatible (one-shot, no agent; aspects: code, errors)_\n"
            "_Base: main, from ",
            report,
        )
        self.assertNotIn("_Incomplete:", report)
        rec = json.loads(read(self.artifact("receipts", "review.json")))
        self.assertEqual(
            rec["executor"],
            {
                "host": "oneshot",
                "provider": "compatible",
                "model": "local-m",
                "aspects": "code,errors",
            },
        )
        self.assertEqual(rec["head"], self.git("rev-parse", "HEAD").strip())
        # What the passes returned is on disk next to the report.
        passes = [
            json.loads(ln)
            for ln in read(self.artifact("review-passes.jsonl")).split("\n")
            if ln
        ]
        self.assertEqual(sorted(p["aspect"] for p in passes), ["code", "errors"])
        self.assertEqual(passes[0]["head"], rec["head"])
        self.assertEqual(
            passes[0]["lines"], ["CRIT|app.py:2|swallows the error|fact|errors"]
        )
        # One line per completed pass: duration, findings, tokens, served model.
        self.assertRegex(
            r.stderr,
            r"pass errors: done in \d+s, 1 finding line\(s\), 7 tokens, served by served",
        )
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

    def test_imagine_writes_report_and_receipt(self):
        def reply(prompt):
            if "You are the aggregator" in prompt:
                return IMAGINE_REPORT
            return IMAGINE_PASS

        Handler.reply = reply
        r = self.run_oneshot(
            "--provider", "compatible", "--model", "local-m", pass_name="imagine"
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        head = self.git("rev-parse", "HEAD").strip()
        report = read(self.artifact("imagine.md"))
        self.assertIn(
            "_Model: local-m via compatible (one-shot, no agent; aspects: happy-path, "
            "state, concurrency, observability, docs)_\n"
            f"_Analyzed: main, from the default branch — HEAD at {head[:7]}_",
            report,
        )
        rec = json.loads(read(self.artifact("receipts", "imagine.json")))
        self.assertEqual(rec["head"], head)
        self.assertEqual(
            rec["executor"],
            {
                "host": "oneshot",
                "provider": "compatible",
                "model": "local-m",
                "aspects": "happy-path,state,concurrency,observability,docs",
            },
        )
        # Its pass log is its own; review's isn't touched.
        passes = [
            json.loads(ln)
            for ln in read(self.artifact("imagine-passes.jsonl")).split("\n")
            if ln
        ]
        self.assertEqual(len(passes), 5)
        self.assertEqual(passes[0]["lines"], IMAGINE_PASS.split("\n"))
        self.assertFalse(os.path.exists(self.artifact("review-passes.jsonl")))
        self.assertRegex(
            r.stderr,
            r"pass happy-path: done in \d+s, 1 finding line\(s\), 2 context line\(s\)",
        )
        # Five simulation passes built from imagine.md, then one aggregation
        # that gets Step 3 and every kind of pass line under the agent's name.
        self.assertEqual(len(Handler.calls), 6)
        prompts = [c["body"]["messages"][-1]["content"] for c in Handler.calls]
        for marker in (
            "HAPPY PATH",
            "STATE TRANSITIONS",
            "CONCURRENCY",
            "OBSERVABILITY",
            "DOCUMENTATION COVERAGE",
        ):
            self.assertTrue(any(marker in p for p in prompts[:5]), marker)
        agg = prompts[5]
        self.assertIn("## Step 3: Aggregate and Deliver the Simulation Report", agg)
        self.assertIn("[happy-path: Agent 1: Happy Path Simulation", agg)
        self.assertIn("HANDLED|cleanup|the handle is closed", agg)
        self.assertIn("transport:HTTP|API calls: 1", agg)
        self.assertIn("`# Production Imagination:`", agg)
        self.assertNotIn("## Step 4: Aggregate and Deliver the Verdict", agg)

    def test_imagine_aspects_pick_simulation_passes(self):
        Handler.reply = lambda p: (
            IMAGINE_REPORT if "You are the aggregator" in p else IMAGINE_PASS
        )
        r = self.run_oneshot(
            "--provider",
            "compatible",
            "--model",
            "m",
            "--aspects",
            "docs,state",
            pass_name="imagine",
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(Handler.calls), 3)
        self.assertIn("aspects: state, docs", read(self.artifact("imagine.md")))
        r = self.run_oneshot(
            "--provider",
            "compatible",
            "--model",
            "m",
            "--aspects",
            "code",
            pass_name="imagine",
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("happy-path", r.stderr)

    def test_imagine_dry_run_prints_five_prompts_and_calls_nothing(self):
        r = self.run_oneshot("--model", "m", "--dry-run", pass_name="imagine")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.count("===== pass: "), 5)
        self.assertIn("5 pass call(s) + 1 aggregation call", r.stdout)
        self.assertEqual(Handler.calls, [])

    def test_rejected_key_is_redacted_and_a_setup_error(self):
        Handler.status = 401
        r = self.run_oneshot(
            "--provider", "compatible", "--model", "m", "--aspects", "code"
        )
        self.assertEqual(r.returncode, 2)
        self.assertNotIn(KEY, r.stderr)
        self.assertIn("[redacted]", r.stderr)
        self.assertEqual(len(Handler.calls), 1)  # a 401 is not retried
        self.assertFalse(os.path.exists(self.artifact("review.md")))
        self.assertFalse(os.path.exists(self.artifact("receipts", "review.json")))

    def test_setup_status_cancels_the_passes_still_queued(self):
        Handler.status, Handler.delay = 401, 0.3
        r = self.run_oneshot(
            "--provider",
            "compatible",
            "--model",
            "m",
            "--aspects",
            "code,errors,docs",
            "--jobs",
            "1",
        )
        self.assertEqual(r.returncode, 2)
        # The first answer settled it. The worker may already have the second
        # upload in flight (urllib can't be cancelled; `delay` holds it there),
        # but the third, still queued, is cancelled instead of sent.
        self.assertLess(len(Handler.calls), 3)

    def test_server_errors_retry_once_then_earn_nothing(self):
        Handler.status = 500
        r = self.run_oneshot(
            "--provider", "compatible", "--model", "m", "--aspects", "code"
        )
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(Handler.calls), 2)
        self.assertNotIn(KEY, r.stderr)

    def test_rate_limit_is_retried(self):
        Handler.status = lambda prompt, n: 429 if n == 1 else 200
        r = self.run_oneshot(
            "--provider", "compatible", "--model", "m", "--aspects", "code"
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(Handler.calls), 3)  # 429, retry, aggregation

    def test_truncated_body_is_retried(self):
        Handler.truncate = 1
        r = self.run_oneshot(
            "--provider", "compatible", "--model", "m", "--aspects", "code"
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_cut_short_answer_is_not_a_clean_pass(self):
        Handler.finish_reason = "length"
        r = self.run_oneshot(
            "--provider", "compatible", "--model", "m", "--aspects", "code"
        )
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.exists(self.artifact("receipts", "review.json")))

    def test_one_failed_pass_saves_the_report_but_no_receipt(self):
        Handler.status = lambda prompt, n: (
            500 if "focusing on errors" in prompt else 200
        )
        r = self.run_oneshot(
            "--provider", "compatible", "--model", "m", "--aspects", "code,errors"
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("errors", r.stderr.split("failed or answered without")[0])
        self.assertTrue(os.path.exists(self.artifact("review.md")))
        self.assertIn("_Incomplete: errors (failed", read(self.artifact("review.md")))
        self.assertFalse(os.path.exists(self.artifact("receipts", "review.json")))
        agg = Handler.calls[-1]["body"]["messages"][-1]["content"]
        self.assertIn("[errors] errored: the call failed", agg)

    def test_empty_answer_is_not_a_clean_pass(self):
        Handler.reply = lambda p: REPORT if "aggregator" in p else ""
        r = self.run_oneshot(
            "--provider", "compatible", "--model", "m", "--aspects", "code"
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("empty answer: errored", r.stderr)
        self.assertIn(
            "[code] errored", Handler.calls[-1]["body"]["messages"][-1]["content"]
        )
        self.assertIn("_Incomplete: code", read(self.artifact("review.md")))
        self.assertFalse(os.path.exists(self.artifact("receipts", "review.json")))

    def test_malformed_lines_are_counted_not_lost_silently(self):
        Handler.reply = lambda p: (
            REPORT
            if "aggregator" in p
            else "CRIT|app.py:2|swallows|fact|errors\nWARN|app.py|no line|fact|errors"
        )
        r = self.run_oneshot(
            "--provider", "compatible", "--model", "m", "--aspects", "errors"
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("1 malformed line(s) dropped", r.stderr)
        agg = Handler.calls[-1]["body"]["messages"][-1]["content"]
        self.assertIn("the runner dropped 1 malformed line(s)", agg)

    def test_finding_lines_reach_the_aggregator(self):
        r = self.run_oneshot(
            "--provider", "compatible", "--model", "m", "--aspects", "errors"
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        agg = Handler.calls[-1]["body"]["messages"][-1]["content"]
        self.assertIn("[errors]\nCRIT|app.py:2|swallows the error|fact|errors", agg)

    def test_unusable_report_earns_nothing(self):
        Handler.reply = lambda p: (
            "Looks fine to me." if "aggregator" in p else "Nothing found."
        )
        r = self.run_oneshot(
            "--provider", "compatible", "--model", "m", "--aspects", "code"
        )
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.exists(self.artifact("receipts", "review.json")))
        self.assertFalse(os.path.exists(self.artifact("review.md")))
        # The paid finding passes survive the failed aggregation.
        self.assertIn("aggregation failed", r.stderr)
        self.assertIn(self.artifact("review-passes.jsonl"), r.stderr)
        self.assertTrue(os.path.exists(self.artifact("review-passes.jsonl")))

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

    def test_prose_pass_is_named_and_earns_nothing(self):
        Handler.reply = lambda p: REPORT if "aggregator" in p else "Prose " * 50
        r = self.run_oneshot(
            "--provider", "compatible", "--model", "m", "--aspects", "code"
        )
        self.assertEqual(r.returncode, 1)
        agg = Handler.calls[-1]["body"]["messages"][-1]["content"]
        self.assertTrue(
            re.search(r"\[code\] errored: the response was not pipe lines", agg)
        )
        self.assertFalse(os.path.exists(self.artifact("receipts", "review.json")))

    def test_detached_head_is_refused_before_any_call(self):
        self.git("checkout", "-q", "--detach")
        r = self.run_oneshot("--provider", "compatible", "--model", "m")
        self.assertEqual(r.returncode, 2)
        self.assertIn("detached HEAD", r.stderr)
        self.assertEqual(Handler.calls, [])

    def test_settled_legacy_file_is_read_only_when_the_new_one_is_missing(self):
        legacy = os.path.join(self.repo, ".claude", "grumpy", BRANCH)
        os.makedirs(legacy)
        with open(os.path.join(legacy, "fix.md"), "w") as f:
            f.write("## Settled\n- old.py:1 | x | declined | legacy\n")
        self.assertEqual(
            oneshot.settled_entries(self.repo, BRANCH),
            ["- old.py:1 | x | declined | legacy"],
        )
        os.makedirs(self.artifact(), exist_ok=True)
        with open(self.artifact("fix.md"), "w") as f:
            f.write("## Settled\n- new.py:1 | x | declined | new\n")
        self.assertEqual(
            oneshot.settled_entries(self.repo, BRANCH),
            ["- new.py:1 | x | declined | new"],
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
        with mock.patch.object(
            oneshot, "resolve_diff", return_value=("main", "a" * 40, "", [], "x")
        ):
            self.assertEqual(self.main(), 1)
        self.assertEqual(Handler.calls, [])

    def test_rate_limit_waits_for_retry_after_and_says_so(self):
        Handler.status = lambda prompt, n: 429 if n == 1 else 200
        Handler.retry_after = 7
        # Swap oneshot's own `time` name, not the shared module's `sleep`: the
        # base lookup's `gh pr view` runs under subprocess.run(timeout=...), whose
        # wait polls time.sleep, so a global patch counted those polls too.
        clock = mock.Mock(wraps=time)
        sleep = clock.sleep = mock.Mock()
        # Nested, not parenthesized: the 3.9 floor doesn't parse `with (a, b):`.
        with mock.patch.object(oneshot, "time", clock):
            with mock.patch.object(oneshot, "_log") as log:
                self.assertEqual(self.main(env={"SHARPEN_LLM_API_KEY": KEY}), 0)
        self.assertEqual(sleep.call_count, 1)
        self.assertTrue(7 <= sleep.call_args.args[0] < 8, sleep.call_args)
        logged = "\n".join(str(c.args[0]) for c in log.call_args_list)
        self.assertRegex(
            logged,
            r"pass code: attempt 1 failed \(rate limited: HTTP 429.*retrying in [78]s",
        )
        self.assertNotIn(KEY, logged)

    def test_compatible_runs_one_at_a_time_unless_told_otherwise(self):
        self.assertEqual(oneshot._parse_args(["review"])["jobs"], None)
        self.assertEqual(oneshot._parse_args(["review", "--jobs", "3"])["jobs"], 3)
        self.assertEqual(oneshot.DEFAULT_JOBS, {"openai": 4, "compatible": 1})
        with mock.patch.object(
            oneshot.concurrent.futures,
            "ThreadPoolExecutor",
            wraps=oneshot.concurrent.futures.ThreadPoolExecutor,
        ) as pool:
            self.assertEqual(self.main(), 0)
        pool.assert_called_once_with(max_workers=1)

    def test_caps_are_marked_in_the_prompt_and_on_stderr(self):
        with mock.patch.object(oneshot, "DIFF_CAP", 20):
            with mock.patch.object(oneshot, "FILE_CAP", 10):
                with mock.patch.object(oneshot, "_log") as log:
                    self.assertEqual(self.main(), 0)
        prompt = Handler.calls[0]["body"]["messages"][-1]["content"]
        self.assertIn("[grumpy oneshot: diff truncated at 20 characters", prompt)
        self.assertIn("[grumpy oneshot: file truncated at 10 characters]", prompt)
        logged = "\n".join(str(c.args[0]) for c in log.call_args_list)
        self.assertIn("sending the first 20", logged)
        self.assertIn("file text truncated at 10 characters: app.py", logged)

    def test_receipt_refuses_a_head_other_than_the_reviewed_one(self):
        self.assertEqual(self.main(), 0)
        with self.assertRaises(ValueError) as caught:
            oneshot.receipt.write_receipt("review", self.repo, head="0" * 40)
        self.assertIn("HEAD moved during the run", str(caught.exception))

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

    def test_head_moving_during_the_run_earns_nothing(self):
        real = oneshot.finish_report

        def commit_then_finish(text, line, *stamps, **kw):
            self.commit("late.py", "x = 1\n", "landed mid-run")
            return real(text, line, *stamps, **kw)

        with mock.patch.object(
            oneshot, "finish_report", side_effect=commit_then_finish
        ):
            self.assertEqual(self.main(), 1)
        self.assertFalse(os.path.exists(self.artifact("receipts", "review.json")))

    def test_aspects_are_stripped_and_unknown_ones_refused(self):
        self.assertEqual(self.main("--aspects", " code , errors"), 0)
        Handler.calls = []
        self.assertEqual(self.main("--aspects", "code,bogus"), 2)
        self.assertEqual(Handler.calls, [])

    def test_override_is_read_from_the_toplevel(self):
        os.makedirs(os.path.join(self.repo, ".sharpen"), exist_ok=True)
        with open(os.path.join(self.repo, ".sharpen", "models.yaml"), "w") as f:
            f.write("roles:\n  analysis:\n    openai: gpt-top\n")
        sub = os.path.join(self.repo, "sub")
        os.makedirs(sub)
        self.assertEqual(oneshot.model_from_map(sub)[0], "gpt-top")

    def test_receipt_executor_needs_a_host(self):
        for bad in ({"provider": "p"}, "oneshot"):
            with self.subTest(bad), self.assertRaises(ValueError):
                oneshot.receipt._checked_executor(bad)
        self.assertEqual(
            oneshot.receipt._checked_executor(
                {"host": "oneshot", "model": None, "provider": 3}
            ),
            {"host": "oneshot"},
        )

    def test_no_base_falls_back_to_previous_commit(self):
        with mock.patch.object(
            oneshot.diff_base, "resolve", return_value=(None, "none")
        ):
            base, _head, _diff, names, why = oneshot.resolve_diff(self.repo)
        self.assertEqual((base, names), ("HEAD~1", ["app.py"]))
        self.assertIn("no branch base", why)

    def test_changed_files_skip_deleted_and_respect_the_cap(self):
        self.assertEqual(oneshot.changed_files_text(self.repo, ["gone.py"]), "")
        with mock.patch.object(oneshot, "FILES_CAP", 1):
            self.assertIn("omitted", oneshot.changed_files_text(self.repo, ["app.py"]))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
