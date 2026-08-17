---
description: "Inspect the current diff and route automatically to the most relevant grumpy review mode(s); for broad changes fans out and synthesizes results."
argument-hint: "[--level grumpy|grumpier|linus] [--worktree <path>] [--dry-run]"
allowed-tools: ["Bash", "Read", "Grep", "Glob", "TaskCreate", "TaskUpdate"]
---

# /grumpy:dispatch

Smart router: inspect the current diff and dispatch to the grumpy mode(s) best
suited to the change. For a pure security patch it runs `/grumpy:security`. For
a new subsystem it runs `/grumpy:architecture`. For broad changes it fans out to
multiple modes and synthesizes the results so you get one unified report without
having to remember which review to run.

## Worktree targeting

Detect `--worktree <path>` (alias `--path <path>`) from `$ARGUMENTS`; if
present, remove it from `$ARGUMENTS` and set `WT` to that path. Otherwise `WT`
is the current directory. Pass `--worktree "$WT"` through to every dispatched
command so they all target the same tree.

## Level passthrough

Detect `--level <value>` from `$ARGUMENTS`. If present, remove it and store the
value. Pass `--level <value>` through to every dispatched command unchanged.
Default is `grumpy`.

## Dry-run mode

If `--dry-run` is present in `$ARGUMENTS`, print the routing decision and the
list of modes that would be invoked, then stop. Do not run any review.

## Step 1: Collect the diff

```bash
WT="${WT:-.}"
# Resolve the default branch instead of assuming "main" (could be master/develop).
BASE=$(git -C "$WT" symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||')
BASE="${BASE:-$(git -C "$WT" rev-parse --verify -q main >/dev/null 2>&1 && echo main || echo master)}"
# Priority: branch ahead of base → staged → unstaged → last commit
git -C "$WT" diff "$BASE...HEAD" 2>/dev/null | head -c 200000
```

Try each fallback in order until you get non-empty output:

```bash
git -C "$WT" diff --staged
git -C "$WT" diff
git -C "$WT" diff HEAD~1
```

If all are empty: "Nothing to review. Stage a change or commit something first."
— stop.

Capture the list of changed files:

```bash
git -C "$WT" diff --name-only "$BASE...HEAD" 2>/dev/null \
  || git -C "$WT" diff --name-only --staged \
  || git -C "$WT" diff --name-only \
  || git -C "$WT" diff --name-only HEAD~1
```

## Step 2: Classify the diff

Analyze the changed files and diff content to determine the dominant signal(s).
Apply each rule below; a diff can match more than one.

| Signal | Evidence | Mode |
|--------|----------|------|
| **Security** | auth, crypto, secrets, token, permission, sanitize, SQL, XSS, CSRF, JWT, env vars, credentials, `.env`, middleware, input validation | `security` |
| **Architecture** | new top-level directories, new packages/modules, new service boundaries, dependency additions, large structural refactors (>10 files across multiple subsystems) | `architecture` |
| **Product/UX** | UI components, routes/pages, copy changes, user-facing API surface, feature flags, onboarding flows, accessibility changes | `product` |
| **Edge cases** | algorithm changes, data transformation logic, pagination, error handling paths, retry logic, timeouts, concurrent access patterns | `edge-cases` |
| **Cleanup** | dead code removal, renaming, formatting, comment-only changes, dependency removal, lint fixes | `cleanup` |
| **Imagination** | database schema changes, new async flows, background jobs, webhooks, first-deploy concerns, infrastructure changes | `imagine` |
| **General** | everything else, or when multiple signals are present and no single one dominates | `review` |

Produce a routing table:

```
Routing decision
----------------
Changed files   : <N>
Dominant signal : <signal(s)>
Modes selected  : <comma-separated list>
Fan-out         : <yes|no>
```

State the reasoning in one sentence per selected mode.

## Step 3: Dispatch

### Single-mode path

If exactly one mode is selected, run it immediately by reading and executing
the instructions in the corresponding command file:

```bash
cat "$CLAUDE_PLUGIN_ROOT/commands/<mode>.md"
```

