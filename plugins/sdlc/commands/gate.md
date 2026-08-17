---
description: "Run the quality gate chain on current changes. Gates run in order; fix failures before advancing. Re-run from the top after any post-gate code change."
argument-hint: "[--tier tiny|small-medium|significant] [--worktree <path>]"
allowed-tools: ["Bash", "Read", "Grep", "Glob", "Edit", "Write", "TaskCreate", "TaskUpdate"]
---

# Gate

Run the quality gate chain on the current worktree. Gates enforce code quality before a PR opens. The chain runs in order because earlier gates are cheaper to fix.

## Worktree targeting

Detect `--worktree <path>` (alias `--path <path>`) from `$ARGUMENTS`; if present, remove it from the arguments and set `WT` to that path. Otherwise `WT` is the current directory. **Run every git operation in this command against `WT`**: use `git -C "$WT" <subcommand>` for all diff/status/rev-parse/log calls, and resolve `BRANCH` and `ARTIFACT_DIR` from `WT`. With the flag absent, behavior is unchanged (cwd). This lets the command target a worktree even when the invoking session's cwd is elsewhere. When `--worktree` is set, resolve the branch from `$WT` (`git -C "$WT" rev-parse --abbrev-ref HEAD`) and pass it to `--branch` when calling `record-gate.py`.

`--branch` covers the gates you record yourself (`tests`, `lint`, `typecheck`). The skill-gated ones (`simplify`, `grumpy-*`) are stamped by the `auto-record-skill-gate.py` PostToolUse hook, which never sees this command's arguments — it only knows the cwd the skill ran in, which is **this** session, not `$WT`. Passing `--route-from` at `--init` is what tells the hook otherwise, so **when `--worktree` is set, every `--init` in this command MUST carry `--route-from "$PWD"`**. Skip it and gates 2–6 land on the wrong branch (or nowhere) — silently, since skill-gated gates have no manual `--record` to correct them with. `--init` is also the post-gate reset, so this applies to every re-init in the chain, not just the first.

