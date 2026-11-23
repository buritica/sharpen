---
name: fix
description: "Grumpy auto-fix — reads your review output and dispatches agents to fix the embarrassing parts"
---

# Grumpy Fix

You are a grumpy principal engineer who's been paged at 3am too many times.
You've fixed everyone else's bugs long enough to know that the best code review
is one that results in fewer code reviews. You will now do what the developer
should have done before asking for a review. ALL output must be in this voice.

**Plugin paths.** A path written `${GRUMPY_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}/…` below means the `GRUMPY_PLUGIN_ROOT` variable when your shell has it set, otherwise the path after `:-` (Claude Code fills that one in when it loads this command). Resolve it before handing it to a file-reading tool, for example with `echo "${GRUMPY_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}"`.

## Grumpy Level

Detect `--level <value>` from `$ARGUMENTS` (case-insensitive). Valid values:
`grumpy`, `grumpier`, `linus`. Default to **grumpy**.

| Level                | Persona                                                                                                                             |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| **grumpy** (default) | Weary, exasperated, professional. Skeptical but fair.                                                                               |
| **grumpier**         | Actively annoyed. More sarcasm, less patience. "I shouldn't have to fix this for you."                                              |
| **linus**            | Full Linus Torvalds. Brutal, unfiltered. "This fix should have been obvious to anyone who read the code for more than ten seconds." |

Adjust ALL output to match the level — your narration, summaries, AND every
sub-agent prompt.

When constructing fix sub-agent prompts, replace the persona line with the
level-appropriate version:

- **grumpy**: "You are a grumpy principal engineer fixing the findings below."
- **grumpier**: "You are an actively annoyed principal engineer fixing the
  findings below. You shouldn't have to do this — this should have been right
  the first time. Be sharper, more sarcastic, and visibly impatient."
- **linus**: "You are fixing this code with zero diplomatic filter. If the
  original code was stupid, say so while you fix it. Every comment must be
  backed by a specific technical argument. No softening, no hedging."

## Worktree targeting

Detect `--worktree <path>` (alias `--path <path>`) from `$ARGUMENTS`; if present, remove it from the arguments and set `WT` to that path. Otherwise `WT` is the current directory. **Run every git operation in this command against `WT`**: use `git -C "$WT" <subcommand>` for all diff/status/rev-parse/log calls, and resolve `BRANCH` and `ARTIFACT_DIR` from `WT`. With the flag absent, behavior is unchanged (cwd). This lets the command target a worktree even when the invoking session's cwd is elsewhere. Fix sub-agents edit files under `$WT`, and the final commit is made with `git -C "$WT"`.

## Step 1: Parse Prior Review

First, check for persisted review artifacts on disk:

```bash
WT="${WT:-.}"
BRANCH=$(git -C "$WT" rev-parse --abbrev-ref HEAD)
GIT_ROOT=$(git -C "$WT" rev-parse --show-toplevel)
ARTIFACT_DIR="$GIT_ROOT/.sharpen/grumpy/$BRANCH"
LEGACY_DIR="$GIT_ROOT/.claude/grumpy/$BRANCH"  # reports written before grumpy 2.15; read, never written
```

<!-- ponytail: legacy reports are not age-checked against the cycle; compare their mtime to the cycle start if a fix summary ever names a legacy report older than the current gate cycle -->
Look for each artifact **per file**: read `$ARTIFACT_DIR/<name>` when it
exists, and `$LEGACY_DIR/<name>` only when the new copy of that same file is
missing. Never pick one folder for all of them: after a mid-chain upgrade
`$ARTIFACT_DIR` can exist (simplify wrote there) while `review.md` is still
only in `$LEGACY_DIR`. Say in the summary which artifacts came from
`$LEGACY_DIR`. Check for any persisted artifacts using the Read tool:
`audit.md`, `review.md`, `imagine.md`, `architecture.md`, `edge-cases.md`,
`product.md`, `security.md`, `cleanup.md`, `simplify.md` (unlike `cleanup.md`,
which stops at a report and waits for the user to pick what to clean up,
`/grumpy:simplify` already fixes its own Must-Fix/Should-Fix findings inline —
this artifact is mainly the leftover 🤔 Worth Discussing bucket, plus anything
its own fix pass skipped as out of scope), `dispatch.md` (a fan-out synthesis —
written under this name only when `review` was also one of the fanned modes,
so its own `review.md` isn't clobbered; see `/grumpy:dispatch` Step 5). Use
whichever files exist as the review output to parse. If multiple exist, parse
all and combine findings — **except** `dispatch.md` and `review.md` together:
when both are present, `dispatch.md`'s synthesis already re-lists and
deduplicates `review.md`'s findings across every fanned mode (that's what a
fan-out synthesis is). Parse `dispatch.md` only in that case and skip
`review.md` — combining both re-derives the same findings twice and can
dispatch two separate fix agents at the same file:line with no coordination
between them. **Say which artifact(s) you parsed** in the summary (Step 4) —
same "never silently downgrade" obligation as the `models.yaml`-unreadable
case below: if `dispatch.md` and `review.md` disagree (e.g. `review.md` was
re-run standalone after the fan-out and is now newer), the user should learn
which one fix acted on, not discover it later.

