#!/usr/bin/env python3
"""Tests for the Claude SessionStart capability adapter."""

import contextlib
import glob
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

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


def _touch(*parts):
    path = os.path.join(*parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("")
    return path


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


class RealLayoutDetectionTest(unittest.TestCase):
    """Regression coverage against actual on-disk layouts, not the
    `path_exists` stub above — that stub says nothing about whether the
    *path being checked* is even correct. Confirmed live against an
    installed Codex CLI plugin cache: the previous single
    `${CLAUDE_PLUGIN_ROOT}/../grumpy/...` join resolved to
    `cache/sharpen/sdlc/grumpy/...` (nested inside sdlc's own directory, one
    level too shallow) and never matched, so `review`/`imagine`/`fix` were
    silently absent from every real manifest despite grumpy being installed
    — invisible to the stubbed test above, which never exercises real path
    construction at all."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def test_flat_dev_checkout_layout(self):
        # plugins/sdlc and plugins/grumpy as direct siblings.
        sdlc_root = os.path.join(self.tmpdir, "plugins", "sdlc")
        _touch(self.tmpdir, "plugins", "grumpy", "skills", "review", "SKILL.md")
        os.makedirs(sdlc_root, exist_ok=True)
        result = session_start.detect_capabilities(plugin_root=sdlc_root)
        self.assertIn("review", result)
        self.assertNotIn("imagine", result)  # only review's SKILL.md exists
        self.assertNotIn("fix", result)

    def test_version_nested_installed_cache_layout(self):
        # cache/<marketplace>/<plugin>/<version>/ — sdlc and grumpy version
        # independently, so grumpy's version segment differs from sdlc's.
        sdlc_root = os.path.join(self.tmpdir, "cache", "sharpen", "sdlc", "4.10.0")
        os.makedirs(sdlc_root, exist_ok=True)
        _touch(
            self.tmpdir,
            "cache",
            "sharpen",
            "grumpy",
            "2.4.0",
            "skills",
            "imagine",
            "SKILL.md",
        )
        result = session_start.detect_capabilities(plugin_root=sdlc_root)
        self.assertIn("imagine", result)
        self.assertNotIn("review", result)
        self.assertNotIn("fix", result)

    def test_version_nested_layout_with_no_grumpy_installed(self):
        sdlc_root = os.path.join(self.tmpdir, "cache", "sharpen", "sdlc", "4.10.0")
        os.makedirs(sdlc_root, exist_ok=True)
        result = session_start.detect_capabilities(plugin_root=sdlc_root)
        self.assertNotIn("review", result)
        self.assertNotIn("imagine", result)
        self.assertNotIn("fix", result)

    def test_sdlc_grumpy_root_env_override_unblocks_an_unrecognized_layout(self):
        # A layout neither candidate above finds — the escape hatch this
        # bug report exists for: fix it via an env var, not a new release.
        sdlc_root = os.path.join(self.tmpdir, "somewhere", "unrecognized", "sdlc")
        grumpy_root = os.path.join(self.tmpdir, "elsewhere", "grumpy")
        os.makedirs(sdlc_root, exist_ok=True)
        _touch(grumpy_root, "skills", "fix", "SKILL.md")
        with mock.patch.dict(os.environ, {"SDLC_GRUMPY_ROOT": grumpy_root}):
            result = session_start.detect_capabilities(plugin_root=sdlc_root)
        self.assertIn("fix", result)
        self.assertNotIn("review", result)

    def test_debug_env_var_reports_which_candidate_matched(self):
        # The observability gap that made this bug take a live debugging
        # session to find: with no SDLC_DEBUG, troubleshooting "why isn't
        # review showing up" has nothing to go on but re-deriving the paths
        # by hand, same as this bug's own investigation had to.
        sdlc_root = os.path.join(self.tmpdir, "plugins", "sdlc")
        _touch(self.tmpdir, "plugins", "grumpy", "skills", "review", "SKILL.md")
        os.makedirs(sdlc_root, exist_ok=True)

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            session_start.detect_capabilities(plugin_root=sdlc_root)
        self.assertEqual(stderr.getvalue(), "")  # quiet by default

        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"SDLC_DEBUG": "1"}):
            with contextlib.redirect_stderr(stderr):
                session_start.detect_capabilities(plugin_root=sdlc_root)
        output = stderr.getvalue()
        self.assertIn("'review'", output)
        self.assertIn("matched", output)
        self.assertIn("no match", output)  # imagine/fix, neither installed


class WriteManifestConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.path = os.path.join(self.tmpdir, "capabilities.claude.json")

    def test_concurrent_write_manifest_calls_do_not_corrupt_the_output(self):
        # Drive the actual write_manifest function (not a hand-rolled
        # stand-in) from multiple threads racing on the same manifest path,
        # the way two SessionStart hooks starting at once would. With the
        # old fixed "<path>.tmp" name this could read/clobber a partial
        # write; mkstemp's kernel-unique name means every writer's temp
        # file is exclusive to it, so the final manifest is always one
        # writer's complete, valid output, never a mix of two.
        errors = []

        def write(n):
            try:
                session_start.write_manifest(
                    self.path, session_start.build_manifest([f"cap-{n}"])
                )
            except Exception as e:  # noqa: BLE001 - surfaced via `errors`
                errors.append(e)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        with open(self.path, encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(len(manifest["capabilities"]), 1)
        self.assertTrue(manifest["capabilities"][0].startswith("cap-"))
        leftover = glob.glob(os.path.join(self.tmpdir, "capabilities-*.tmp"))
        self.assertEqual(leftover, [])

    def test_write_manifest_cleans_up_temp_file_when_replace_fails(self):
        manifest = session_start.build_manifest(["test"])
        with mock.patch.object(
            session_start.os, "replace", side_effect=OSError("boom")
        ):
            with self.assertRaises(OSError):
                session_start.write_manifest(self.path, manifest)
        self.assertFalse(os.path.exists(self.path))
        leftover = glob.glob(os.path.join(self.tmpdir, "capabilities-*.tmp"))
        self.assertEqual(leftover, [])

    def test_write_manifest_leaves_no_temp_file_behind(self):
        manifest = session_start.build_manifest(["test"])
        session_start.write_manifest(self.path, manifest)
        self.assertFalse(os.path.exists(f"{self.path}.tmp"))
        leftover = glob.glob(os.path.join(self.tmpdir, "capabilities-*.tmp"))
        self.assertEqual(leftover, [])
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["capabilities"], ["test"])

    def test_write_manifest_cleans_up_temp_file_when_dump_fails(self):
        manifest = {"not": "json-serializable", "bad": object()}
        with self.assertRaises(TypeError):
            session_start.write_manifest(self.path, manifest)
        self.assertFalse(os.path.exists(self.path))
        leftover = glob.glob(os.path.join(self.tmpdir, "capabilities-*.tmp"))
        self.assertEqual(leftover, [])


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

    def read_manifest(self, path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def assert_manifest_overwritten(self, path):
        # Seed with a real (but invalid) file rather than just creating the
        # parent directory: state_file_path() gates on file existence, not
        # directory existence, so this is what actually exercises the
        # "already-active" branch instead of the "created fresh" one.
        with open(path, "w", encoding="utf-8") as f:
            json.dump({}, f)
        result = self.run_adapter()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        manifest = self.read_manifest(path)
        self.assertEqual(
            manifest.get("protocol_version"),
            "1",
            f"{path} was not overwritten by the adapter",
        )
        return manifest

    def test_writes_valid_manifest_without_blocking_session(self):
        result = self.run_adapter()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout.decode(), "")
        manifest = self.read_manifest(self.manifest_path)
        self.assertEqual(manifest["protocol_version"], "1")
        self.assertEqual(manifest["provider"]["name"], "claude-code")
        self.assertIn("test", manifest["capabilities"])

    def test_existing_claude_manifest_root_remains_active_until_neutral_exists(self):
        # Fallback precedence mirrors gate_store.state_file_path (see
        # test_gate.py's test_legacy_fallback_is_per_file for the per-file
        # trigger itself): once a legacy file exists, it stays active until a
        # neutral file appears.
        legacy_dir = os.path.join(self.repo, ".claude", "data")
        os.makedirs(legacy_dir)
        legacy_path = os.path.join(legacy_dir, "capabilities.claude.json")
        legacy_content_after_first_phase = self.assert_manifest_overwritten(legacy_path)
        self.assertFalse(os.path.exists(self.manifest_path))

        os.makedirs(os.path.dirname(self.manifest_path))
        self.assert_manifest_overwritten(self.manifest_path)
        self.assertEqual(
            self.read_manifest(legacy_path),
            legacy_content_after_first_phase,
            "legacy manifest should be untouched once the neutral path takes over",
        )

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
