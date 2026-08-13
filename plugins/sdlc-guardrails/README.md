# sdlc-guardrails

Main-branch protection for the [sdlc](../sdlc) workflow, agent-side (it acts on Claude Code tool calls, not on your shell):

- **block-main-commits** — denies `git commit`/`git push` targeting `main`/`master`. **Opt-in per repo** (see below). Target-aware: respects `git -C <path>` and `cd <path> &&` prefixes.

> **Gate-chain enforcement** (blocking `gh pr create` until gates pass) lives in the **[sdlc](../sdlc) plugin**, not here. It is pure-python and JSON-backed (`enforce-sdlc-gates.py`, reads the `.claude/data/gates.json` that `/sdlc:gate` writes). An earlier `enforce-gates` hook lived here but read a JSON format nothing wrote — it was a no-op and has been removed.

This hook is agent-side only. It reads JSON from stdin (Claude Code hook protocol) and acts on Claude's tool calls; it does not install git hooks or affect commits you make by hand outside Claude Code.

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
