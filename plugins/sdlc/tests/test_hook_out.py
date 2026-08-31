#!/usr/bin/env python3
"""
Host-aware behavior of hook_out.deny()/warn()/emit().

Only `deny()`'s stdout envelope is opt-out via SDLC_HOOK_HOST, not inferred:
nothing about a hook subprocess's environment reliably signals "this is
Claude Code" (see hook_out.py's docstring), so a non-"claude" host must be
declared explicitly by whoever wires the hook — e.g. codex-hooks.json setting
SDLC_HOOK_HOST=codex. Default stays "claude" so every existing caller and
test is unaffected. `deny()` is safe to gate this way because every call site
already writes its reason to stderr independently before exit 2 — dropping
the envelope loses decoration, not the message.

`warn()` is deliberately NEVER host-gated: some call sites use `systemMessage`
as their only channel at all (see hook_out.py's module docstring), so
dropping it on an unrecognized host would turn a marginal channel into no
channel — worse than emitting to a host that might not render it.

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


class HostAwareEnvelope(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.pop("SDLC_HOOK_HOST", None)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("SDLC_HOOK_HOST", None)
        else:
            os.environ["SDLC_HOOK_HOST"] = self._prev

    def test_deny_builds_the_envelope_by_default(self):
        payload = ho.deny("nope")
        self.assertEqual(
            payload["hookSpecificOutput"]["permissionDecisionReason"], "nope"
        )

    def test_deny_builds_the_envelope_when_host_is_claude(self):
        os.environ["SDLC_HOOK_HOST"] = "claude"
        payload = ho.deny("nope")
        self.assertEqual(
            payload["hookSpecificOutput"]["permissionDecisionReason"], "nope"
        )

    def test_deny_returns_none_for_a_declared_non_claude_host(self):
        os.environ["SDLC_HOOK_HOST"] = "codex"
        self.assertIsNone(ho.deny("nope"))

    def test_deny_host_declaration_is_case_insensitive_and_trims_whitespace(self):
        os.environ["SDLC_HOOK_HOST"] = " Codex \n"
        self.assertIsNone(ho.deny("nope"))

    def test_warn_always_builds_the_envelope_regardless_of_host(self):
        payload = ho.warn("prefix", "one", "two")
        self.assertEqual(
            payload["systemMessage"], "[gate] prefix: one\n[gate] prefix: two"
        )
        os.environ["SDLC_HOOK_HOST"] = "codex"
        payload = ho.warn("prefix", "one", "two")
        self.assertEqual(
            payload["systemMessage"], "[gate] prefix: one\n[gate] prefix: two"
        )

    def test_emit_writes_nothing_for_none_payload(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ho.emit(None)
        self.assertEqual(out.getvalue(), "")

    def test_emit_writes_a_warn_envelope_regardless_of_host(self):
        os.environ["SDLC_HOOK_HOST"] = "codex"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ho.emit(ho.warn("prefix", "one"))
        self.assertEqual(
            json.loads(out.getvalue())["systemMessage"], "[gate] prefix: one"
        )

    def test_emit_writes_nothing_when_deny_opted_out_on_a_non_claude_host(self):
        os.environ["SDLC_HOOK_HOST"] = "codex"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ho.emit(ho.deny("nope"))
        self.assertEqual(out.getvalue(), "")

    def test_emit_still_writes_json_for_a_real_payload(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ho.emit({"a": 1})
        self.assertEqual(json.loads(out.getvalue()), {"a": 1})


if __name__ == "__main__":
    unittest.main()
