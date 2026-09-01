---
name: plan
description: "Analyze scope and plan implementation for a task. Use before starting any code change to classify the work, identify affected files, and propose an approach."
---

# Plan

Open an isolated worktree, then analyze the task, classify its size, and produce an implementation plan before writing any code.

## Inputs

The argument is a task description or issue number. If an issue number is given, fetch it:
```bash
gh issue view <number> --json title,body,labels
```

If the task has open product decisions (what to build, for whom, what "done" looks like), run `/sdlc:spec` first and use its output as the task description here.

## Step 1 — Open the worktree first

**Never plan on main.** Before any analysis, open a fresh worktree off `origin/main`. The plan artifact, all subsequent edits, and the gate chain all live inside it.

Pick a branch name from the task description:
- Prefix: `feature/` (new capability), `fix/` (bug), `chore/` (refactor/deps/tooling), `docs/` (documentation).
- Short-name: 2–4 kebab-cased words capturing the essence of the task.

```bash
git fetch origin main -q
git worktree add -b <prefix>/<short-name> .claude/worktrees/<short-name> origin/main
cd .claude/worktrees/<short-name>
```

`git fetch` (not `git pull`) is deliberate: it updates the `origin/main` remote-tracking ref without needing a working tree, without touching local `main`, and without merging into whatever branch you happen to be on. The worktree then branches off `origin/main` directly, so local `main` staleness is irrelevant — **never use bare `main` as the base**. If fetch fails, stop and resolve before proceeding.

If you're already inside a worktree off the right base, skip this step. If you're on main with uncommitted changes, **stop and ask the user how to handle them** before moving — do not silently discard or carry them across.

State the worktree path in one line: `Worktree: .claude/worktrees/<short-name> on <prefix>/<short-name>`.

Everything below runs inside the worktree.

## Step 2 — Classify the tier

Read the task and classify:

| Tier | Criteria |
|------|----------|
| **Docs-only** | Only non-executable files changed (`.md`, text, images, prompt/markdown plugin files), any size. No code touched. |
| **Tiny** | ≤3 lines of code, or pure comments/formatting. No behavior change. |
| **Small-medium** | Any code change, refactor, config edit. Default when uncertain. |
| **Significant** | New user-facing behavior, new integration, >3 files or >200 lines. |

State the tier explicitly: `Tier: <name> — <one-line rationale>`

When uncertain, step up a tier. Under-classification ships bugs; over-classification wastes tokens. **Docs-only is the exception to "step up":** a 500-line markdown doc with zero executable changes is still Docs-only — size does not promote it. But the moment the diff touches one executable file, it is at least Tiny.

### Docs-only handling

When the diff touches no executable files, the code gates have nothing to verify and are **vacuously satisfied**. Record `lint`, `typecheck`, and `tests` directly (there is no executable change to lint, type-check, or break), skip the `simplify`/grumpy chain entirely, and go straight to ship. This decouples documentation size from code rigor: a large prose change is not forced through a code-review chain it can't meaningfully use.

The gate command verifies this at run time with `git diff --name-only origin/main...HEAD`; at plan time, classify based on what you expect to change.

## Step 3 — Identify scope

Search the codebase to identify:
- Files that need changes (use Grep/Glob to find them)
- Files that test the changed code
- Files that import or depend on changed code (blast radius)

List each with a one-line description of what changes.

## Step 4 — Propose approach

For **Tiny**: skip this step, go straight to implementation.

For **Small-medium**:
- One paragraph describing the approach
- List the test cases (TDD: what failing tests will you write first?)
- Flag any risks or unknowns

For **Significant**:
- Problem statement (2-3 sentences)
- Proposed approach with concrete steps
- Test strategy (what to test, edge cases)
- Risks and mitigations
- Verification plan (how to confirm it works post-merge)

## Step 5 — Create the task checklist

Use your harness's task-tracking feature if it has one (one task per gate) — otherwise keep a plain checklist in your working notes as an invariant that survives context resets.

For **Docs-only** (gates vacuously satisfied — no executable change):
```
- [ ] implement (write the docs/content)
- [ ] tests
- [ ] lint
- [ ] typecheck
- [ ] ship
```

For **Tiny**:
```
- [ ] implement (TDD: failing test → pass → refactor)
- [ ] tests
- [ ] lint
- [ ] typecheck
- [ ] ship
```

For **Small-medium+**:
```
- [ ] implement (TDD: failing test → pass → refactor)
- [ ] tests
- [ ] simplify
- [ ] review
- [ ] fix (post-review)
- [ ] imagine
- [ ] fix (post-imagine)
- [ ] lint
- [ ] typecheck
- [ ] ship
```

Check items off as each gate passes. When resuming interrupted work, check todos first.

## Step 6 — Persist the plan

Save the plan so it survives context compaction:

```bash
BRANCH=$(git rev-parse --abbrev-ref HEAD)
GIT_ROOT=$(git rev-parse --show-toplevel)
ARTIFACT_DIR="$GIT_ROOT/.claude/sdlc/$BRANCH"
mkdir -p "$ARTIFACT_DIR"
```

If `$ARTIFACT_DIR/spec.md` exists (written by `/sdlc:spec`), include its Goals and User scenarios in the Approach section as acceptance criteria.

Write the tier classification, scope analysis, and approach (Steps 2–4 output) to `$ARTIFACT_DIR/plan.md` using the Write tool. Include a `## Progress` section and a `## Notes` section at the end — these are updated by other skills as work proceeds.

**Progress section** — tracks the formal gate chain (starts at `tests`). The `implement` and `ship` steps from the Step 5 task checklist are not here: implementation is pre-gate work, and ship is handled by `/sdlc:ship`. Use the tier-appropriate list:

For **Docs-only**:
```markdown
## Progress
- [ ] tests
- [ ] lint
- [ ] typecheck
```

For **Tiny**:
```markdown
## Progress
- [ ] tests
- [ ] lint
- [ ] typecheck
```

For **Small-medium+**:
```markdown
## Progress
- [ ] tests
- [ ] simplify
- [ ] review
- [ ] fix (post-review)
- [ ] imagine
- [ ] fix (post-imagine)
- [ ] lint
- [ ] typecheck
```

Always end the plan with:
```markdown
## Notes
```

The Notes section starts empty. `/grumpy:review` and `/grumpy:imagine` append their findings here as the gate chain runs. This turns the plan into a decision log.

When resuming interrupted work, check `$ARTIFACT_DIR/plan.md` before re-planning.

## Rules

- **TDD strict.** Every code change starts with a failing test. No exceptions.
- **Never commit on main.** All changes go through worktrees and PRs.
- **Plan depth matches tier.** Don't over-plan Tiny work. Don't under-plan Significant work.
- When resuming interrupted work: check `git status` + `git diff --stat` in the worktree first. Uncommitted work is invisible to `git log`.

## Gotchas

- Never plan on main. The worktree must be created BEFORE any analysis, not after.
- When resuming interrupted work, check `git status` + `git diff --stat` first. Zero commits does not mean zero progress — uncommitted files contain the work.
- Tier under-classification is worse than over-classification. "No logic change" is the most common misclassification — new guards, cleanup handlers, and early returns are all logic changes.
- The plan artifact at `.claude/sdlc/$BRANCH/plan.md` survives context compaction. Always check if one exists before re-planning.