Then follow those instructions exactly, passing `--worktree "$WT"` and
`--level <level>` as arguments.

### Fan-out path

If two or more modes are selected, use `TaskCreate` to create one task per
mode before dispatching any of them. Mark each task `in_progress` when its
mode starts and `completed` when it finishes.

Run the modes **sequentially** (not in parallel — each is already a
multi-agent pipeline that saturates available capacity):

For each selected mode:
1. Mark its task `in_progress`.
2. Read `$CLAUDE_PLUGIN_ROOT/commands/<mode>.md`.
3. Execute the instructions in that file, passing `--worktree "$WT"` and
   `--level <level>`.
4. Capture the findings (Critical Issues, Serious Concerns, Suggestions).
5. Mark its task `completed`.

After all modes complete, synthesize results (see Step 4).

## Step 4: Synthesize (fan-out only)

When more than one mode ran, produce a unified dispatch report:

```markdown
# Dispatch Review: [Branch / short description]

_[One grumpy sentence on the overall state of the diff.]_

## Routing

| Mode | Signal |
|------|--------|
| <mode> | <one-line rationale> |

## Critical Issues 🚨
[Deduplicated across all modes. Each finding: mode-label + file:line + consequence.]

## Serious Concerns ⚠️
[Deduplicated across all modes.]

## Questionable Decisions 🤔
[Aggregated lower-priority findings.]

## Verdict
[Overall: ship it (grudgingly) / fix and reship / burn it down. One paragraph.]
```

Deduplication rules:
- If two modes flag the same file:line for different reasons, keep both
  findings but group them under the same bullet.
- Drop any finding that lacks a file:line or a concrete stated consequence.
- Prefer the higher severity when the same issue appears at different severities.

## Step 5: Persist

Save the dispatch report to the standard artifact location so `/grumpy:fix` can
find it:

```bash
WT="${WT:-.}"
BRANCH=$(git -C "$WT" rev-parse --abbrev-ref HEAD)
GIT_ROOT=$(git -C "$WT" rev-parse --show-toplevel)
ARTIFACT_DIR="$GIT_ROOT/.claude/grumpy/$BRANCH"
mkdir -p "$ARTIFACT_DIR"
```

Write the full report to `$ARTIFACT_DIR/review.md`.

For single-mode dispatches the individual mode already writes its own
`review.md`; do not overwrite it — the single-mode output is the final report.

## Available modes

The following modes exist in this plugin. **Only ever reference or dispatch to
modes in this list:**

- `review` — comprehensive code review
- `security` — auth, injection, credential, and data-exposure audit (whole-project,
  not diff-scoped — see Gotchas)
- `architecture` — structure, coupling, scalability, and conventions (whole-project,
  not diff-scoped — see Gotchas)
- `product` — UX, user-facing API surface, and experience quality
- `edge-cases` — algorithm correctness, error paths, and boundary conditions
- `cleanup` — dead code, over-abstraction, and unnecessary complexity (whole-project,
  not diff-scoped — see Gotchas)
- `imagine` — production-scenario walkthrough (deploys, async, failure modes)
- `audit` — full-codebase comprehensive audit (use only when the diff touches
  the entire project or is a major structural change)
- `fix` — apply fixes from a prior review artifact (do not dispatch here;
  mention it in the verdict when critical issues are found)

## Gotchas

- `audit`, `security`, `architecture`, and `cleanup` are all whole-project, not
  diff-based — each says so in its own command doc. A diff whose keywords match
  their routing signal (e.g. one file touching a JWT secret) still triggers a
  full-repo scan, not a scoped one. Route to them for a small diff only when the
  user explicitly wants that depth; otherwise say so in the report rather than
  silently paying for a whole-project pass the diff didn't ask for.
- `fix` is a repair action, not a review. Never dispatch to it automatically —
  mention it in the verdict as a next step.
- Fan-out increases runtime significantly. If the diff is clearly dominated by
  one signal, prefer single-mode dispatch and say so.
- `--dry-run` is useful before a large fan-out to confirm the routing makes
  sense without spending the time.
