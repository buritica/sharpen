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

`/sdlc:new` is for consumer repos, not this one; this repo is already scaffolded. The
managed **SDLC** block at the bottom of this file is `/sdlc:init` step 10's output — when
the toolchain changes, re-run `python3 plugins/sdlc/scripts/agents_md.py --root .` with the
new commands rather than editing inside the markers.

Review runs through `grumpy`, not by hand: `/grumpy:review` and `/grumpy:imagine` are gates
3 and 5, and `/grumpy:fix` is how their findings get resolved. Reviewing your own diff in
your head does not record a gate, by design.

If you edit `plugins/grumpy/commands/review.md` or `imagine.md`'s sub-agent prompt
contract (the compact `SEVERITY|file:line|text|FACT|ASPECT`/`DOMAIN` pipe format each
sub-agent returns), re-run the recall eval before merging:
`plugins/grumpy/tests/fixtures/` holds a fixture diff with 7 planted issues and a golden
answer key; `scripts/eval-grumpy-findings.py` scores a candidate findings run (JSON or raw
pipe-format output) against it — see `plugins/grumpy/tests/fixtures/README.md` for the
run steps. This is manual, not CI-enforced (no LLM calls in CI, per the stdlib-only
constraint below), so a prompt-wording change can silently regress recall if the eval is
skipped — recall must stay at 100% on the fixture, not just "look reasonable."

## Gates on this repo

The managed **SDLC** block at the bottom of this file is the contract; these are the
parts of it that are easy to get wrong here:

- **Instruction vs enforcement are different layers.** This file reminds the agent to run
  `/sdlc:gate`; it cannot block anything. The `sdlc` plugin's `enforce-sdlc-gates` hook is
  what blocks `gh pr create` when gates are incomplete.
- **Enforcement is opt-out per branch.** `auto-init-gate-cycle` arms a `small-medium`
  cycle on the first `git commit` to any non-default branch. Manual `/sdlc:gate --init
  tiny` *before* that commit takes precedence, because auto-init no-ops when a cycle
  exists. For a docs-only diff say that you classified it docs-only.
- **Batch fixes, reset once.** A reset clears gates 2–6, which only their skills can
  re-earn. Resetting per fix means running five skills per fix.
- **The installed plugin cache may lag this repo.** The chain runs whatever version is
  installed; if a branch changes a command, exercise its mechanics directly as the
  dogfood step and say in the PR body which installed versions ran the gates.

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

## Editing a command's frontmatter or body regenerates its SKILL.md

Every `commands/<name>.md` has a generated `skills/<name>/SKILL.md` (the cross-host format
Codex CLI, Gemini CLI, Cursor, and Copilot read — see `plugins/sdlc/README.md`, "Codex CLI
support"). After editing a command's `description`/`name` frontmatter or its body, run
`python3 scripts/generate-skill.py --write-all-in <plugin-dir>` before opening a PR.
`scripts/check-marketplace.py` fails if any `skills/*/SKILL.md` is missing or stale, and
this repo's own pre-merge hook (`scripts/pre-merge-check.sh`) blocks `gh pr create`/`gh pr
merge` on that failure — so a branch that edited a command before this check existed will
need one `--write-all-in` run the first time it rebases past this point.

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

<!-- sdlc:begin -->
## SDLC

Managed by `/sdlc:init` between the `sdlc:begin`/`sdlc:end` markers — re-running init updates this block and nothing else. Repo rules go outside the markers, in this file. `CLAUDE.md` includes this file via `@AGENTS.md`; hosts that read `AGENTS.md` directly (Codex, Gemini, Cursor, Copilot) get the same contract.

### Lifecycle
- `/sdlc:plan` before any code. It opens a worktree off `origin/main` — never work on `main` — on a `<prefix>/short-name` branch (`feature/`, `fix/`, `chore/`, `docs/`) and writes `.claude/sdlc/<branch>/plan.md`.
- `/sdlc:spec` only when there are open product decisions. Skip for a bug fix.
- `/sdlc:gate` before every PR. Gates run in order; fix failures before advancing; a code change after the chain completes resets it.
- `/sdlc:ship` pushes, opens the PR, and squash-merges once CI is green.

### Gates
- Tiers: **docs-only** (no executable files; tests/lint/typecheck vacuously satisfied), **tiny** (≤3 lines, no behavior change), **small-medium** (any code change — the default), **significant** (new behavior, new integration, >3 files or >200 lines).
- The `sdlc` hook arms a small-medium cycle on the first commit to a branch and blocks `gh pr create` until every gate is recorded. For docs-only or trivial changes run `/sdlc:gate --init tiny` **before** the first commit.
- Gates 2–6 are `/grumpy:simplify`, then `/grumpy:review` → `/grumpy:fix` → `/grumpy:imagine` → `/grumpy:fix`. Run the skills; reviewing the diff in your head records nothing.
- Never record a gate you did not run. Gates 2–6 can only be stamped by their skills.

### Commands
- test: `python3 scripts/run-tests.py`
- lint: `ruff format --check . && ruff check .`
- format: `ruff format .`
- typecheck: not configured — wire one and re-run `/sdlc:init`
- CI runs the same commands; `ci-pass` is the single required check on `main`.

### Pull requests
- Title: conventional-commit prefix (`feat`, `fix`, `chore`, `docs`, `refactor`) with a scope, under 70 characters.
- Body: `## Summary`, `## Verification`, and for small-medium+ a `## Confirmation` window with the checks that prove it worked; `Closes #N` when an issue exists.
- Squash merge only. Never force-push `main`. Never bypass hooks (`--no-verify` and friends are forbidden; fix the failure or ask).

### Artifacts (gitignored)
- `.claude/sdlc/<branch>/` — plan and spec. `.claude/grumpy/<branch>/` — review, imagine, simplify reports.
- `.sharpen/data/` — gate state, shared across worktrees. `.sharpen/simplify.json` is the one committed file under `.sharpen/` (the simplify policy config), when present.
<!-- sdlc:end -->
