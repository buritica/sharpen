# Hook payload fixtures

Full PreToolUse / PostToolUse payloads, one host per directory, replayed through
the real hook scripts by [`../../test_hook_payloads.py`](../../test_hook_payloads.py).
The unit tests elsewhere hand-build minimal payloads (`tool_name` +
`tool_input`); these carry every field a host sends, so a hook that starts
depending on a field one host omits, or choking on one it adds, fails here.

`__REPO__` is replaced with the throwaway repo's path at test time.

## Provenance

Payloads are split by where they came from, and every test failure names the
provenance (`transcribed/codex`, `recorded/codex`):

- **`transcribed/<host>/`**: written by hand from the host's hook reference.
  Everything here today is transcribed.
- **`recorded/<host>/`**: captured from a live session. Empty so far. This is
  the marked place for real captures; the first ones are expected from a live
  Codex run.

| Host | Transcribed from | Notes |
|---|---|---|
| `claude` | Claude Code hooks reference (input fields for PreToolUse and PostToolUse on `Bash`) | `tool_response` is the Bash tool's `{stdout, stderr, interrupted, isImage}` object; it has no `is_error` |
| `codex` | Codex CLI hooks reference, plus the live check in [`plugins/sdlc/README.md`](../../../../plugins/sdlc/README.md#codex-cli-support) that the same `hooks/hooks.json` and `Bash` matcher fire on Codex | `tool_response` is transcribed as a plain string, which also exercises the hooks' non-dict path. `model` and `turn_id` are Codex-only extras |

pi has no fixtures here: its extensions build the payload themselves, and
[`../../pi/extensions.test.mjs`](../../pi/extensions.test.mjs) checks that
mapping end to end.

## Adding a recorded payload

1. Capture the hook's stdin in a live session (for example, a hook command
   that runs `tee /tmp/payload.json | python3 <the real hook>`), doing one of
   the transcribed scenarios: `gh pr create`, a hand-stamped skill gate, or a
   `git commit`.
2. Replace the repo path in it with `__REPO__`, and drop anything private
   (session ids can stay; they're random).
3. Save it as `recorded/<host>/<scenario>.json`, using the transcribed file's
   name, so the test knows which decision to expect. A name with no
   transcribed scenario fails the suite.
4. Note the host version in the commit message. Keep the transcribed file:
   the two together show when a host's real payload drifts from its docs.