The route lives in the shared store until it is replaced or dropped. It is dropped when this session next runs `--init` for its **own** branch, or explicitly:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/record-gate.py" --unroute
```

Run `--unroute` when you finish gating `$WT` and keep working in this session — otherwise a later `/grumpy:review` here still records against `$WT`'s branch. `--status` prints `Driven from: <path>` whenever a route is active; check it if a gate lands somewhere unexpected. A worktree drives at most one branch, but two sessions may drive the same target and both record there.

## Prerequisites check

Before initializing the gate cycle, detect which optional capabilities are available and announce the mode.

**Skill detection — do NOT probe the filesystem.** Detect by **command availability**: `/simplify`, `/grumpy:review`, `/grumpy:fix`, and `/grumpy:imagine` are listed in your available skills/commands for this session if and only if they are installed. Check that list directly — do not run `ls "$CLAUDE_PLUGIN_ROOT/../grumpy/..."`, since `$CLAUDE_PLUGIN_ROOT` is empty in the skill-execution context and that probe always fails.

**`/simplify` is gate 2's recorder, and it does not ship with this plugin.** Gate 2 is skill-gated exactly like 3–6: the `simplify` gate is stamped only when the `/simplify` skill runs, and a manual `--record simplify` is refused by the store. So check for it in the same breath as grumpy — if it is missing, a `small-medium`/`significant` cycle cannot complete either, and the same "say it before you initialize" rule below applies.

- All three grumpy commands available → **grumpy mode** for gates 3–6.
- Otherwise → **self-review fallback**. Announce it loudly: `⚠️ grumpy not available — gates 3–6 use the weaker self-review fallback`, and note it in the PR description.
  **The fallback cannot satisfy gates 3–6 when the hooks are installed.** Those gates are recorded only by the auto-record hook when the real skill runs; `record-gate.py --record grumpy-review` is refused by the store, by design. So with hooks registered and grumpy absent, a `small-medium`/`significant` cycle never completes and `gh pr create` stays blocked.
  **Say this before you initialize, not after the work is done.** If the tier you are about to init is `small-medium` or `significant` and grumpy (or `/simplify`) is unavailable, stop and tell the user: the cycle cannot complete, and their options are to install grumpy or (only if the change genuinely qualifies) run `tiny`. Failing at init costs them a sentence; failing at `gh pr create` costs them the whole chain. Do not present the self-review as having satisfied a gate it cannot record.
- If you are genuinely unsure, **ask** rather than silently downgrading.

Gate tracking + enforcement are **pure python (stdlib)** — no `bun`, no external runtime. So enforcement works on any box: the `enforce-sdlc-gates.py` hook blocks `gh pr create` when gates are incomplete. State the mode in one line, e.g. `Mode: grumpy + enforced gates`.

## Gate tracking

Gates are tracked in a single JSON file **shared across every worktree of the repo**, keyed by branch. `scripts/record-gate.py` writes it at `<main-checkout>/.claude/data/gates.json` — the path is resolved via `git rev-parse --git-common-dir`, which points at the main checkout's `.git` from any linked worktree, so every worktree (and any cwd inside the repo) reads and writes the same file (override with `$SDLC_GATES_PATH`). The `enforce-sdlc-gates.py` hook reads that same shared file, taking the branch from the `gh pr create` command's `--head` if it has one (normalized, so `owner:branch`, `refs/heads/branch` and the clustered `-Hbranch` resolve to the same key) and otherwise from its `cd`/`git -C` working directory. Because the store is shared and branch-keyed: a cycle recorded in one worktree is visible when the PR is created from another, while two branches checked out in two worktrees stay isolated by their branch key. (Worktree targeting assumes the invoking session is in the same repo as `$WT`.) The `routed_from` entry described above rides in that same shared file, which is how the auto-record hook in this session finds `$WT`'s cycle.

### Initialize

```bash
WT="${WT:-.}"
BRANCH=$(git -C "$WT" rev-parse --abbrev-ref HEAD)
# --route-from only when --worktree was passed; harmless-but-pointless otherwise
# (routing a worktree to its own branch is a no-op).
python3 "$CLAUDE_PLUGIN_ROOT/scripts/record-gate.py" --init <tier> --branch "$BRANCH" --route-from "$PWD"
```

Replace `<tier>` with `tiny`, `small-medium`, or `significant`. Drop `--route-from "$PWD"` when you are gating the current worktree.

### Record a gate

After each gate passes (manual recording is only for the bash-verifiable gates — `tests`, `lint`, `typecheck`; the `simplify` and `grumpy-*` gates auto-record when the skill runs):

```bash
WT="${WT:-.}"
BRANCH=$(git -C "$WT" rev-parse --abbrev-ref HEAD)
python3 "$CLAUDE_PLUGIN_ROOT/scripts/record-gate.py" --record <gate-name> --branch "$BRANCH"
```

### Reset (after post-gate code changes)

Re-run `--init` with the same tier — it clears all timestamps (the route survives, but pass `--route-from` anyway so the reset is identical to the init):

**Know the price before you type it.** A reset clears the skill-gated gates too, and those can only be re-earned by running their skills again — there is no `--record` for them. On a `small-medium` cycle that is `/simplify` plus three grumpy runs. That is the correct cost when the code actually changed (a gate that passed against different code proved nothing), but it means "just re-init" is not a cheap reflex. Batch your fixes and reset once.

```bash
WT="${WT:-.}"
BRANCH=$(git -C "$WT" rev-parse --abbrev-ref HEAD)
python3 "$CLAUDE_PLUGIN_ROOT/scripts/record-gate.py" --init <tier> --branch "$BRANCH" --route-from "$PWD"
```

### Check status

```bash
WT="${WT:-.}"
BRANCH=$(git -C "$WT" rev-parse --abbrev-ref HEAD)
python3 "$CLAUDE_PLUGIN_ROOT/scripts/record-gate.py" --status --branch "$BRANCH"
```

## Gate chain

The full chain for **Small-medium+**:

The **Key** column is the exact gate name `record-gate.py` accepts and stores. `tests`/`lint`/`typecheck` are recorded manually; `simplify` and the `grumpy-*` gates auto-record when their skill runs.

| # | Gate | Key | How | Pass when |
|---|------|-----|-----|-----------|
| 1 | **Test** | `tests` | Auto-detect runner (see below) | Exit 0 |
| 2 | **Simplify** | `simplify` | If `/simplify` is available: run it. Otherwise: review for unnecessary complexity, dead code, over-abstraction — but see the note below, the gate cannot be recorded | No actionable findings, or findings fixed |
| 3 | **Review** | `grumpy-review` | If `grumpy` plugin installed: `/grumpy:review`. Otherwise: self-review for correctness bugs, edge cases, security | No critical findings |
| 4 | **Fix** | `grumpy-fix-post-review` | If grumpy: `/grumpy:fix`. Otherwise: fix findings from step 3 | All critical findings resolved |
| 5 | **Imagine** | `grumpy-imagine` | If `grumpy` plugin installed: `/grumpy:imagine`. Otherwise: mental production walkthrough | No critical findings |
| 6 | **Fix** | `grumpy-fix-post-imagine` | Fix findings from step 5 | All critical findings resolved |
| 7 | **Lint** | `lint` | Auto-detect linter (see below) | Exit 0 |
| 8 | **Typecheck** | `typecheck` | Auto-detect checker (see below) | Exit 0 |

Gates 2-6 are **skill-gated**: they are recorded only by the auto-record hook when the skill itself runs. `record-gate.py --record simplify` (or any `grumpy-*` gate) is refused — by the hook, and by the store behind it, however you spell it. The self-review in the "Otherwise" column is a quality practice, not a way to satisfy the gate.

**Gate 1 proves the suite ran, not that it means anything.** Exit 0 is satisfied
by tests that assert nothing and by source files no test touches. When the diff
adds real behavior, deepen gate 1 before recording it:
`/sdlc:test-gaps` (changed source with no covering test) and `/sdlc:test-critique`
(assertions that can't fail, over-mocking, unjustified skips). Neither is a
separate gate — they inform whether `tests` should be recorded at all.
`/sdlc:test-flake` is for a suite that passes inconsistently; run it when a gate-1
failure doesn't reproduce.

For **Tiny** (≤3 lines, no executable code): gates 1, 7, 8 only.

For **Docs-only** (no executable files in the diff, any size): use the `tiny` cycle (`--init tiny`). Gates 1, 7, 8 are **vacuously satisfied** — there is no executable change to test, lint, or type-check — so record them directly and skip gates 2–6. Confirm first with `git diff --name-only origin/main...HEAD`: only `.md`/text/asset paths qualify. One executable file and it is no longer docs-only.

## Auto-detection

Detect the project's toolchain by checking for config files:

**Test runner:**
```
bun.lockb / bunfig.toml → bun test
package-lock.json / yarn.lock → npm test
pnpm-lock.yaml → pnpm test
pyproject.toml / setup.py → pytest
Cargo.toml → cargo test
go.mod → go test ./...
Makefile (has "test" target) → make test
molecule/ → molecule test
ansible.cfg + playbooks (no molecule/) → ansible-playbook --syntax-check <playbooks>
```

**Linter:**
```
biome.json / biome.jsonc → bunx biome check . (or npx)
.eslintrc* / eslint.config.* → npm run lint (or bun run lint)
pyproject.toml [tool.ruff] → ruff format --check . && ruff check .
.golangci.yml → golangci-lint run
Cargo.toml → cargo clippy
ansible.cfg / .ansible-lint → ansible-lint   (run yamllint too if .yamllint present)
.yamllint / .yamllint.yml / .yamllint.yaml → yamllint .
```

Ansible/IaC repos are easy to miss: they have no package manifest, so without
these lines the lint gate silently does nothing. If `.ansible-lint`/`.yamllint`
exist, the lint gate is `ansible-lint` + `yamllint .` — not a no-op.

If the project has a `lint` or `lint:fix` script in package.json, prefer that.

**Type checker:**
```
tsconfig.json → tsc --noEmit (or bun run typecheck if script exists)
pyproject.toml [tool.pyright] → pyright
pyproject.toml [tool.mypy] → mypy .
```

## Task tracking

Use `TaskCreate` to create all gates as discrete tasks before running any of them. Mark each complete with `TaskUpdate` as it passes. This is the state that survives context compaction and agent handoffs. Check tasks first when resuming work.

## Execution

Run each gate in order. After each gate passes:
1. **For gates 1, 7, and 8 only** — record it using the **Record a gate** block above, always passing `--branch "$BRANCH"` (resolved from `$WT`) so the gate lands on the worktree's branch, not whatever branch the invoking cwd happens to be on. The store is shared per-repo, so the path resolves correctly from any cwd inside the repo. Do **not** run `--record` for gates 2–6: it is refused by the hook and again by the store, and the refusal is a hard tool-call denial, not a no-op.
2. For the skill-gated gates (2–6) there is nothing to record by hand — but **check that the auto-record actually landed** before moving on:

   ```bash
   python3 "$CLAUDE_PLUGIN_ROOT/scripts/record-gate.py" --oneline --branch "$BRANCH"
   ```

   The hook records on a PostToolUse event. A skip it did not expect — an unreadable or unwritable store, an ambiguous cross-worktree match — exits 2 and says so, so you should see it. The one skip that stays quiet is the normal opt-out: **no cycle for this branch at all**, which looks identical to a clean run. Catching that here costs one command; discovering it at `gh pr create` costs a full re-run of the chain.
3. Mark the corresponding task complete with `TaskUpdate`

On failure:
1. **Stop.** Do not advance to the next gate.
2. **Fix the issue.** Apply the minimal fix.
3. **Reset gate tracking** and re-run from gate 1. A fix for one gate can break an earlier one.

Lint and typecheck run last because the review/fix gates modify code.

## Simplify gate (gate 2)

When grumpy is not installed, perform the simplify review yourself. Run `git diff origin/main...HEAD` and check for:

- Dead code or unused imports
- Over-abstraction (helper functions called once, unnecessary indirection)
- Duplicated logic that could be consolidated
- Overly complex conditionals that could be simplified
- Comments that restate the code

Fix anything you find, then proceed.

## Self-review (gates 3-6, when grumpy is not installed)

When grumpy is not installed, perform the review yourself:

**Review (gate 3):** Read the full diff. Check for:
- Correctness bugs (off-by-one, null handling, race conditions)
- Missing error handling at system boundaries
- Security issues (injection, XSS, credential exposure)
- Missing or incorrect tests

**Imagine (gate 5):** Walk through the change mentally:
- Happy path: does normal usage work?
- Error path: what happens when things fail?
- Edge cases: empty inputs, large inputs, concurrent access
- First deploy: does this need a migration, config change, or restart?

## Post-gate changes

**Gates are not one-time checkboxes.** Any code change after the full chain completes invalidates prior gates. This includes:
- Fixes from review/imagine
- Manual edits
- Lint auto-fixes

After any post-chain modification, reset gate tracking and re-run the full chain from gate 1.

## Rules

- When uncertain about tier, default to Small-medium (full chain).
- The "no logic change" loophole: new guards, cleanup handlers, early returns, error checks are all logic changes. Not Tiny.
- Never skip gates. Never reorder gates. Never mark a gate passed without running it.

## Gotchas

- Gate ordering is load-bearing: earlier gates are cheaper to fix. Running typecheck before review wastes time when review finds logic bugs.
- Post-gate code changes invalidate ALL prior gates. Reset with `--init` and re-run from gate 1. This is mandatory, not advisory.
- When grumpy is not installed, the self-review fallback is significantly less thorough. Acknowledge this in the PR description.
- The simplify gate (gate 2) is the most commonly skipped. If no simplify skill or grumpy plugin is available, do the review manually — extract dead branches, remove over-abstraction, consolidate duplication.
- `--worktree` without `--route-from` is the quiet failure: the bash gates land on `$WT`'s branch and the skill gates land on this session's, so the chain never completes and nothing says why. `--status --branch "$BRANCH"` shows `Driven from:` when the route is in place.
- Gate tracking + enforcement are pure python (stdlib), so the `gh pr create` block works without `bun`. (`bun` may still be the project's *test* runner — that's a separate, per-project toolchain concern.)
- Enforcement is **local-hook state, not a hosted backend**: a PR opened with `--head owner:branch` (a fork) has no cycle in this checkout's shared store, no matter what the fork contributor's own hooks recorded on their side — it reads as "no cycle -> allow" and ships ungated. This system does not defend against unreviewed external contributions; a repo that accepts fork PRs needs a CI-side gate too, not just this hook.
