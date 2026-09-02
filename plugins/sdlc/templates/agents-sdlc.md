## SDLC

Managed by `/sdlc:init` between the `sdlc:begin`/`sdlc:end` markers — re-running init updates this block and nothing else. Repo rules go outside the markers, in this file. `CLAUDE.md` includes this file via `@AGENTS.md`; hosts that read `AGENTS.md` directly (Codex, Gemini, Cursor, Copilot) get the same contract.

### Lifecycle
- `/sdlc:plan` before any code. It opens a worktree off `origin/{default_branch}` — never work on `{default_branch}` — on a `<prefix>/short-name` branch (`feature/`, `fix/`, `chore/`, `docs/`) and writes `.claude/sdlc/<branch>/plan.md`.
- `/sdlc:spec` only when there are open product decisions. Skip for a bug fix.
- `/sdlc:gate` before every PR. Gates run in order; fix failures before advancing; a code change after the chain completes resets it.
- `/sdlc:ship` pushes, opens the PR, and squash-merges once CI is green.

### Gates
- Tiers: **docs-only** (no executable files; tests/lint/typecheck vacuously satisfied), **tiny** (≤3 lines, no behavior change), **small-medium** (any code change — the default), **significant** (new behavior, new integration, >3 files or >200 lines).
- The `sdlc` hook arms a small-medium cycle on the first commit to a branch and blocks `gh pr create` until every gate is recorded. For docs-only or trivial changes run `/sdlc:gate --init tiny` **before** the first commit.
- {grumpy_line}
- Never record a gate you did not run. Gates 2–6 can only be stamped by their skills.

### Commands
- test: {test_cmd}
- lint: {lint_cmd}
- format: {format_cmd}
- typecheck: {typecheck_cmd}
{deploy_line}- CI runs the same commands; `ci-pass` is the single required check on `{default_branch}`.

### Pull requests
- Title: conventional-commit prefix (`feat`, `fix`, `chore`, `docs`, `refactor`) with a scope, under 70 characters.
- Body: `## Summary`, `## Verification`, and for small-medium+ a `## Confirmation` window with the checks that prove it worked; `Closes #N` when an issue exists.
- Squash merge only. Never force-push `{default_branch}`. Never bypass hooks (`--no-verify` and friends are forbidden; fix the failure or ask).

### Artifacts (gitignored)
- `.claude/sdlc/<branch>/` — plan and spec. `.claude/grumpy/<branch>/` — review, imagine, simplify reports.
- `.sharpen/data/` — gate state, shared across worktrees. `.sharpen/simplify.json` is the one committed file under `.sharpen/` (the simplify policy config), when present.