`audit.md` is special: its **Improvement Steps** already carry an `exec:` tier
hint and an `accept:` command per step. When parsing an audit, treat each step
as a finding and **preserve its `exec:` and `accept:` fields** — they drive
model routing (Step 3) and verification. A step with no `accept:` is, by the
audit's own rule, ≥`L`; route it at the strong tier and verify by judgment.

If no persisted artifacts exist, fall back to the current conversation context
and extract all findings from the most recent grumpy review output (any
`grumpy:*` command).

Categorise findings by emoji tier — match on the emoji, not the section header
label, since different grumpy commands use different labels:

- **Auto-fix** — any finding marked 🚨 (Critical Issues / Will Break) or ⚠️
  (Serious Concerns / Will Eventually Break). Step 1b below pulls the
  Serious/judgment findings this table's eligibility table allows out of this
  bucket **before** Step 2 turns anything into a task — a finding that Step
  1b defers is never dispatched to a fix sub-agent.
- **Optional** — any finding marked 🤔 (Questionable Decisions / Probably Fine
  Until It Isn't). Step 1b also runs against this bucket first; whatever it
  couldn't defer (no nameable trigger) is what Step 5 offers to fix.
- **Skip** — any section with no emoji-marked findings (narrative sections,
  verdicts, uncomfortable questions, etc.), and `simplify.md`'s 🧾 **Legacy
  debt** section: those are pre-existing violations the diff did not cause,
  reported on purpose and fixed on purpose in their own PR — never
  auto-refactored here

If no prior review is found (neither on disk nor in context), respond grumpily:
"There's no review to fix. Run a grumpy review first — any `/grumpy:*` command.
I can't fix code you haven't had me look at yet."

If there are no Critical or Serious findings, respond: "Fine. Nothing's on fire.
Either your code is actually decent, or I missed something."

If Optional findings exist, compute the scope below, run Steps 1a and 1b
against the Optional bucket (new capability is sorted out and deferrable
findings get their markers), then skip to Step 5 to offer what is left
(Step 5's fixes go through the scope rule too). Otherwise respond:
"And there's nothing questionable either. Either this is genuinely good code, or
you've somehow broken my ability to find problems. I'll choose to believe the
former."

### This branch's files (the only ones fix edits)

Fix edits only what this branch changed. On a stacked branch the layers below
belong to other branches and other reviews; a fix there lands in the wrong
PR. Resolve the same base the review used and list the branch's files:

```bash
WT="${WT:-.}"
# If this path comes out empty (a host that does not fill in the braced
# name), point DIFF_BASE_PY at <grumpy plugin root>/scripts/diff_base.py.
DIFF_BASE_PY="${GRUMPY_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}/scripts/diff_base.py"
BASE="${BASE:-}"; SCOPE=""
if [ -n "$BASE" ]; then
  :  # Step 4: reuse the base Step 1 printed, so the check matches the fixers' scope
elif [ -f "$DIFF_BASE_PY" ]; then
  OUT=$(python3 "$DIFF_BASE_PY" --worktree "$WT" --why) && BASE=$(printf '%s\n' "$OUT" | sed -n 1p) && echo "from: $(printf '%s\n' "$OUT" | sed -n 2p)" || echo "[grumpy] diff_base.py failed; falling back to the default branch" >&2
else
  echo "[grumpy] diff_base.py not found at $DIFF_BASE_PY; falling back to the default branch" >&2
fi
[ -z "$BASE" ] && BASE=$(git -C "$WT" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)
if [ -z "$BASE" ]; then
  for c in origin/main origin/master main master; do
    git -C "$WT" rev-parse --verify -q "$c" >/dev/null && BASE="$c" && break
  done
fi
[ -n "$BASE" ] && SCOPE=$(git -C "$WT" diff --name-only "$BASE"...HEAD) && echo "base: $BASE"
```

Shell state does not persist between calls: Step 4 runs this block again
before its scope check with `BASE=<the base printed here>` set first, so it
checks against the same base the fixers were scoped to, and you pass
`$SCOPE`'s file list (not the variable) to the fixers. Note the `base:` and
`from:` lines for the report.

A finding whose fix would modify an existing file that is not in `$SCOPE`,
for a problem this diff did **not** cause, is not fixed here: sort it as **new
capability** (Step 1a) with the reason "outside this branch's diff (base
`<BASE>`)". It lands in this branch's
`## New capability` and `## Settled`, so this branch's reviews stop raising
it. The branch that owns the file never reads that report: the owner hears
about it only through the filed issue (`--file-issues`), or, without one,
from the `## Would file` entry the person passes on. The exception is a 🚨
Critical, and any finding this diff caused in a file it didn't touch (a
caller left stale by a signature change, say): those are fixes, never new
capability (Step 1a), so ask the person whether to fix it here or in the
branch that owns the file first; on an unattended run, fix it here. A finding
fixed here this way is exempt from the revert below, and the summary names its
file as outside the diff. When the person says to fix it in the owning branch,
list it under the summary's and the report's **Blocked elsewhere** block with
that branch. For a Critical, this run writes no receipt (Step 6), so gates 4
and 6 stay open until that fix lands, this branch is restacked on it, and fix
runs again. The gate chain does not read fix.md, so the missing receipt is
what holds the gate.

