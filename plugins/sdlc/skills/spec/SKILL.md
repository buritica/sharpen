---
name: spec
description: "Write a spec (problem, goals, BDD scenarios, success metrics) for a feature before implementation. Use before sdlc:plan for anything with open product decisions."
---

# Spec

Write a structured spec for a feature before any implementation begins. The spec defines *what* to build and *why* — `/sdlc:plan` defines *how*. The two commands compose: spec first, then plan.

## When to use

Use before `/sdlc:plan` when:
- The work has open product decisions (what to build, for whom, what "done" looks like)
- Multiple approaches are possible and need evaluation before coding
- Acceptance criteria need to be agreed on before implementation starts
- The feature affects user-facing behavior or API contracts

Skip when:
- The task is a bug fix with a clear expected behavior
- The change is purely internal (refactor, deps, tooling) with no behavior change
- A spec already exists for this feature

## Inputs

The argument is a feature description or issue number. If an issue number is given, fetch it:
```bash
gh issue view <number> --json title,body,labels,comments
```

## Step 1 — Open the worktree first

**Never spec on main.** Before any analysis, open a fresh worktree off `origin/main`:

```bash
git pull origin main
git worktree add -b feature/<short-name> .claude/worktrees/<short-name> origin/main
```

Pick `<short-name>` from the feature description: 2–4 kebab-cased words.

State the worktree path: `Worktree: .claude/worktrees/<short-name> on feature/<short-name>`

Everything below runs inside the worktree.

## Step 2 — Research context

Before writing, gather context from the codebase:
- Search for related existing commands, patterns, or prior art (`Grep`, `Glob`)
- Check if a spec or plan already exists: `ls .claude/sdlc/` across branches
- Look for related tests or interfaces that constrain the design

Don't over-research. One pass with targeted searches is enough.

## Step 3 — Write the spec

Produce a structured spec with these sections:

### Problem
One paragraph. What pain does this solve? Who feels it? Why now? Be concrete — name the actual failure mode, not a vague "improve DX."

### Goals
Concrete, testable outcomes. 3–5 bullets. Each starts with a verb and names an observable behavior:
- "Users can run `/sdlc:spec <description>` and receive a structured spec artifact."
- "The spec artifact saves to `.claude/sdlc/<branch>/spec.md`."

Not: "Improve the workflow." Not: "Make specs easier."

### Non-goals
What this explicitly won't do. 2–4 bullets. Prevents scope creep. If you're tempted to add a feature, put it here instead.

### Success metrics
How you'll know it worked. Behavioral or measurable. Not "users are happy" — write the test you'd run to confirm success:
- "Running the command on a real feature description produces a spec with all sections populated in under 60 seconds."
- "The dogfood run (spec of the command itself) passes."

### User scenarios (BDD)

For each key flow, one `Given/When/Then` block:

```
Given <starting state>
When <action or trigger>
Then <observable outcome>
```

Cover at minimum: happy path, an error or edge case, the handoff to `/sdlc:plan`.

### Open questions

Unresolved design decisions — things that must be decided before implementation starts. Write each as a question, not a statement. Flag the impact if it goes unresolved.

If you have a recommended answer, include it in parentheses: *(Recommendation: X)*

### References

Related commands, issues, prior art, or external specs. One line each.

## Step 4 — Persist the spec

```bash
BRANCH=$(git rev-parse --abbrev-ref HEAD)
GIT_ROOT=$(git rev-parse --show-toplevel)
ARTIFACT_DIR="$GIT_ROOT/.claude/sdlc/$BRANCH"
mkdir -p "$ARTIFACT_DIR"
```

Write to `$ARTIFACT_DIR/spec.md` using the Write tool.

After saving, output exactly:
```
Spec saved to .claude/sdlc/<branch>/spec.md

Review the open questions above, then run /sdlc:plan with this spec as the task description.
```

## Step 5 — Create task checklist

```
- [ ] resolve open questions
- [ ] run /sdlc:plan
```

## Rules

- **Never invent requirements.** If something is unclear, put it in Open Questions and stop.
- **Goals must be falsifiable.** "Improve DX" is not a goal. If you can't write a test for it, rewrite it.
- **Non-goals are as important as goals.** Omitting them invites scope creep in PR review.
- **The spec is a living document until `/sdlc:plan` starts.** After that, scope changes require a new spec.
- **Pause after saving.** Do not automatically proceed to `/sdlc:plan`. Open questions need human answers.

## Gotchas

- If the feature description is vague (one word, no context), ask one clarifying question before writing. A spec built on a misunderstood requirement wastes more time than the question takes.
- BDD scenarios describe user-observable behavior, not implementation details. "Given the spec.md file exists" is an implementation detail. "Given a feature has been specced" is behavior.
- `/sdlc:plan` auto-detects a `spec.md` in the same artifact directory and includes it as context. Write the spec there — don't pass it manually.
- The "open questions" section is not optional. If you have zero open questions, you either have perfect information or you haven't looked hard enough.
