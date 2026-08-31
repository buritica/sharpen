#!/usr/bin/env python3
"""Tests for OpenAI and local LLM adapters."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, SCRIPTS)

import gate_store as gs  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(SCRIPTS, f"{name}.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ga = _load("generic_adapter")
oa = _load("openai_adapter")
la = _load("local_llm_adapter")


def git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], capture_output=True, check=True)


def make_repo(branch="feat/x"):
    repo = tempfile.mkdtemp()
    git(repo, "init", "-q", "-b", branch)
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    return repo


def make_manifest(adapter_name, capabilities_list, model=None):
    return {
        "protocol_version": "1",
        "provider": {
            "name": adapter_name,
            "agent": f"{adapter_name}-adapter",
            "model": model or "test-model",
        },
        "capabilities": capabilities_list,
    }


def write_manifest(path, manifest):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(manifest, f)


class MockLLMHandler(BaseHTTPRequestHandler):
    response_body = b'{"choices":[{"message":{"content":"[]"}}]}'
    status_code = 200
    received_body = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        MockLLMHandler.received_body = self.rfile.read(length)
        self.send_response(self.status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.response_body)

    def log_message(self, *args):
        pass


class MockLLMServer(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), MockLLMHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.port}/v1/chat/completions"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()


class OpenAIAdapterTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def test_no_key_falls_back_to_generic(self):
        manifest = make_manifest("openai", ["test", "lint", "typecheck"])
        results = [
            {
                "capability": "test",
                "command": "true",
                "exit_code": 0,
                "duration_s": 0.01,
                "stdout": "",
                "stderr": "",
            }
        ]
        # Remove key to force fallback
        old = os.environ.pop("OPENAI_API_KEY", None)
        try:
            report = oa.build_review_report(manifest, results, cwd=self.repo)
            self.assertEqual(report["status"], "fail")
            self.assertEqual(len(report["findings"]), 1)
            self.assertIn("OPENAI_API_KEY", report["findings"][0]["consequence"])
        finally:
            if old:
                os.environ["OPENAI_API_KEY"] = old

    def test_gate_failure_skips_review(self):
        manifest = make_manifest("openai", ["test", "lint", "typecheck"])
        results = [
            {
                "capability": "test",
                "command": "false",
                "exit_code": 1,
                "duration_s": 0.01,
                "stdout": "",
                "stderr": "boom",
            }
        ]
        report = oa.build_review_report(manifest, results, cwd=self.repo)
        self.assertEqual(report["status"], "fail")
        self.assertIn("test", report["findings"][0]["summary"])


class LocalLLMAdapterTest(MockLLMServer):
    def setUp(self):
        super().setUp()
        self.repo = make_repo()
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self.old_url = os.environ.get("LOCAL_LLM_URL")
        os.environ["LOCAL_LLM_URL"] = self.url
        MockLLMHandler.received_body = None

    def tearDown(self):
        super().tearDown()
        if self.old_url:
            os.environ["LOCAL_LLM_URL"] = self.old_url
        else:
            os.environ.pop("LOCAL_LLM_URL", None)
        MockLLMHandler.received_body = None

    def test_passing_review(self):
        manifest = make_manifest("local-llm", ["test", "lint", "typecheck"])
        results = [
            {
                "capability": "test",
                "command": "true",
                "exit_code": 0,
                "duration_s": 0.01,
                "stdout": "",
                "stderr": "",
            }
        ]
        self.assertIsNone(MockLLMHandler.received_body)
        report = la.build_review_report(manifest, results, cwd=self.repo)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["executor"]["adapter"], "local-llm")
        sent = json.loads(MockLLMHandler.received_body)
        self.assertEqual(sent["model"], "test-model")
        self.assertEqual(len(sent["messages"]), 1)
        user_content = sent["messages"][0]["content"]
        self.assertEqual(sent["messages"][0]["role"], "user")
        self.assertIn("You are a code reviewer", user_content)
        # The prompt must actually carry the diff section (not just the
        # instructions preamble) — this is what proves the request sent over
        # the wire is built from the real manifest/gate-results context
        # rather than a stubbed-out or truncated payload.
        self.assertIn("```diff", user_content)
        self.assertIn("Diff:", user_content)
        # make_repo() creates a fresh repo with a single empty commit, so
        # there is no real base...head range to diff against — the adapter's
        # diff extraction falls through to an empty diff. Assert that empty
        # case explicitly so a future change to the fallback message doesn't
        # silently swap in something else unnoticed.
        self.assertIn("(no diff)", user_content)

    def test_server_error_falls_back(self):
        self.server.shutdown()
        self.server.server_close()
        # Point at a dead port
        os.environ["LOCAL_LLM_URL"] = "http://127.0.0.1:1/v1/chat/completions"
        manifest = make_manifest("local-llm", ["test", "lint", "typecheck"])
        results = [
            {
                "capability": "test",
                "command": "true",
                "exit_code": 0,
                "duration_s": 0.01,
                "stdout": "",
                "stderr": "",
            }
        ]
        report = la.build_review_report(manifest, results, cwd=self.repo)
        self.assertEqual(report["status"], "fail")
        self.assertIn("delegated review failed", report["findings"][0]["summary"])

    def test_no_url_falls_back(self):
        os.environ.pop("LOCAL_LLM_URL", None)
        manifest = make_manifest("local-llm", ["test", "lint", "typecheck"])
        results = [
            {
                "capability": "test",
                "command": "true",
                "exit_code": 0,
                "duration_s": 0.01,
                "stdout": "",
                "stderr": "",
            }
        ]
        report = la.build_review_report(manifest, results, cwd=self.repo)
        self.assertEqual(report["status"], "fail")
        self.assertIn("LOCAL_LLM_URL", report["findings"][0]["consequence"])


class CliTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self.store_path = gs.default_store_path(self.repo)

    def run_cli(self, script, manifest_path):
        return subprocess.run(
            ["python3", os.path.join(SCRIPTS, script), manifest_path],
            capture_output=True,
            cwd=self.repo,
        )

    def test_openai_adapter_no_key_still_runs_gates(self):
        manifest = make_manifest(
            "openai",
            ["test", "lint", "typecheck"],
        )
        manifest["x-host-command-map"] = {
            "test": "true",
            "lint": "true",
            "typecheck": "true",
        }
        path = os.path.join(self.repo, "caps.json")
        write_manifest(path, manifest)
        data = {}
        gs.init_gates(data, "feat/x", "tiny")
        os.makedirs(os.path.dirname(self.store_path), exist_ok=True)
        with open(self.store_path, "w") as f:
            json.dump(data, f)
        old = os.environ.pop("OPENAI_API_KEY", None)
        try:
            r = self.run_cli("openai_adapter.py", path)
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            with open(self.store_path) as f:
                store = json.load(f)
            # Review should have fallen back and marked as fail
            self.assertEqual(store["feat/x"]["review_report"]["status"], "fail")
        finally:
            if old:
                os.environ["OPENAI_API_KEY"] = old

    def test_local_llm_adapter_with_mock_server(self):
        old_url = os.environ.get("LOCAL_LLM_URL")
        os.environ["LOCAL_LLM_URL"] = "http://127.0.0.1:1/v1/chat/completions"  # dead
        manifest = make_manifest("local-llm", ["test", "lint", "typecheck"])
        manifest["x-host-command-map"] = {
            "test": "true",
            "lint": "true",
            "typecheck": "true",
        }
        path = os.path.join(self.repo, "caps.json")
        write_manifest(path, manifest)
        data = {}
        gs.init_gates(data, "feat/x", "tiny")
        os.makedirs(os.path.dirname(self.store_path), exist_ok=True)
        with open(self.store_path, "w") as f:
            json.dump(data, f)
        try:
            r = self.run_cli("local_llm_adapter.py", path)
            # A delegated-review failure is attached for evidence but must fail
            # the adapter rather than reporting a false successful gate run.
            self.assertEqual(r.returncode, 1, r.stderr.decode())
            with open(self.store_path) as f:
                store = json.load(f)
            self.assertEqual(store["feat/x"]["review_report"]["status"], "fail")
        finally:
            if old_url:
                os.environ["LOCAL_LLM_URL"] = old_url
            else:
                os.environ.pop("LOCAL_LLM_URL", None)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