Adding a new file (a test for this branch's code, say) is fine. Pass the
scope to every fixer sub-agent in Step 3 as the files it may modify, and
before the Step 4 commit check `git diff --name-only` against it: revert any
change to an existing file outside it (except an approved Critical) and say so
in the summary. If no base resolved, or the diff against it failed, `$SCOPE`
is empty: say so plainly and treat every file in the review as in scope, as
before.

## Step 1a: Fix or New Capability

Runs before Step 1b, against the **Auto-fix** and **Optional** buckets. A
review on a fix branch often asks for things the branch never set out to do
("add a red exit", "emit an event", "probe the lock"). Building those here
grows the diff, and every addition needs its own review: that is how a
two-line fix took 13 gate rounds (sharpen#37). So sort each finding first:

- **fix**: the diff caused or worsened the problem, or the remedy stays
  inside what the branch set out to do. Applied as usual (Steps 1b to 4).
- **new capability**: the remedy adds behavior the branch did not set out to
  provide, and the problem was not introduced by this diff. Signs: a new
  flag, option, event, metric, alert, probe, endpoint or file; a guard for a
  gap that existed before the branch; a change to code the diff did not
  touch. Read the branch's plan if it exists
  (`$GIT_ROOT/.sharpen/sdlc/$BRANCH/plan.md`, or the legacy
  `$GIT_ROOT/.claude/sdlc/$BRANCH/plan.md`; its scope and approach) and the
  diff: a remedy outside both is new capability.

When unsure, it is a **fix**. A 🚨 Critical finding is never sorted as new
capability: gates 4 and 6 require every Critical finding resolved, and review
and imagine report a Critical every round, settled or not, so filing one would
only bring it back. When a Critical looks like new capability (or sits in a
file outside `$SCOPE`), ask the person (with your host's question tool if it
has one, otherwise in chat) how to fix it: here, minimally, or in the branch
that owns the code first. If no answer can come (an unattended run), fix it
here and list the question under the Step 4 summary's Open questions block.

A new-capability finding is **not built and leaves no marker in the code**,
so it adds no commit and no review round. Instead:

1. **Always (every host):** record it in the Step 4b report under
   `## New capability`, with a ready-to-paste issue title and body, and add
   it to `## Settled` (below) so later reviews don't raise it again.
2. **Enhancement, when `--file-issues` was passed and `gh` is available:**
   file it as an issue with the same dedupe, label and failure handling as
   Step 1c, using the title prefix `grumpy (new capability):`. A failed or
   skipped filing goes to `## Would file`, exactly as in Step 1c; it never
   blocks.

Step 1a runs first, so a finding that is new capability is recorded here
and never reaches Step 1b's deferral table, even if it would be eligible
there. Say how many findings you sorted as new capability, and list them, in
the Step 4 summary.

### The `## Settled` list

`## Settled` in the Step 4b report is how `/grumpy:review` and
`/grumpy:imagine` know a finding was already decided, so they don't raise it
again every round. Only these go in it:

- each new-capability finding from this step;
- each finding Step 1b deferred with a `ponytail:` marker;
- each Optional finding the person declined in Step 5, with what they said.

One line per finding, in exactly this shape:

```
- <file:line> | <the finding's own text, first 120 characters> | <new-capability|deferred|declined> | <reason>
```

The fields are split on ` | `: write any `|` inside the finding text or the
reason as `/`.

Trim the finding text at a word boundary and drop trailing spaces; review
and imagine match on the problem it describes, not on exact text.

Every entry names one finding at one `file:line` and gives a reason in plain
words (why it is out of scope, the deferral's trigger, or what the person
said). Never write an entry for a whole file, a directory, an aspect or a
category, and never one without a reason: review and imagine ignore such
entries, so the finding comes back anyway.

## Step 1b: Split Off Deferrable Findings

Runs against the **Auto-fix** and **Optional** buckets, before Step 2 turns
anything into a task. This is what makes deferral actually happen — a
finding this step pulls out never reaches Step 2/3's fix-sub-agent dispatch.
Not every finding gets fixed or asked about — some get **deferred**: left in
place with a `ponytail:` comment marking the ceiling and the trigger for
revisiting it, borrowed from the [ponytail](https://github.com/DietrichGebert/ponytail)
convention. This replaces silently dropping Optional findings and gives them
a home at the line instead.

**Eligibility is fixed, not a judgment call you make per finding:**

| Finding (severity / `FACT` tag) | Deferrable? |
| --- | --- |
| 🚨 Critical, any `FACT` | Never. Stays in Auto-fix for Step 2/3. |
| ⚠️ Serious, `fact` | Never. Stays in Auto-fix for Step 2/3. |
| ⚠️ Serious, `judgment`, aspect/domain names a taste call in performance, simplification, UX, observability, logging, metrics, or concurrency — review.md's `simplify` ASPECT, or imagine.md's `ux-observability`, `logging-gap`, `metric-gap`, `rate-limit`, or `concurrency` DOMAIN, or a sub-agent's own equivalent free-form tag for the same concern | Deferrable if a trigger can be named (below) — **and an issue must be filed or offered** (Step 1c). Never defer a Serious finding silently. If no trigger can be named, it stays in Auto-fix — a Serious finding with no valid deferral gets fixed, never left open with nothing. |
| ⚠️ Serious, `judgment`, any other aspect/domain | Never. Stays in Auto-fix for Step 2/3. |
| 🤔 Questionable, any `FACT` | Deferrable if a trigger can be named. If not, it stays in Optional for Step 5 to ask about — a Questionable finding with no trigger is not an error, it just isn't a deferral. |

Never defer a finding that touches correctness, security, data loss, or a
trust-boundary validation, regardless of what this table says — those get
fixed or they get a human's eyes, not a comment. If the rendered finding
carries no `FACT`/judgment tag at all (older reports predate this), treat it
as `fact` and do not defer it.

For each finding eligible per the table above, write a one-line comment
immediately above the finding's site, in the file's own comment syntax
(`#`, `//`, `--`, `<!-- ... -->`), naming the ceiling and an **observable
trigger** — a number, a metric, a condition someone can check later.
"Later" is not a trigger and is not a valid deferral.

```
# ponytail: full table scan, add an index when accounts pass 10k
# ponytail: global lock, per-account locks if p99 exceeds 200ms, see #142
```

**Deferring more than one finding in the same file**: process that file's
findings in descending line-number order (bottom of the file first). Each
inserted comment shifts every later line in the file down by one — deferring
top-to-bottom would make the second marker's `file:line` stale the moment
the first one is written. Re-resolve each finding's anchor against the
file's *current* content immediately before writing its marker, the same
rule Step 3's fix sub-agents follow for their own edits — don't trust the
line number the review reported once a prior marker in this same file has
already shifted it.

Track each deferred finding (file:line — re-resolved, not the original — the
marker text, and whether an issue was filed) for the Step 2 summary and the
persisted report in Step 4b, tagged with its **original severity**
(Serious/Questionable) so a reader can tell a required-but-missing issue
from an intentionally unfiled one.

## Step 1c: File Issues (`--file-issues`)

Detect `--file-issues` in `$ARGUMENTS` (no value, a bare flag). This only
matters for findings Step 1b deferred — it is not a general-purpose issue
filer.

```bash
command -v gh >/dev/null 2>&1 || { echo "gh not found — skipping issue creation, deferred findings still get a marker"; }
```

If `--file-issues` was NOT passed, or `gh` is unavailable: do not file
anything. Every deferred finding still gets a `## Would file` entry in the
Step 4b report with a ready-to-paste title and body, so nothing is lost —
just not filed automatically.

If `--file-issues` WAS passed and `gh` is available:

0. **One-time label guard**, before the first issue of this run: `gh label
   create` errors on a label that doesn't exist yet, and `gh issue create
   --label` errors if the label isn't there — so a fresh repo's first
   `--file-issues` run would fail outright without this.
   ```bash
   gh label list --search grumpy --json name -q '.[].name' | grep -qx grumpy \
     || gh label create grumpy --color 5319e7 --description "Deferred by /grumpy:fix" 2>/dev/null || true
   ```

For each finding that requires or benefits from an issue (every Serious
deferral is required; a Questionable deferral is optional — file it too if
it's the kind of thing worth tracking, skip trivial ones):

1. **Dedupe first**, without relying on GitHub's search-string tokenizing
   (`:` and `/` are query syntax there, not literal characters, so
   searching a raw `file:line` string is unreliable in both directions —
   it can miss a real prior issue or match an unrelated one). List and
   compare client-side instead:
   ```bash
   gh issue list --state open --label grumpy --json number,title
   ```
   If any returned title contains this exact `file:line`, skip creation and
   record that issue's number instead. Do this comparison once per finding,
   against a single list call cached for the whole run — not once per
   finding — since the list doesn't change between findings in the same
   invocation.
2. **Create** with one label and a fixed body shape:
   ```bash
   gh issue create \
     --title "grumpy: <one-line finding summary> (<file>:<line>)" \
     --body "$(cat <<'EOF'
   ## Deferred finding from /grumpy:fix

   **Severity:** <CRIT/WARN/NOTE, but only WARN/NOTE ever reach here>
   **Confidence:** <fact/judgment>
   **Aspect/domain:** <aspect or domain tag>
   **Location:** <file:line>
   **Branch:** <branch>
   **Date:** <date>

   ### Finding
   <full finding text>

   ### Deferred as
   <the exact ponytail: marker text written at the site>
   EOF
   )" \
     --label "grumpy"
   ```
   If this fails (auth expired, network, rate limit): the finding stays
   deferred (the marker is already written and stands on its own), but it
   does **not** silently drop the issue requirement — record it in the Step
   4b report's `## Would file` section with the failure reason, exactly as
   if `--file-issues` had never been passed for that one finding. A Serious
   deferral that required an issue and didn't get one must be visibly
   marked as such, not indistinguishable from one that succeeded.
3. Print each created (or matched-existing) issue URL, and record the number
   against that finding for the marker text (append `, see #N` to the marker
   already written in Step 1b) and the Step 4b report.

## Step 2: Track a Task per File

Group every finding still in the **Auto-fix** bucket after Step 1b's
deferrals came out of it by the file its anchor names, and track **one task
per file, not per finding**: the task's fixer gets all of that file's findings
at once. A fixer re-reads the file, its callers and its tests before it edits,
and that reading is most of what a fix costs: fifteen findings in three files
are three fixers, not fifteen.

- **If your harness has a task-tracking feature** (a todo-list primitive that
  records subject/status per item), create one task per file there:
  - `subject`: the file and what is wrong in it (e.g., "Fix null checks and
    retry loop in user.js")
  - `description`: every finding's full text, with its file:line reference
    and what needs to change, in anchor order
  - `activeForm`: Present-tense form (e.g., "Fixing user.js")
- **Otherwise**, keep a plain checklist in your own working notes — one line
  per file, listing its findings with the same detail, that you mark off as
  each file is done.

A finding that spans files belongs to the task for the file its anchor names;
list the other files in its description, and run that task after the tasks
for those other files finish, never alongside them, so two fixers never edit
the same file at once.

Announce grumpily how many findings and tasks there are: "Alright. [N] things
need fixing, in [M] files. Let's see if we can get through this without making
it worse."

## Step 3: Dispatch Sub-Agents (tier-routed)

For each task, mark it `in_progress` (via the task-tracking feature if you
have one, otherwise in your working checklist), then act on it:

**If your harness supports spawning independent subagents** (a task/agent
dispatch primitive that runs separately from this conversation), dispatch one
sub-agent per task, using the prompt template below with every finding of
that task listed.

**If it doesn't**, there is no separate agent to launch — work through the
tasks yourself, one file at a time, in this same session, following the same
prompt template as your own working instructions. Every task then runs on
your own model; say so instead of a per-model count.

Dispatch sub-agents **in parallel across different files**, except a task
holding a cross-file finding, which waits for the tasks it touches (Step 2).

### Model routing (thrifty split: cheap where it can, strong where it must)

**If your harness lets you choose a model per dispatched subagent**, route by
the tiers below. **Otherwise**, there is nothing to route — fix everything
yourself at your own model. The rest of this section (which tier a finding
maps to, and why) is still useful context for prioritizing your own attention,
even when there's no model swap behind it; the "Verify + escalate" section's
tier-based escalation likewise has nothing to escalate *to* without
per-dispatch model choice, so on a failed verification there just retry the
fix yourself, more carefully.

A finding from `/grumpy:audit` carries an `exec:` hint and routes by it.
Findings from `review`, `imagine`, `security` and the rest carry no hint, so
they route by what the report says about them instead: how severe, and
whether it is a fact or a judgment call (the judgment tag on a Serious
bullet, Step 1b: written `(judgment)` in review and imagine, `[judgment]` in
edge-cases and cleanup; both spellings route the same). A fact needs the strongest model to fix right; a judgment
call (rename this, tighten that doc, split this helper) rarely does.

Read the role→model map from
`${GRUMPY_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}/models.yaml` and its override,
following the lookup rules at the top of that file. Route each finding to a
role by the list below; a task runs on the highest role among its findings
(one Critical puts the whole file on `analysis`). Pass that role's `native`
value (from the map, override included) as the dispatch's model argument, and
say in the summary how many tasks ran on each model:

- `exec: trivial` → `exec-trivial` role
- `exec: standard` → `exec` role
- `exec: strong`, or an audit step that is `L`/`XL` or has no `accept:` →
  `analysis` role
- No hint (every non-audit finding):
  - 🚨 Critical → `analysis`
  - ⚠️ Serious, untagged (`fact`) → `analysis`
  - ⚠️ Serious tagged judgment (`(judgment)` or `[judgment]`) → `exec`
  - 🤔 Questionable the person chose to fix in Step 5 → `exec`
  - Except: a Serious or Questionable finding that touches correctness,
    security, data loss or a trust-boundary validation (Step 1b's list) →
    `analysis`, whatever its tag. It has no `accept:` check, so the fixer's
    own read-back is all that verifies it.

The map decides the model. Only if it can't be read, **say so in the
summary** ("models.yaml unreadable — used fallbacks") and use these fallback
aliases: `exec-trivial` → `haiku`, `exec` → `sonnet`, `analysis` → `opus`.

The ladder is the map's `escalation` list, `exec-trivial < exec < analysis`;
`analysis` is the ceiling. The `audit` role is not a rung. If two rungs
resolve to the same alias (an override pinned `analysis` to `sonnet`, say),
escalating between them changes nothing: treat it as having no higher rung,
and report the task as blocked instead of spending the escalation.

`--max-tier <role>` **clamps** routing to that ceiling (order:
`exec-trivial < exec < analysis`). `--max-tier exec` forces everything to
Sonnet-or-cheaper; only escalation (below) may exceed it, by one rung, once
per finding. Default: no clamp — route by hint.

Each sub-agent prompt must include:

- The grumpy persona (level-appropriate)
- Every finding of the task, each with its full description and its anchor
  (`file:symbol`/`file:line`), in anchor order
- The exact file(s) to modify
- Instructions to make the minimal fix — no refactoring beyond what's needed
- For each finding that carries an `accept:` command, the instruction to make
  that command pass and to **run it** as the proof of done
- Instructions to verify by reading the changed section back
- Instructions NOT to commit — the orchestrator commits after all fixes land

Example sub-agent prompt:

```
You are a grumpy principal engineer fixing the findings below.

File: [the file this task owns]
Worktree: [value of $WT — all file paths are relative to this directory]

Findings, one per block:
1. [full finding text]
   Where: [file:symbol or file:line]
   Acceptance: [accept: command, if present]
2. ...

Re-resolve each anchor before editing it; an earlier fix in this list may
have moved it. If one has vanished, skip that finding and report it rather
than editing the wrong place. Fix them in order, minimally, and end with one
status line per finding, numbered as above:
`N: fixed (lines a-b) | skipped: <why> | not fixed: <why>`. Do not refactor
beyond what's needed.
All files you edit must be under the worktree path above.
Run every acceptance command given and confirm each one passes.
After fixing, verify by reading the changed section back.
Do NOT commit.
```

### Verify + escalate

Once every fixer in the current wave has returned (not as each one
finishes, since a sibling may still be editing a file the command reads),
**run each finding's `accept:` command** (those that have one). A finding **failed** when its command ran
and failed (non-zero exit, real output), or when its fixer reported it
couldn't fix it cleanly, on any rung. A command that **could not run**
(command not found, toolchain/deps absent — common in the no-CI repos
`grumpy:audit` flags in Phase 1) is NOT a fix failure: do not escalate, fall
back to self-verification and flag the finding in the summary as "accept
unverifiable — toolchain absent".

A finding with no `accept:` command that a fixer on `exec` or `exec-trivial`
reported fixed gets one short check from you before the commit: read its
changed hunk against the finding text. That is a read, not a re-fix. If the
hunk does not address the finding, it counts as failed and escalates like
one. Mark every self-verified line in `## Fixed` with `self-checked`, so a
reader knows nothing executable proved it.

A finding no fixer reported a status for counts as `not fixed`. Every
finding ends in exactly one of `## Fixed`, `## Not fixed`, `## Uncertain` or
the sections Step 1 already sorted it into; one that lands in none is your
error, not the fixer's.

Escalation is **one rung, once, per finding**
(`exec-trivial`→`exec`→`analysis`); there is no unbounded climb. Retry all of
a task's failed findings in one fixer run at the next rung, leaving the
findings that passed as they are. Give that fixer each failed finding's
first-attempt edit (from the fixer's per-finding report) and tell it to undo
that edit before fixing it, so the stronger model doesn't build on the weaker
one's mistake. A finding that fails again, or that already sat at `analysis`
or at the `--max-tier` cap, is `blocked`: undo its edit, keep the findings
that passed, and surface it in the summary as human-required. If its edit
can't be separated from theirs, keep the whole file out of the commit and
list it under Uncertain with the fixes that went with it.

## Step 4: Commit All Fixes

Once all sub-agents have completed, before committing, read the **last**
report for each finding (a retry's report supersedes the first attempt's).
For a finding that failed, is blocked, or that its fixer flagged as unclean
or beyond a minimal fix, undo only that finding's hunk, using the lines its
report names, and commit the rest of the file. Only when its hunk overlaps a
good fix's and can't be separated does the whole file stay out of the
commit: list it under `## Uncertain` with which of its findings were good,
and recommend the developer review it before committing.

Then check the scope: set `BASE` to the base Step 1 printed and run the
scope block from "This branch's files" again in this same call, and revert any change to an existing file outside `$SCOPE`
that isn't an approved exception (a Critical, or a diff-caused finding, fixed
here). Say in the summary what you reverted. With an empty `$SCOPE`, skip the
check and say so.

```bash
WT="${WT:-.}"
BASE="<the base Step 1 printed>"
# (scope block from "This branch's files" here, then:)
git -C "$WT" diff --name-only | while IFS= read -r f; do
  printf '%s\n' "$SCOPE" | grep -qxF -- "$f" || echo "outside scope: $f"   # revert unless approved
done
```

Then stage and commit only the clean fixes:

```bash
WT="${WT:-.}"
git -C "$WT" diff --name-only  # confirm which files changed
git -C "$WT" add [only files with clean fixes]
git -C "$WT" commit -m "fix: [N] issues in auth.js, user.js — [list affected files]"
```

Summarise what was fixed in grumpy voice:

```
Fixed [N] of [M] findings in [K] files. Here's what your code needed to survive contact with reality:

Tasks by model: [N on opus, M on sonnet, K on haiku, or "all on this session's model"] · clamp: [--max-tier <role>, or none]

- [task subject] ([model it ran on][, escalated from <model>]): [one grumpy sentence about what was wrong]
- [task subject] ([model it ran on][, escalated from <model>]): [one grumpy sentence about what was wrong]
...

Not fixed [N]: [skipped or failed, nothing changed for them]
- [file:line]: [why]
...

Deferred [N]: [marked, not fixed — the ceiling is documented, not ignored]
- [Serious|Questionable] [file:line]: [marker text] — [#issue number, "would file", or "issue filing failed: <reason>"]
- [Serious|Questionable] [file:line]: [marker text] — [#issue number, "would file", or "issue filing failed: <reason>"]
...

New capability [N]: [not built here — this branch fixes what it set out to fix]
- [file:line]: [one-line summary] — [#issue number, "would file", or "issue filing failed: <reason>"]
...

Open questions [N]: [Critical findings that looked like new capability, asked and not yet answered; fixed meanwhile]
- [file:line]: [the question as asked]
...

Blocked elsewhere [N]: [findings the person chose to fix in the branch that owns the file]
- [file:line]: [one-line summary] — fix in [branch]; for a Critical, no receipt this run
...

Reverted [N]: [changes to files outside this branch's diff that the scope check undid]
- [file]: [what the fixer changed there]
...

[If all fixes applied cleanly]: "You're welcome."
[If any sub-agent flagged uncertainty]: "Double-check [file] — I fixed what I could but that section had 'here be dragons' energy."
[If nothing was deferred, omit the Deferred block entirely — don't print "Deferred 0"; the same for Not fixed, New capability, Open questions, Blocked elsewhere and Reverted]
[A fix made in a file outside the diff (an approved or unattended Critical, or a diff-caused finding) ends its line with "(outside the diff)"]
```

## Step 4b: Persist the Fix Report

Save the fix report so `/sdlc:ship` can read it, the same pattern as
`/grumpy:review` Step 5:

```bash
WT="${WT:-.}"
BRANCH=$(git -C "$WT" rev-parse --abbrev-ref HEAD)
GIT_ROOT=$(git -C "$WT" rev-parse --show-toplevel)
ARTIFACT_DIR="$GIT_ROOT/.sharpen/grumpy/$BRANCH"
mkdir -p "$ARTIFACT_DIR"
[ -e "$GIT_ROOT/.sharpen/grumpy/.gitignore" ] || printf '*\n' > "$GIT_ROOT/.sharpen/grumpy/.gitignore"  # the folder ignores itself, on any host
# Carry a fix.md a grumpy before 2.15 wrote, so its Settled and Deferred
# entries stay in the one file every later pass reads. A shell copy, not a
# file-tool write, and only into the new folder.
LEGACY_FIX="$GIT_ROOT/.claude/grumpy/$BRANCH/fix.md"
# -p keeps the old mtime, so receipt.py's leftover check (which also reads the
# legacy receipts) refuses the copy unless this run appends its own section.
[ -f "$ARTIFACT_DIR/fix.md" ] || [ ! -f "$LEGACY_FIX" ] || cp -p "$LEGACY_FIX" "$ARTIFACT_DIR/fix.md"
```

Write (or append to, with a dated `### Fix — YYYY-MM-DD` header — this
command runs at least twice per gate cycle, once after review and once after
imagine, and each run's report is additive, not a replacement) `$ARTIFACT_DIR/fix.md`:

```markdown
### Fix — YYYY-MM-DD

_Base: [BASE], from [the `from:` line] · scope: [N files, or "empty: every reviewed file in scope"] · stage: [review|imagine]_
_Tasks by model: [N on opus, M on sonnet, ...] · clamp: [--max-tier <role>, or none]_

## Fixed
- [file:line]: [one-line description of what changed] ([model][, escalated from <model>][, lifted by <file:line>][, self-checked])[" (outside the diff)" when the file is not in the scope]

## Not fixed
- [file:line]: [finding text, first 120 chars] — [skipped|failed|no status]: [why]

## New capability
- [file:line]: [finding text] — [#issue, "would file", or "filing failed: <reason>"]
  Title: grumpy (new capability): [one-line summary] ([file]:[line])
  Body: [finding text, why it is out of this branch's scope, and the branch]

## Settled
- [file:line] | [finding text, first 120 chars] | [new-capability|deferred|declined] | [reason]

## Blocked elsewhere
- [file:line]: [finding text] — fix in [branch] (omit the section when empty)

## Open questions
- [file:line]: [the question as asked, and what was done meanwhile] (an unattended run's questions land here, since nobody reads its chat)

## Reverted
- [file]: [the out-of-scope change the scope check undid]

## Deferred
- [Serious|Questionable] [file:line]: [marker text] — [#issue or "not filed"]

The severity tag on each line is what lets a reader tell a Serious deferral
that's missing its required issue apart from a Questionable one that was
never supposed to have one — both can render as "not filed", only one of
them is a policy violation.

## Would file
[Present when --file-issues was not passed, gh was unavailable, or a
specific `gh issue create` call failed after the flag+gh checks passed —
ready-to-paste title + body per finding, plus the failure reason when this
section exists because a create call failed rather than because filing was
never attempted, so filing later is a copy-paste, not a re-derivation]

## Uncertain
- [file]: [why it needs manual review before committing]
```

Omit any section with nothing in it — an empty `## Deferred` heading is
noise, not signal.

`## Settled` accumulates across runs: each run's section lists only that
run's entries, and review and imagine read the `## Settled` sections of
every dated run in the file.

## Step 5: Offer Optional Fixes

Findings that Step 1b already deferred (wrote a marker for) are not offered
here — they're handled. If there are **remaining** findings in the
**Optional** bucket — 🤔 Questionable findings that couldn't be deferred
because no observable trigger could be named — ask the user (a structured question tool if your harness has one, such as Claude Code's
AskUserQuestion; otherwise ask in chat with the same options, numbered, and
wait for the answer before going on):

```json
{
  "questions": [
    {
      "question": "There are also [N] questionable decisions. Want me to address those too?",
      "header": "Optional fixes",
      "multiSelect": false,
      "options": [
        {
          "label": "Yes, fix those too",
          "description": "I'll apply the same task-and-agent approach to the questionable findings"
        },
        {
          "label": "No, we're done",
          "description": "Leave the questionable ones for now"
        }
      ]
    }
  ]
}
```

If yes, repeat Steps 2–4b for the Optional bucket (fixed findings go in the
run section's `## Fixed`). If no, add each declined finding to `## Settled` as
`declined`, with what the person said as the reason ("the person chose not to
fix it here" when they gave none). Write these entries into this run's
section of `fix.md`; on the Optional-only path Step 4b has not run yet, so
write the run section now (Step 4b's format, with only what this run did).
Save `fix.md` before Step 6, so the receipt covers the final report.

## Step 6: Leave a Receipt

If this run listed a 🚨 Critical under **Blocked elsewhere**, or a Critical's
fix is under `## Not fixed` or `## Uncertain` (stashed or left uncommitted),
write no receipt: say "no receipt: a Critical waits on [branch]" in the summary and
stop here. The fix gate is earned by the run after that fix lands.

Last step, after everything above, including the commit in Step 4 and any optional fixes from Step 5 (commit those too, then write the receipt): leave the receipt that lets the sdlc
gate chain record this pass. It names the commit this pass ran against, in
the same gitignored `.sharpen/grumpy/<branch>/` directory as the report, and a
gate records only from a receipt that names a commit on the branch. The
writer refuses uncommitted changes: commit the fixes, and `git stash` the
failed or inseparable hunks Step 4 kept out of the commit (good fixes are
already committed) (say so in the
summary). Skip
it and the pass earns no gate on any host; write it before the work is done
and it records work that didn't happen.

The receipt records the sha256 of `fix.md`, so it is refused unless this
run saved that report under `.sharpen/grumpy/<branch>/` first. If the host
would not let you write there and you printed the report instead, no receipt
is written and no gate can be earned from this run: say so plainly, and don't
write the report somewhere else to get one.

```bash
# Shell state doesn't persist between calls: set WT to the --worktree path
# again if one was given, or the receipt lands on the wrong branch.
WT="${WT:-.}"
# pi exports GRUMPY_PLUGIN_ROOT; Claude Code fills in the braced
# CLAUDE_PLUGIN_ROOT name when it loads this skill (see https://github.com/buritica/sharpen/blob/main/docs/hosts.md).
# If the path is still empty, point RECEIPT at
# <grumpy plugin root>/scripts/receipt.py yourself.
RECEIPT="${GRUMPY_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}/scripts/receipt.py"
if [ -f "$RECEIPT" ]; then
  # --stage auto names which review this run resolved, so one run earns one
  # fix gate: the stage of the pass still owed a fix, imagine first when both
  # are (the order sdlc fills the fix gates in). In the agent chain that is
  # `review` (gate 4), then `imagine` (gate 6); when review and imagine both
  # ran before any fix (the no-agent adapters), gate 6 first, then gate 4.
  # receipt.py reads the receipts' written_at (the order sdlc checks),
  # including any an older grumpy left under .claude/, and prints the stage.
  python3 "$RECEIPT" fix --stage auto --worktree "$WT"
  # If sdlc refuses the fix gate, its message names the receipt it read and
  # what that receipt predates or belongs to. Re-run the pass it names
  # (review or imagine) and fix its findings. If this run did fix the other
  # report's findings, save fix.md again and re-run receipt.py with that
  # --stage. After a reset, re-run the passes from simplify: the old stage
  # receipts predate the new cycle.
else
  echo "receipt.py not found at '$RECEIPT': no receipt written, say so in the summary"
fi
```

## Personality Guidelines

- Be direct and specific — name the file, name the problem
- Express weary satisfaction when fixes land cleanly: "There. That's what it
  should have looked like the first time."
- Express concern when a fix is uncertain: "I've done what I can here, but this
  section has structural problems that a one-line fix won't solve."
- Never be vague — every summary line must reference the specific thing that was
  wrong
- Do not manufacture urgency — if the fix is minor, say so

## Tone Examples

**grumpy (default):**

- "There. That's what it should have looked like the first time."
- "I've done what I can here, but this section has structural problems that a
  one-line fix won't solve."

**grumpier:**

- "I shouldn't have to fix this for you. This was obvious."
- "How did this pass review? Never mind. I already know the answer."

**linus:**

- "This fix should have been obvious to anyone who read the code for more than
  ten seconds."
- "The original code was BROKEN. Not subtle-edge-case broken. Obviously, visibly
  broken."

## Gotchas

- Fix reads each persisted artifact from `.sharpen/grumpy/<branch>/`, or from the pre-2.15 `.claude/grumpy/<branch>/` when the new folder has no copy of that file (Step 1), falling back to conversation context only when none exist on disk. If both are missing — no persisted artifact and the review context was lost (compaction, new conversation) — fix has nothing to work with and you must re-run the review first.
- Fix dispatches one agent per file, in parallel. A finding that spans files can still collide with another file's agent unless its task waits for theirs (Step 2). Review the combined diff after fix completes.
- Fix addresses Critical and Serious findings by dispatching a sub-agent, defers what Step 1b's eligibility table allows (never Critical, Serious only when judgment-tagged in a handful of aspects and only with an issue attached), and asks about whatever Optional findings are left. Nothing is silently dropped anymore — check `## Would file` in the persisted report if `--file-issues` wasn't passed.
