## SDLC

Managed by `/sdlc:init` between the `sdlc:begin`/`sdlc:end` markers — re-running init updates this block and nothing else. Repo rules go outside the markers, in this file. `CLAUDE.md` includes this file via `@AGENTS.md`; hosts that read `AGENTS.md` directly (Codex, Gemini, Cursor, Copilot) get the same contract.

### Lifecycle
- `/sdlc:plan` before any code. It opens a worktree off `origin/{default_branch}` — never work on `{default_branch}` — on a `<prefix>/short-name` branch (`feature/`, `fix/`, `chore/`, `docs/`) and writes `.sharpen/sdlc/<branch>/plan.md`.
- `/sdlc:spec` only when there are open product decisions. Skip for a bug fix.
- `/sdlc:gate` before every PR. Gates run in order; fix failures before advancing; a code change after the chain completes resets it.
- `/sdlc:ship` pushes, opens the PR, and squash-merges once CI is green.

### Gates
- Tiers: {tiers_line}. A docs-only diff (no executable files) uses the `tiny` cycle; its tests/lint/typecheck gates are vacuously satisfied — say that you classified it docs-only.
- The `sdlc` hook arms a small-medium cycle on the first commit to a branch and blocks opening a PR (`gh pr create`, `gh api`, `glab`, `hub`, MCP PR tools) until every gate is recorded; `/sdlc:gate` also installs a git pre-push hook that warns when a pushed branch isn't ready. For docs-only or trivial changes run `/sdlc:gate --init tiny` **before** the first commit.
- {grumpy_line}
- Never record a gate you did not run. Gates 2–6 can only be stamped by their skills.
- Each pass leaves a receipt; `record-gate.py --verify <pass>` records the gate from it (the Claude Code and pi hooks also sweep receipts at turn end). Commit before the pass. The chain is done when every gate shows recorded (re-run tests after gate 6, whose fix clears it): don't re-run review to check gate 4/6's own fixes. A commit after the last review gate makes the chain stale and blocks the PR; `record-gate.py --carry` carries the gates over a clean rebase, release bookkeeping, whitespace, safe docs, or a small comment fix or mechanical conflict resolution with `--reason`, and anything else needs a reset.

### Commands
- test: {test_cmd}
- lint: {lint_cmd}
- format: {format_cmd}
- typecheck: {typecheck_cmd}
{deploy_line}- CI runs these commands, and may run more; `ci-pass` is the single required check on `{default_branch}`. CI checks the code; it never reads gate state.

### Pull requests
- Title: conventional-commit prefix (`feat`, `fix`, `chore`, `docs`, `refactor`) with a scope, under 70 characters.
- Body: `## Summary` and `## Verification`; `Closes #N` when an issue exists. (Consumer repos that run a post-merge confirmation window own that in their own `.claude/skills/sdlc/SKILL.md` overlay — generic sdlc does not mandate one.)
- Squash merge only. Never force-push `{default_branch}`. Never bypass hooks (`--no-verify` and friends are forbidden; fix the failure or ask).

### Artifacts (gitignored)
- `.sharpen/sdlc/<branch>/` — plan and spec. `.sharpen/grumpy/<branch>/` — review, imagine, simplify reports and pass receipts. `.claude/worktrees/` — `/sdlc:plan` worktrees. Older checkouts may still hold plans and reports under `.claude/sdlc/` and `.claude/grumpy/`; sharpen reads them there but no longer writes there.
- `.sharpen/data/` — gate state, shared across worktrees. `.sharpen/simplify.json` (the simplify policy) and `.sharpen/models.yaml` (grumpy model overrides) are the committed files under `.sharpen/`, when present, so never ignore `.sharpen/` as a whole.
- Each of these folders ignores itself when sharpen creates it (a `.gitignore` holding `*` inside it); `/sdlc:init` also lists them in the repo's `.gitignore`.
