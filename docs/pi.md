# sharpen under pi

This repo's three plugins — `sdlc`, `grumpy`, `sdlc-guardrails` — also install
into the [pi coding agent](https://github.com/earendil-works/pi-coding-agent).
pi has no Claude Code `PreToolUse`/`PostToolUse`/`SessionStart` hook events and
no `Skill` tool, so the hooks are ported as pi **extensions** that translate pi
lifecycle events into the Claude hook payloads the existing pure-stdlib
`scripts/*.py` already read on stdin.

**The gate logic stays in Python.** Nothing here reimplements enforcement in
TypeScript. Each extension is a thin adapter: build the payload, spawn the
existing script, map its exit code / stderr / stdout back to a pi decision. The
repo's "pure stdlib" constraint holds — the adapters use only Node's built-in
`node:child_process`/`node:path`/`node:url`.

## Install

```sh
# the whole marketplace as one pi package
pi install git:https://github.com/your-org/sharpen

# or one plugin at a time (needed for grumpy's `audit` skill — see below)
pi install git:https://github.com/your-org/sharpen --dir plugins/sdlc
pi install git:https://github.com/your-org/sharpen --dir plugins/grumpy
pi install git:https://github.com/your-org/sharpen --dir plugins/sdlc-guardrails
```

Each plugin has its own `package.json` with a `pi` manifest (skills +
extensions); the top-level `package.json` is pi's analog of the
`.claude-plugin/marketplace.json` listing, bundling all three for a single
install. `scripts/check-marketplace.py` keeps both registries consistent.

## Event mapping

| Claude hook | pi event | adapter action |
|---|---|---|
| `SessionStart` | `session_start` | run `claude-session-start.py` (writes the capability manifest) |
| `PreToolUse` Bash | `tool_call` (bash) | run `enforce-sdlc-gates.py` then `block-direct-gate-record.py`; block on non-zero exit |
| `PostToolUse` Bash | `tool_result` (bash) | run `auto-init-gate-cycle.py`; surface its notice |
| `PreToolUse` Bash (guardrails) | `tool_call` (bash) | run `hooks/_block-main-commits.py`; block on non-zero exit |
| `PostToolUse` Skill | — | **no pi equivalent** (pi has no `Skill` tool) — see the attest fallback below |

## Replicating the gate confirmations

Claude shows an interactive modal when a `PreToolUse` hook denies a tool, and
only a human can override. pi replicates this with `ctx.ui.confirm`:

- **TUI mode**: the denial reason is shown in a confirmation dialog with an
  explicit human "proceed anyway" override. This is the pi analog of the
  `SDLC_ALLOW_MAIN=1` escape hatch surfacing as a prompt — a human decision,
  never a model decision.
- **Headless (print/rpc/json) mode**: there is no human to ask, so the gate
  **fails closed** — the tool call is blocked with the reason, `terminate: true`.
  The model can never self-bypass, matching the fail-closed posture the scripts
  enforce.

The deny contract is unchanged from Claude: exit code `2` = deny, reason on
stderr, a `{"permissionDecision":"deny",...}` envelope on stdout. The adapter
maps `exitCode !== 0` → block, and surfaces an allow-time
`{"systemMessage":...}` caveat via `ctx.ui.notify`.

## `$CLAUDE_PLUGIN_ROOT` and the shared SKILL.md bodies

The cross-host `SKILL.md` files reference `$CLAUDE_PLUGIN_ROOT/scripts/...`.
pi sets no equivalent env var, so each extension sets
`process.env.CLAUDE_PLUGIN_ROOT` (and `CLAUDE_PLUGIN_SCRIPTS`) to its plugin
root at load. pi's bash tool spawns shells inheriting `process.env`, so the
existing skill bodies run verbatim — no edits to the shared files.

## Known pi limitations

- **No `Skill` tool → skill-gated gates record via `--attest`.** Under Claude,
  `auto-record-skill-gate.py` stamps a gate when the actual skill finishes. pi
  has no `Skill` tool (skills are read like any file), so this hook cannot
  fire. The documented fallback already lives in the skill bodies:
  `python3 "$CLAUDE_PLUGIN_ROOT/scripts/record-gate.py" --attest <gate>
  --reason "<text>"`. That stamp is marked unverified in `--status`, exactly as
  in Claude — the loud, reason-required path, not a silent bypass.
- **Flat skill names → the `audit` collision.** pi skills are flat-named, so
  sdlc's `audit` and grumpy's `audit` cannot coexist in one pi package. The
  top-level bundle keeps sdlc's `audit` (the pipeline grader) and excludes
  grumpy's via a `!` glob. Install grumpy alone for its `audit` skill.
- **`$ARGUMENTS` in SKILL.md bodies** is a documented cross-host limitation (no
  shared arg convention); pi appends args as `User: <args>` text. Left as-is.

## Guardrails under pi

`sdlc-guardrails` is opt-in per repo (config at
`${CLAUDE_CONFIG_DIR:-~/.claude}/sdlc-guardrails.json`, or `/guard on`). The pi
extension shells out to the same `hooks/_block-main-commits.py`; a denied
main-branch commit shows the interactive override (TUI) or fails closed
(headless). `SDLC_ALLOW_MAIN=1` on the command itself still bypasses, exactly
as in Claude.

## Testing

The pi adapters are exercised the same way the Codex port was: a real repo
with an incomplete gate cycle, a real `gh pr create` attempt (blocked), and a
real first commit (auto-arms a cycle). The deny contract is verified by feeding
the exact payload the adapter sends to the script and asserting exit code 2.
