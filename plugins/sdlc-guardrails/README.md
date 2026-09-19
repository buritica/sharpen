# sdlc-guardrails

Main-branch protection for the [sdlc](../sdlc) workflow, agent-side: it denies `git commit`/`git push` targeting `main`/`master`, opt-in per repo, aware of `git -C <path>` and `cd <path> &&` prefixes.

```sh
claude plugin marketplace add buritica/sharpen
claude plugin install sdlc-guardrails@sharpen
```

> **Gate-chain enforcement** (blocking `gh pr create` until gates pass) lives in the **[sdlc](../sdlc) plugin**, not here. It is pure-python and JSON-backed (`enforce-sdlc-gates.py`, reads the `.claude/data/gates.json` that `/sdlc:gate` writes). An earlier `enforce-gates` hook lived here but read a JSON format nothing wrote — it was a no-op and has been removed.

This hook is agent-side only: a `PreToolUse` `Bash` matcher in `hooks/hooks.json` that reads the tool call's JSON off stdin and denies before the shell command runs. It does not install a git hook, so it has no effect on a commit you make by hand outside whatever agent has this plugin installed. Mechanically this is the same `hooks.json` shape as `sdlc`'s own `enforce-sdlc-gates.py` — Codex CLI reads that file directly and its `PreToolUse` denials work identically to Claude Code's (see [`sdlc`'s README, "Codex CLI support"](../sdlc/README.md#codex-cli-support)) — but that has only been live-verified for `enforce-sdlc-gates.py`, not for this hook specifically. Until someone runs it on a real Codex (or other) session and checks, treat "works the same way there" as a reasonable bet, not a confirmed fact.

## Main-branch protection is opt-in

`block-main-commits` stays silent until a repo opts in, so it never gets in the way of personal or scratch repos where committing to `main` is fine. Turn it on where you run a trunk-based PR flow:

```
/guard status   # is the current repo protected?
/guard on       # protect this repo
/guard off      # stop protecting this repo
/guard list     # show every protected repo on this machine
```

Prefer everything-protected with exceptions? Flip the global default:

```
/guard default-on    # protect every repo; `/guard off` carves out exceptions
/guard default-off   # back to the shipped default (nothing protected)
```

State lives in a user-level config on this machine at `${CLAUDE_CONFIG_DIR:-~/.claude}/sdlc-guardrails.json`:

```json
{
  "protectMainDefault": false,
  "protectedRepos": ["/abs/path/to/repo"],
  "unprotectedRepos": []
}
```

`/guard` edits this file for you; you can also edit it directly. Repos are matched by absolute, symlink-resolved path.

### One-off override

To bypass protection for a single command without touching config, set `SDLC_ALLOW_MAIN=1` (also accepts `true`/`yes`/`on`):

```
SDLC_ALLOW_MAIN=1 git commit -m "..."
```

## pi (coding agent) support

pi has no Claude `PreToolUse` event, so this plugin ships a small pi extension
(`extensions/guardrails-hooks.ts`) that replicates the guard. It shells out to
the same `hooks/_block-main-commits.py` (single source of truth — no logic
reimplemented), feeding it the Claude PreToolUse payload on stdin and mapping
its exit code back to a pi decision:

- **deny** (exit 2) → an interactive confirmation with the reason; only an
  interactive human in the TUI can override. This is the pi analog of the
  `SDLC_ALLOW_MAIN=1` escape hatch surfacing as a prompt. Headless
  (print/rpc/json) mode has no human to ask, so it **fails closed** and blocks.
- `SDLC_ALLOW_MAIN=1` on the command itself still bypasses, exactly as in
  Claude.

Install as a pi package (`pi install git:...sharpen --dir plugins/sdlc-guardrails`,
or via the top-level bundle). `$CLAUDE_PLUGIN_ROOT` is injected into
`process.env` so the shared SKILL.md body runs unchanged. See
[`docs/pi.md`](../../docs/pi.md) for the full contract.

## Tests

The hook has a stdlib test suite (no pip install):

```
python3 plugins/sdlc-guardrails/tests/test_block_main.py
```

It builds throwaway repos and drives the real hook for every case: opt-in
silence, deny-with-guidance on protected main, the `feature/main-rework`
false positive, and the `SDLC_ALLOW_MAIN=1` escape. Run it before shipping any
change to the hook.

## Performance

Each PreToolUse hook is a single `python3` process invoked directly from
`hooks.json` (no bash wrapper) that reads the tool JSON, runs a regex, and
exits — roughly 15-20ms per Bash call on a warm machine. Dropping the former
`bash ... .sh` wrappers cut about a third off that. `python3` is used (not a
JS runtime) because it ships on every macOS/Linux box, so the guardrail can
never fail to run for lack of an installed interpreter.
