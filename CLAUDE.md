# sharpen plugin marketplace

This repo **is** the `sharpen` Claude Code plugin marketplace. It ships the SDLC tooling
(`sdlc`, `grumpy`, `sdlc-guardrails`), so it must **dogfood its own tools**: changes here go
through the same workflow the plugins enforce elsewhere. If a command is awkward to use on
this repo, that is a bug report about the command, not a reason to skip it.

## The workflow

| Step | Command | When |
|---|---|---|
| Scope it | `/sdlc:plan` | Before writing code. Classifies the change, names the files, opens a worktree. |
| Spec it | `/sdlc:spec` | Only when there are open product decisions. Skip for a bug fix. |
| Gate it | `/sdlc:gate` | Before every PR. See below. |
| Ship it | `/sdlc:ship` | Push, open the PR, squash merge. |

`/sdlc:audit` grades this repo's own pipeline against
[`plugins/sdlc/templates/spec.md`](plugins/sdlc/templates/spec.md). Run it when you touch
CI, and treat a FAIL here as more embarrassing than a FAIL anywhere else. `/sdlc:secrets`
owns the 1Password tiers. `/sdlc:test-gaps` and `/sdlc:test-critique` deepen gate 1 when a
change adds real behavior, because exit 0 proves the suite ran, not that it means anything.

This repo has no secret-scan job (P4 in the audit spec) by deliberate choice — the
gitleaks workflow it shipped with silently no-op'd without an undocumented
`GITLEAKS_LICENSE` secret, so it was removed rather than fixed. `/sdlc:audit` will flag
P4 as missing here; that's expected, not a regression to chase.

`/sdlc:new` and `/sdlc:init` are for consumer repos, not this one. This repo is already
scaffolded.

Review runs through `grumpy`, not by hand: `/grumpy:review` and `/grumpy:imagine` are gates
3 and 5, and `/grumpy:fix` is how their findings get resolved. Reviewing your own diff in
your head does not record a gate, by design.

## Run gates before every PR

```
/sdlc:gate
```

Mandatory for any change with executable code. Two layers, and they are **not** the same
thing:

- **This file instructs**: it reminds the agent to run `/sdlc:gate`. It cannot block
  anything; an agent can forget it.
- **The hook enforces**: the `sdlc` plugin's auto-loaded `enforce-sdlc-gates` hook blocks
  `gh pr create` when gates are incomplete.

Enforcement is **opt-out per branch**: the `auto-init-gate-cycle` PostToolUse hook starts a
`small-medium` cycle on the first `git commit` to any non-default branch. After that,
`gh pr create` is blocked until the chain completes.

For a lighter cycle (docs-only, or a genuinely tiny change), run `/sdlc:gate --init tiny`
*before* your first commit. Manual init takes precedence, because auto-init is idempotent
and no-ops when a cycle already exists.

For docs-only changes (no executable files in the diff), the `tiny` cycle applies: gates
1/7/8 are vacuously satisfied. Say that you classified it docs-only.

The review gates (2–6) are recorded **only** when their skill runs. `record-gate.py
--record grumpy-review` is refused by the hook and again by the store. A reset clears them
and they can only be re-earned by re-running the skills, so batch your fixes and reset
once, rather than resetting per fix.

## Plugin changes need a version bump in two places

A fix doesn't reach installed copies unless you bump the version in **both**:

1. `plugins/<name>/.claude-plugin/plugin.json`
2. `.claude-plugin/marketplace.json` (the matching entry)

**The bump must be in the same PR as the change, never a follow-up.** A merged PR without
it is live in the repo and undelivered to users. `scripts/check-marketplace.py` catches the
mismatch; CI runs it.

Note that `sdlc` and `grumpy` share a path contract (`.claude/sdlc/<branch>/` and
`.claude/grumpy/<branch>/`): grumpy's review reads a plan sdlc writes, and sdlc's gate chain
reads what grumpy writes. If you change that contract, both plugins move together; a skew
leaves one side reading a directory the other never wrote.

## Hook authoring

- Always quote `${CLAUDE_PLUGIN_ROOT}` in hook commands and paths.
- Do **not** build cross-plugin paths from `$CLAUDE_PLUGIN_ROOT` (e.g.
  `$CLAUDE_PLUGIN_ROOT/../other-plugin/...`). The plugin cache is version-nested
  (`cache/<marketplace>/<plugin>/<version>/`), so `../other-plugin` resolves to
  `<plugin>/other-plugin` and **never exists**. The read fails *open*, so the command
  proceeds from memory with no error. Detect sibling capabilities by command availability
  instead, the way `/sdlc:gate` detects `grumpy`.
- Each plugin's `hooks/hooks.json` is auto-loaded; do **not** also declare a `hooks` key in
  `plugin.json` (double-load error).

## Tests

```sh
python3 scripts/run-tests.py          # every plugin's suite
python3 scripts/check-marketplace.py  # registry consistency
```

Stdlib python only, so there is no runtime to install and enforcement works on any box with
`python3`.
