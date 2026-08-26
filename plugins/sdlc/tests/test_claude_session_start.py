#!/usr/bin/env python3
"""Tests for the Claude SessionStart capability adapter."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
SCRIPT = os.path.join(SCRIPTS, "claude-session-start.py")
sys.path.insert(0, SCRIPTS)

spec = importlib.util.spec_from_file_location("claude_session_start", SCRIPT)
session_start = importlib.util.module_from_spec(spec)
spec.loader.exec_module(session_start)


def git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], capture_output=True, check=True)


def make_repo(branch="feat/x"):
    repo = tempfile.mkdtemp()
    git(repo, "init", "-q", "-b", branch)
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    return repo


class DetectCapabilitiesTest(unittest.TestCase):
    def test_base_capabilities_are_always_present(self):
        result = session_start.detect_capabilities(path_exists=lambda _path: False)
        self.assertEqual(result, ["lint", "plan", "ship", "test", "typecheck"])

    def test_detected_grumpy_skills_add_review_imagine_and_fix(self):
        result = session_start.detect_capabilities(path_exists=lambda _path: True)
        self.assertEqual(
            result,
            [
                "fix",
                "imagine",
                "lint",
                "plan",
                "review",
                "ship",
                "test",
                "typecheck",
            ],
        )

    def test_manifest_is_valid_v1_shape(self):
        manifest = session_start.build_manifest(["typecheck", "test"])
        self.assertEqual(manifest["protocol_version"], "1")
        self.assertEqual(manifest["provider"], {"name": "claude-code"})
        self.assertEqual(manifest["capabilities"], ["test", "typecheck"])
        self.assertEqual(manifest["x-source"], "claude-session-start")


class SessionStartCliTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self.manifest_path = os.path.join(
            self.repo, ".sharpen", "data", "capabilities.claude.json"
        )

    def clean_env(self):
        env = dict(os.environ)
        env.pop("SDLC_CAPABILITIES_PATH", None)
        return env

    def run_adapter(self, env=None):
        return subprocess.run(
            ["python3", SCRIPT],
            capture_output=True,
            cwd=self.repo,
            env=env or self.clean_env(),
        )

    def test_writes_valid_manifest_without_blocking_session(self):
        result = self.run_adapter()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout.decode(), "")
        with open(self.manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["protocol_version"], "1")
        self.assertEqual(manifest["provider"]["name"], "claude-code")
        self.assertIn("test", manifest["capabilities"])

    def test_existing_claude_manifest_root_remains_active_until_neutral_exists(self):
        legacy_dir = os.path.join(self.repo, ".claude", "data")
        os.makedirs(legacy_dir)
        legacy_path = os.path.join(legacy_dir, "capabilities.claude.json")
        result = self.run_adapter()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(os.path.exists(legacy_path))
        self.assertFalse(os.path.exists(self.manifest_path))

        os.makedirs(os.path.dirname(self.manifest_path))
        result = self.run_adapter()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(os.path.exists(self.manifest_path))

    def test_unwritable_manifest_path_is_non_blocking_but_visible(self):
        blocker = os.path.join(self.repo, "blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("not a directory")
        result = self.run_adapter(
            env=dict(
                os.environ,
                SDLC_CAPABILITIES_PATH=os.path.join(blocker, "manifest.json"),
            )
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("could not write capability manifest", result.stderr.decode())


class HookRegistrationTest(unittest.TestCase):
    def test_hooks_json_registers_claude_session_start_adapter(self):
        with open(
            os.path.join(HERE, "..", "hooks", "hooks.json"), encoding="utf-8"
        ) as f:
            hooks = json.load(f)["hooks"]
        session_start = hooks.get("SessionStart", [])
        commands = [
            hook["command"]
            for group in session_start
            for hook in group.get("hooks", [])
        ]
        self.assertIn(
            'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/claude-session-start.py"',
            commands,
        )


if __name__ == "__main__":
    unittest.main()
