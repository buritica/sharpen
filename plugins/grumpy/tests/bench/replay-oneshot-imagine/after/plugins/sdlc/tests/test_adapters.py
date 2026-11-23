#!/usr/bin/env python3
"""Tests for openai_adapter.py and local_llm_adapter.py.

Both run the manifest's gate commands and then hand the review to grumpy's
oneshot.py. The real oneshot.py is grumpy's to test (plugins/grumpy/tests/
test_oneshot.py); here a stub stands in for it and records how it was called.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))

STUB = """import json, os, signal, sys
with open(os.environ["STUB_OUT"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "env": {k: v for k, v in os.environ.items()
               if k.startswith(("SHARPEN_LLM", "OPENAI"))}}) + "\\n")
code = os.environ.get("STUB_EXIT_" + sys.argv[1], os.environ.get("STUB_EXIT", "0"))
if code == "kill":
    os.kill(os.getpid(), signal.SIGKILL)
sys.exit(int(code))
"""


def git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], capture_output=True, check=True)


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        git(self.repo, "init", "-q", "-b", "feat/x")
        self.grumpy = os.path.join(self.repo, "grumpy")
        os.makedirs(os.path.join(self.grumpy, "scripts"))
        with open(os.path.join(self.grumpy, "scripts", "oneshot.py"), "w") as f:
            f.write(STUB)
        self.out = os.path.join(self.repo, "stub.json")

    def manifest(
        self, commands=None, caps=("test", "lint", "typecheck", "review"), model=None
    ):
        m = {
            "protocol_version": "1",
            "provider": {"name": "x", **({"model": model} if model else {})},
            "capabilities": list(caps),
            "x-host-command-map": commands
            or {"test": "true", "lint": "true", "typecheck": "true"},
        }
        path = os.path.join(self.repo, "caps.json")
        with open(path, "w") as f:
            json.dump(m, f)
        return path

    def run_adapter(self, script, *args, env=None):
        full = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("SHARPEN_LLM", "OPENAI", "LOCAL_LLM", "GRUMPY"))
        }
        full.update({"STUB_OUT": self.out}, **(env or {}))
        return subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, script), *args],
            capture_output=True,
            text=True,
            cwd=self.repo,
            env=full,
        )

    def calls(self):
        if not os.path.exists(self.out):
            return []
        with open(self.out) as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def called(self):
        """The first oneshot.py call, or None."""
        calls = self.calls()
        return calls[0] if calls else None

    def test_imagine_runs_after_review_when_declared(self):
        caps = ("test", "lint", "typecheck", "review", "imagine")
        r = self.run_adapter(
            "openai_adapter.py",
            self.manifest(caps=caps, model="gpt-x"),
            "--grumpy",
            self.grumpy,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            [c["argv"] for c in self.calls()],
            [
                ["review", "--provider", "openai", "--model", "gpt-x"],
                ["imagine", "--provider", "openai", "--model", "gpt-x"],
            ],
        )

    def test_imagine_alone_needs_no_review(self):
        caps = ("test", "lint", "typecheck", "imagine")
        r = self.run_adapter(
            "local_llm_adapter.py",
            self.manifest(caps=caps),
            env={"GRUMPY_PLUGIN_ROOT": self.grumpy},
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            [c["argv"] for c in self.calls()],
            [["imagine", "--provider", "compatible"]],
        )

    def test_a_review_that_earns_nothing_still_runs_imagine(self):
        # Separate gates: a failed review must not cost the imagine pass.
        caps = ("test", "lint", "typecheck", "review", "imagine")
        r = self.run_adapter(
            "openai_adapter.py",
            self.manifest(caps=caps),
            "--grumpy",
            self.grumpy,
            env={"STUB_EXIT_review": "1"},
        )
        self.assertEqual(r.returncode, 1)
        self.assertEqual([c["argv"][0] for c in self.calls()], ["review", "imagine"])
        self.assertIn("review earned nothing (exit 1)", r.stderr)

    def test_a_setup_error_stops_before_imagine(self):
        caps = ("test", "lint", "typecheck", "review", "imagine")
        r = self.run_adapter(
            "openai_adapter.py",
            self.manifest(caps=caps),
            "--grumpy",
            self.grumpy,
            env={"STUB_EXIT_review": "2"},
        )
        self.assertEqual(r.returncode, 2)
        self.assertEqual([c["argv"][0] for c in self.calls()], ["review"])

    def test_openai_hands_review_to_oneshot(self):
        r = self.run_adapter(
            "openai_adapter.py", self.manifest(), "--grumpy", self.grumpy
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.called()["argv"], ["review", "--provider", "openai"])

    def test_local_uses_compatible_and_maps_legacy_env(self):
        r = self.run_adapter(
            "local_llm_adapter.py",
            self.manifest(),
            env={
                "GRUMPY_PLUGIN_ROOT": self.grumpy,
                "LOCAL_LLM_URL": "http://127.0.0.1:1/v1/chat/completions",
                "LOCAL_LLM_MODEL": "qwen",
            },
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        call = self.called()
        self.assertEqual(call["argv"], ["review", "--provider", "compatible"])
        self.assertEqual(
            call["env"]["SHARPEN_LLM_URL"], "http://127.0.0.1:1/v1/chat/completions"
        )
        self.assertEqual(call["env"]["SHARPEN_LLM_MODEL"], "qwen")
        self.assertIn("LOCAL_LLM_URL is deprecated", r.stderr)

    def test_manifest_model_is_passed_on(self):
        r = self.run_adapter(
            "openai_adapter.py", self.manifest(model="gpt-x"), "--grumpy", self.grumpy
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.called()["argv"][-2:], ["--model", "gpt-x"])
        self.assertNotIn("overrides", r.stderr)

    def test_variable_beating_the_manifest_model_is_said(self):
        r = self.run_adapter(
            "openai_adapter.py",
            self.manifest(model="gpt-x"),
            "--grumpy",
            self.grumpy,
            env={"SHARPEN_LLM_MODEL": "gpt-env"},
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("--model", self.called()["argv"])
        self.assertIn(
            "SHARPEN_LLM_MODEL=gpt-env overrides the manifest's provider.model (gpt-x)",
            r.stderr,
        )

    def test_gate_failure_skips_review_and_shows_the_output(self):
        path = self.manifest(
            {"test": "true", "lint": "echo nope >&2; exit 3", "typecheck": "true"}
        )
        r = self.run_adapter("openai_adapter.py", path, "--grumpy", self.grumpy)
        self.assertEqual(r.returncode, 1)
        self.assertIsNone(self.called())
        # The FAIL line carries the command, its exit code and its stderr, all
        # under the adapter's own tag.
        self.assertIn("[openai-adapter] FAIL lint", r.stderr)
        self.assertIn(
            "[openai-adapter]     $ echo nope >&2; exit 3 -> exit 3", r.stderr
        )
        self.assertIn("[openai-adapter]     nope", r.stderr)
        self.assertNotIn("[generic-adapter]", r.stderr)

    def test_review_failure_is_the_exit_code(self):
        r = self.run_adapter(
            "openai_adapter.py",
            self.manifest(),
            "--grumpy",
            self.grumpy,
            env={"STUB_EXIT": "1"},
        )
        self.assertEqual(r.returncode, 1)

    def test_missing_grumpy_is_a_setup_error(self):
        r = self.run_adapter("openai_adapter.py", self.manifest())
        self.assertEqual(r.returncode, 2)
        self.assertIn("GRUMPY_PLUGIN_ROOT", r.stderr)
        self.assertIn("grumpy 2.16 or newer", r.stderr)
        self.assertIsNone(self.called())

    def test_old_grumpy_is_named_by_path_not_called_missing(self):
        # A grumpy before 2.16 has no scripts/oneshot.py; the error names the
        # folder that was tried and the version it needs, so the operator
        # upgrades instead of re-setting a variable that was already right.
        old = os.path.join(self.repo, "grumpy-2.15")
        os.makedirs(os.path.join(old, "scripts"))
        r = self.run_adapter("openai_adapter.py", self.manifest(), "--grumpy", old)
        self.assertEqual(r.returncode, 2)
        self.assertIn(
            os.path.join(old, "scripts", "oneshot.py") + " not found", r.stderr
        )
        self.assertIn("grumpy 2.16 or newer", r.stderr)
        self.assertNotIn("grumpy not found", r.stderr)
        self.assertIsNone(self.called())

    def test_no_review_capability_runs_no_review(self):
        path = self.manifest(caps=("test", "lint", "typecheck"))
        r = self.run_adapter("openai_adapter.py", path, "--grumpy", self.grumpy)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(self.called())
        # Exit 0 with no review is said out loud, so the sdlc README's example
        # manifest (which declares review) and this one can't be confused.
        self.assertIn("declares no `review` or `imagine` capability", r.stderr)
        self.assertIn("no receipt was written", r.stderr)

    def test_no_review_capability_needs_no_grumpy(self):
        # The README promises exit 0 without `review`; a box with no grumpy
        # installed used to get "grumpy not found" first.
        path = self.manifest(caps=("test", "lint", "typecheck"))
        r = self.run_adapter("local_llm_adapter.py", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("declares no `review` or `imagine` capability", r.stderr)
        self.assertNotIn("grumpy", r.stderr.replace("grumpy's", ""))

    def test_trailing_grumpy_flag_is_a_usage_error(self):
        # A bare `--grumpy` used to read as root=None and override a valid
        # GRUMPY_PLUGIN_ROOT, failing as "grumpy not found".
        r = self.run_adapter(
            "openai_adapter.py",
            self.manifest(),
            "--grumpy",
            env={"GRUMPY_PLUGIN_ROOT": self.grumpy},
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("--grumpy needs a path", r.stderr)
        self.assertIsNone(self.called())

    def test_a_killed_review_is_exit_1_not_a_negative_code(self):
        r = self.run_adapter(
            "openai_adapter.py",
            self.manifest(),
            "--grumpy",
            self.grumpy,
            env={"STUB_EXIT": "kill"},
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("killed by signal 9", r.stderr)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
