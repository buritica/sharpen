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
with open(os.environ["STUB_OUT"], "w") as f:
    json.dump({"argv": sys.argv[1:], "env": {k: v for k, v in os.environ.items()
               if k.startswith(("SHARPEN_LLM", "OPENAI"))}}, f)
code = os.environ.get("STUB_EXIT", "0")
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

    def called(self):
        if not os.path.exists(self.out):
            return None
        with open(self.out) as f:
            return json.load(f)

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

    def test_gate_failure_skips_review(self):
        path = self.manifest({"test": "true", "lint": "false", "typecheck": "true"})
        r = self.run_adapter("openai_adapter.py", path, "--grumpy", self.grumpy)
        self.assertEqual(r.returncode, 1)
        self.assertIsNone(self.called())

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
        self.assertIsNone(self.called())

    def test_no_review_capability_runs_no_review(self):
        path = self.manifest(caps=("test", "lint", "typecheck"))
        r = self.run_adapter("openai_adapter.py", path, "--grumpy", self.grumpy)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(self.called())
        # Exit 0 with no review is said out loud, so the sdlc README's example
        # manifest (which declares review) and this one can't be confused.
        self.assertIn("declares no `review` capability", r.stderr)
        self.assertIn("no receipt was written", r.stderr)

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
