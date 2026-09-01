---
name: dispatch
description: "Inspect the current diff and route automatically to the most relevant grumpy review mode(s); for broad changes fans out and synthesizes results."
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
# Check HEAD state before anything else. review.md/edge-cases.md/imagine.md
# all abort outright on detached HEAD; dispatch must too, and before routing —
# otherwise it can select a mode set, then have review/edge-cases/imagine
# individually refuse on detached HEAD while product.md silently falls back
# to a whole-project scan, producing a fan-out synthesis that conflates two
# different analysis scopes with no coordination between them.
if [ "$(git -C "$WT" rev-parse --abbrev-ref HEAD)" = "HEAD" ]; then
  echo "You're in detached HEAD state. Attach to a branch before running dispatch." >&2
  exit 1
fi
# Resolve the default branch — this mirrors the same fallback chain
# auto-init-gate-cycle.py uses, since a bare origin/HEAD symref isn't always
# set and a master-only repo would otherwise silently break on a hardcoded
# origin/main guess.
BASE=$(git -C "$WT" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)
if [ -z "$BASE" ]; then
  for candidate in origin/main origin/master main master; do
    git -C "$WT" rev-parse --verify -q "$candidate" >/dev/null 2>&1 && { BASE="$candidate"; break; }
  done
fi
# Priority: branch ahead of base → staged → unstaged → last commit. Only run
# the base-diff when $BASE actually resolved — bash expands an empty "$BASE"
# away, so "$BASE...HEAD" would silently become "...HEAD" (git parses that as
# HEAD...HEAD: exit 0, empty output), indistinguishable from "no diff here."
if [ -n "$BASE" ]; then
  git -C "$WT" diff "$BASE...HEAD" 2>/dev/null | head -c 200000
fi
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
{ [ -n "$BASE" ] && git -C "$WT" diff --name-only "$BASE...HEAD" 2>/dev/null; } \
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

If exactly one mode is selected, invoke it the way your harness supports
invoking one of its own commands from within another (a skill/sub-command
dispatch mechanism) — do **not** `cat` the command file and follow its
instructions inline yourself. Gate enforcement (the `sdlc` plugin's
PostToolUse hook that auto-records `grumpy-review` / `grumpy-imagine` gates)
fires only when that dispatch mechanism itself is invoked with a tracked
command/skill name; inlining a command's instructions never makes that call,
so on a gated branch the gate silently never records. **This gate-recording
effect is currently Claude Code specific**: the auto-record hook watches for
Claude Code's own `Skill` tool by name, and has not been ported to other
hosts (see `sdlc`'s README, "Codex CLI support") — on a host without that
hook, invoking via a dispatch mechanism vs. inlining makes no difference to
gate recording either way, since nothing records the gate on that host at
all yet.

```
Invoke "grumpy:<mode>" with args "--worktree \"$WT\" --level <level>"
```

This is exactly equivalent to the user typing `/grumpy:<mode> --worktree "$WT"
--level <level>` directly — same artifact output, same gate recording. If your
harness has no such mechanism for one command to invoke another, follow that
command's own instructions inline yourself instead, since there is no
recorded call to make.

### Fan-out path

If two or more modes are selected, use your harness's task-tracking feature
if it has one (one task per mode) — otherwise keep a plain checklist in your
working notes as you dispatch each mode.

Run the modes **sequentially** (not in parallel — each is already a
multi-agent pipeline that saturates available capacity):

For each selected mode:
1. Mark it in progress on your task list or checklist.
2. Invoke it the same way as the single-mode path — via your harness's
   command/skill dispatch mechanism if it has one:
   `Invoke "grumpy:<mode>" with args "--worktree \"$WT\" --level <level>"`.
   Each mode still writes its own artifact and — for `review`/`imagine` —
   still records its own gate exactly as a direct invocation would; nothing
   about running inside a fan-out changes that.
3. Capture the findings (Critical Issues, Serious Concerns, Suggestions).
4. Mark it completed on your task list or checklist.

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

Write the full report to `$ARTIFACT_DIR/review.md` — **unless** `review` was
one of the fanned-out modes, in which case its own Step 5 already wrote that
file with its own (non-synthesized) findings; overwriting it here would
silently discard that per-mode output. In that case write the synthesis to
`$ARTIFACT_DIR/dispatch.md` instead, and say so in the report's closing line
so the user knows where to look for both.

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
- Both paths above use the harness's command/skill dispatch mechanism directly
  rather than reading a command file and following it inline, specifically so
  gate-recording works — see the Single-mode path section above for why.
- `--dry-run` is useful before a large fan-out to confirm the routing makes
  sense without spending the time.
