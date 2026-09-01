#!/usr/bin/env python3
"""
Tests for hook_out.deny()/warn()/emit()/notify().

Run: python3 plugins/sdlc/tests/test_hook_out.py
"""

import contextlib
import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)

import hook_out as ho  # noqa: E402


class PayloadTest(unittest.TestCase):
    def test_deny_builds_the_documented_pretooluse_shape(self):
        payload = ho.deny("nope")
        self.assertEqual(
            payload["hookSpecificOutput"]["permissionDecisionReason"], "nope"
        )
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(payload["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_warn_joins_messages_under_one_prefix(self):
        payload = ho.warn("prefix", "one", "two")
        self.assertEqual(
            payload["systemMessage"], "[gate] prefix: one\n[gate] prefix: two"
        )

    def test_emit_writes_json_to_stdout(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ho.emit(ho.deny("nope"))
        self.assertEqual(
            json.loads(out.getvalue())["hookSpecificOutput"][
                "permissionDecisionReason"
            ],
            "nope",
        )


class NotifyTest(unittest.TestCase):
    def test_surface_true_writes_stderr_and_returns_exit_2(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = ho.notify("prefix", "message", surface=True)
        self.assertEqual(code, 2)
        self.assertIn("[gate] prefix: message", err.getvalue())

    def test_surface_false_still_writes_stderr_but_returns_exit_0(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = ho.notify("prefix", "message", surface=False)
        self.assertEqual(code, 0)
        self.assertIn("[gate] prefix: message", err.getvalue())


if __name__ == "__main__":
    unittest.main()
