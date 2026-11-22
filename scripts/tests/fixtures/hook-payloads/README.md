# Hook payload fixtures

Full PreToolUse / PostToolUse payloads, one host per directory, replayed through
the real hook scripts by [`../../test_hook_payloads.py`](../../test_hook_payloads.py).
The unit tests elsewhere hand-build minimal payloads (`tool_name` +
`tool_input`); these carry every field a host sends, so a hook that starts
depending on a field one host omits, or choking on one it adds, fails here.

`__REPO__` is replaced with the throwaway repo's path at test time.

## Provenance

None of these are recorded captures yet. All are **transcribed**:

| Host | Source | Notes |
|---|---|---|
| `claude/` | Claude Code hooks reference (input fields for PreToolUse and PostToolUse on `Bash`) | `tool_response` is the Bash tool's `{stdout, stderr, interrupted, isImage}` object; it has no `is_error` |
| `codex/` | Codex CLI hooks reference, plus the live check in [`plugins/sdlc/README.md`](../../../../plugins/sdlc/README.md#codex-cli-support) that the same `hooks/hooks.json` and `Bash` matcher fire on Codex | `tool_response` is transcribed as a plain string, which also exercises the hooks' non-dict path. `model` and `turn_id` are Codex-only extras |

pi has no fixtures here: its extensions build the payload themselves, and
[`../pi/extensions.test.mjs`](../pi/extensions.test.mjs) checks that mapping
end to end.

When a payload is captured from a live session, replace the file and change
its row above to say **recorded**, with the host version. The trigger for the
Codex row is the next live Codex verification of the hooks.
