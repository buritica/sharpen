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

### A skill-gated gate that won't stamp in a long session

**(Claude Code specific.)** If a `/grumpy:*` skill genuinely ran
— real findings, real artifacts written — but its gate never landed in
`--status`, suspect Claude Code's Skill-tool instruction caching before
assuming the hook is broken: re-invoking a skill already loaded earlier in a
long session can make Claude Code's Skill tool return "already loaded above,
instructions unchanged" instead of dispatching fresh. Reported behavior
(sharpen#11) is that `auto-record-skill-gate.py`'s PostToolUse hook does not
fire for that cached response, since it only fires on a genuine dispatch —
but this hasn't been pinned down as fully reliable across sessions, so a
stamp that lands anyway on a re-invocation isn't a sign the hook is
misbehaving. This entire failure mode, and the fix below, is specific to how
Claude Code's Skill tool caches instructions; it doesn't generalize to other
hosts.

Fix it forward, in order:

1. **Re-run the skill from a fresh subagent.** A clean context has no cached
   instructions to short-circuit, so Claude Code's Skill tool dispatches for
   real and the hook fires normally. If that subagent isn't the worktree
   driving the branch's cycle, route it first (`--route-from`, as above) so
   its stamp lands on the right branch.
2. **Last resort:** `record-gate.py --attest <gate> --reason "<text>"`.
   This stamps the gate on human attestation instead of a hook observation —
   it requires a reason and marks itself in `--status`/`--oneline` with `⚠`
   rather than `✓`, so a reader can tell it apart from a hook-verified stamp.
   That distinction is for the reader, not the enforcer: `gh pr create`
   unblocks on an attested gate exactly as it would on a hook-verified one —
   `enforce-sdlc-gates.py` only checks whether the gate is stamped, not how.
   Use it only when re-dispatching genuinely isn't practical, and say in the
   PR description that a gate was attested rather than hook-verified, and why.

### Cross-repo routing (sharpen#10)

`--worktree`/`--route-from` were originally built for "this session's cwd and
`$WT` are different worktrees of the **same** repo," where the shared store
(one file, keyed by `git rev-parse --git-common-dir`) made cwd mostly not
matter. A session whose home project is a completely unrelated repo — e.g. an
assistant session rooted in repo A, asked to fix repo B's tooling in a
worktree of B — is a real, supported case too, but it needs one more piece:
the auto-record hook resolves ITS OWN git state (which store to open, which
branches are checked out) from the session's own cwd, and for this topology
that cwd is repo A, which has no idea repo B or its branches exist. Without
anything else, the route would record in B's store at `--init` time but never
actually fire when `/grumpy:*` runs later.

`--init --route-from <path>` closes this by **also registering the route** in
a small, repo-independent file (`gate_store.cross_repo_registry_path()`,
default `~/.cache/sharpen/cross-repo-routes.json`, keyed by `<path>`'s own
canonical root) whenever `<path>` resolves to a genuinely different repository
(a different `git rev-parse --git-common-dir`) than the one `--init` is
running against. The auto-record hook checks this registry first, before it
ever resolves a store from its own cwd — a hit redirects every git question
in that invocation (which store, which branches are checked out) to the
target repo, not the session's own. A same-repo, different-worktree route
(the original, still-supported case) never touches this registry at all — it
doesn't need to, since the store is already shared there.

**`--route-from`/`--branch` still refuse when they can't be verified**, rather
than guess:
- `--route-from <path>` is refused when either side's `git rev-parse
  --git-common-dir` is unresolvable (not a repo, broken `.git`) — that's the
  same uncertainty a confirmed mismatch represents, not a reason to proceed.
- `--branch <name>` is refused when `<name>` isn't a real branch (local, or a
  remote-tracking ref — a fetched-but-not-yet-checked-out branch still
  counts) in the repo this process's cwd resolves to. This is the most common
  way to reach a wrong-repo mistake without even using `--route-from`:
  `--branch` names a branch in some OTHER repo entirely while the command
  runs against whatever repo the cwd happens to be, and without this check it
  would silently create a cycle entry for a branch that repo has never heard
  of.

**Check the exit code.** Like every other `record-gate.py` refusal, this is a
nonzero exit with the reason on stderr, not a hook payload a caller can miss —
but nothing forces the invoking skill to look. If you're scripting `--init`
(the "Initialize" block above), treat a nonzero exit here the same as a failed
`--record`: stop and read the message rather than proceeding to the next gate
as if the cycle had actually started.

**`--unroute` follows the registry too.** Run from the source worktree (the
one that originally ran `--route-from`), it checks the cross-repo registry
first — a hit clears the route from the TARGET's store (not whatever repo
`--unroute` itself happens to run in) and drops the registry entry. This
matters because before this, `--unroute` resolved its store the same
cwd-only way `--init` used to: for a cross-repo route it always opened the
CURRENT process's own store, which never had the route in it, and reported
"this worktree was not driving another worktree's gates" — true of that file,
misleading about the actual route. There is no `SDLC_GATES_PATH` override
needed for this anymore.

**Known residual gap:** a session's Bash-tool cwd and the cwd the harness
reports on a hook payload are not guaranteed to be the same thing — a
subagent whose shell is genuinely rooted in the target repo has, in some
environments, still been observed reporting its parent session's cwd on the
`Skill` tool's `PostToolUse` payload. When that happens, `cwd` (and therefore
`source_root`, the registry lookup key) is wrong regardless of how correctly
routing itself is configured, and no fix in this file can see past a payload
that already reports the wrong location. If `--status` and the registry both
show a route configured correctly and a gate still won't record, suspect this
before re-checking the routing setup itself — re-running from a session whose
*top-level* cwd (not just a subagent's) is genuinely inside the target repo
is the reliable workaround.

## Prerequisites check

Before initializing the gate cycle, detect which optional capabilities are available and announce the mode.

**Skill detection — do NOT probe the filesystem.** Detect by **command availability**: `/grumpy:simplify`, `/grumpy:review`, `/grumpy:fix`, and `/grumpy:imagine` are listed in your available skills/commands for this session if and only if they are installed. Check that list directly — do not run `ls "$CLAUDE_PLUGIN_ROOT/../grumpy/..."`, since `$CLAUDE_PLUGIN_ROOT` is empty in the skill-execution context and that probe always fails.

Gate 2 is skill-gated exactly like 3–6: the `simplify` gate is stamped only when `/grumpy:simplify` runs, and a manual `--record simplify` is refused by the store. All four skills ship together with the `grumpy` plugin — but check **gate 2 separately from gates 3–6**, not as one bundle. `/grumpy:simplify` was added later than `/grumpy:review`/`/grumpy:imagine`/`/grumpy:fix`, so a `grumpy` install one version behind has the trio but not yet the fourth skill; treating all four as a single yes/no would downgrade gates 3–6 to the weaker fallback too, on an install where they'd work fine.

- `/grumpy:review`, `/grumpy:fix`, and `/grumpy:imagine` all available → **grumpy mode** for gates 3–6, independent of gate 2's own check below.
- `/grumpy:simplify` available → **grumpy mode** for gate 2, independent of the trio's own check above.
- Whichever of the two checks fails → **self-review fallback** for that gate (or gates) only. Announce it loudly: `⚠️ grumpy incomplete — gate 2 uses the weaker self-review fallback` (or the equivalent for gates 3–6, or both), and note it in the PR description.
  **The fallback cannot satisfy a skill-gated gate when the hooks are installed.** That gate is recorded only by the auto-record hook when the real skill runs; `record-gate.py --record grumpy-review` (or `--record simplify`) is refused by the store, by design. So with hooks registered and the corresponding skill absent, a `small-medium`/`significant` cycle never completes and `gh pr create` stays blocked.
  **Say this before you initialize, not after the work is done.** If the tier you are about to init is `small-medium` or `significant` and any of the four skills is unavailable, stop and tell the user: the cycle cannot complete, and their options are to install the missing skill or (only if the change genuinely qualifies) run `tiny`. Failing at init costs them a sentence; failing at `gh pr create` costs them the whole chain. Do not present the self-review as having satisfied a gate it cannot record.
- If you are genuinely unsure, **ask** rather than silently downgrading.

Gate tracking + enforcement are **pure python (stdlib)** — no `bun`, no external runtime. So enforcement works on any box: the `enforce-sdlc-gates.py` hook blocks `gh pr create` when gates are incomplete. State the mode in one line, e.g. `Mode: grumpy + enforced gates`.

## Is AGENTS.md current?

If the repo has an `AGENTS.md` with a managed SDLC block, check it before
initializing — one command, no toolchain needed:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/agents_md.py" --root "${WT:-.}" --check
```

`stale` means the block was rendered by an older sdlc than the one enforcing
this cycle (tiers or the gate chain may have changed); say so in one line and
suggest re-running `/sdlc:init` step 10. It does not block the chain. A repo
with no block at all is simply pre-4.11; mention it once.

## Gate tracking

Gates are tracked in a single JSON file **shared across every worktree of the repo**, keyed by branch. `scripts/record-gate.py` writes it at `<main-checkout>/.sharpen/data/gates.json` — the path is resolved via `git rev-parse --git-common-dir`, which points at the main checkout's `.git` from any linked worktree, so every worktree (and any cwd inside the repo) reads and writes the same file (override with `$SDLC_GATES_PATH`). Existing installs that only have `<main-checkout>/.claude/data/gates.json` keep using that file until `.sharpen/data/` exists, so an upgrade does not hide active cycles. The `enforce-sdlc-gates.py` hook reads that same shared file, taking the branch from the `gh pr create` command's `--head` if it has one (normalized, so `owner:branch`, `refs/heads/branch` and the clustered `-Hbranch` resolve to the same key) and otherwise from its `cd`/`git -C` working directory. Because the store is shared and branch-keyed: a cycle recorded in one worktree is visible when the PR is created from another, while two branches checked out in two worktrees stay isolated by their branch key. (Worktree targeting assumes the invoking session is in the same repo as `$WT`.) The `routed_from` entry described above rides in that same shared file, which is how the auto-record hook in this session finds `$WT`'s cycle.

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

**Know the price before you type it.** A reset clears the skill-gated gates too, and those can only be re-earned by running their skills again — there is no `--record` for them. On a `small-medium` cycle that is five invocations across four grumpy skills (`/grumpy:simplify`, `/grumpy:review`, `/grumpy:imagine`, and `/grumpy:fix` twice — once after review, once after imagine). That is the correct cost when the code actually changed (a gate that passed against different code proved nothing), but it means "just re-init" is not a cheap reflex. Batch your fixes and reset once.

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
| 2 | **Simplify** | `simplify` | If `grumpy` plugin installed: `/grumpy:simplify`. Otherwise: review for unnecessary complexity, dead code, over-abstraction — but see the note below, the gate cannot be recorded | With `/grumpy:simplify`: no blocking findings (`new`/`regressed` against the merge base) after the fix pass; legacy debt is reported, not failed. Without it the gate cannot be recorded at all (see note below) |
| 3 | **Review** | `grumpy-review` | If `grumpy` plugin installed: `/grumpy:review`. Otherwise: self-review for correctness bugs, edge cases, security | No critical findings |
| 4 | **Fix** | `grumpy-fix-post-review` | If grumpy: `/grumpy:fix`. Otherwise: fix findings from step 3 | All critical findings resolved |
| 5 | **Imagine** | `grumpy-imagine` | If `grumpy` plugin installed: `/grumpy:imagine`. Otherwise: mental production walkthrough | No critical findings |
| 6 | **Fix** | `grumpy-fix-post-imagine` | Fix findings from step 5 | All critical findings resolved |
| 7 | **Lint** | `lint` | Auto-detect linter (see below) | Exit 0 |
| 8 | **Typecheck** | `typecheck` | Auto-detect checker (see below) | Exit 0 |

Gates 2-6 are **skill-gated**: they are recorded only by the auto-record hook when the skill itself runs. `record-gate.py --record simplify` (or any `grumpy-*` gate) is refused — by the hook, and by the store behind it, however you spell it. The self-review in the "Otherwise" column is a quality practice, not a way to satisfy the gate.

**"All critical findings resolved" (gates 4 and 6) allows one other outcome besides a fix:** a non-critical finding may instead be deferred with a `ponytail:` comment at its site, per `/grumpy:fix`'s eligibility table — Critical findings are never deferred, so this never weakens what "resolved" means for gates 4/6's own pass criterion.

**Gate 1 proves the suite ran, not that it means anything.** Exit 0 is satisfied
by tests that assert nothing and by source files no test touches. When the diff
adds real behavior, deepen gate 1 before recording it:
`/sdlc:test-gaps` (changed source with no covering test) and `/sdlc:test-critique`
(assertions that can't fail, over-mocking, unjustified skips). Neither is a
separate gate — they inform whether `tests` should be recorded at all.
`/sdlc:test-flake` is for a suite that passes inconsistently; run it when a gate-1
failure doesn't reproduce.

### A gate fails for reasons unrelated to your diff (sharpen#4)

Tests, lint, or typecheck can fail because of breakage that predates your branch —
a dependency bump, upstream drift, a flaky suite someone else's change exposed.
When that happens you genuinely cannot record the gate (it isn't passing), which
blocks `gh pr create` — not because of anything in your diff, but because
`enforce-sdlc-gates.py` can't tell "this predates me" from "my change broke it."
**There is deliberately no flag to skip this check** — see
`enforce-sdlc-gates.py`'s own `# Deliberately no escape hatch here` comment, which
already removed one waiver (a gitignore-based exemption) for firing silently on
exactly this kind of ambiguity. A gate that can be talked past isn't a gate.

The fix is always to make the gate genuinely pass, not to skip checking it:

1. **Confirm it's actually unrelated before assuming so.** `git stash` (or check
   out a clean `origin/main`) and re-run the failing gate. If it fails there too,
   it predates your branch. If it passes clean, your diff is the cause — go fix
   that instead, this playbook doesn't apply.
2. **Trivial/mechanical fix** (an unused import, a formatting nit, a one-line
   dependency pin): bundle it into your own PR and say so explicitly in the PR
   body — name what was broken, and that you confirmed it via step 1. This is
   already established practice in this repo's own history (sharpen#16, #10) —
   both bundled a confirmed-unrelated lint fix from a just-merged PR rather than
   shipping a separate follow-up for something that small.
3. **Non-trivial fix** (a real logic bug, a dependency needing an actual upgrade,
   an infra issue): don't scope-creep your feature branch. Cut a separate,
   minimal hotfix branch off `main` first — `tiny` or `small-medium` tier as the
   fix warrants — ship it alone, then rebase your feature branch onto the
   now-green `main` and continue. The hotfix earns its own gates the normal way;
   it is not an exemption, just the fix landing first.
4. **The same problem shows up one level up, at `ci-pass` on GitHub**, if `main`
   moves between your local gate-1 run and the PR's actual CI run — your local
   success doesn't substitute for what CI reports. Rebase and let CI re-validate
   rather than trusting a local pass that's gone stale.

For **Tiny** (≤3 lines, no executable code): gates 1, 7, 8 only.

For **Docs-only** (no executable files in the diff, any size): use the `tiny` cycle (`--init tiny`). Gates 1, 7, 8 are **vacuously satisfied** — there is no executable change to test, lint, or type-check — so record them directly and skip gates 2–6. Confirm first with `git diff --name-only origin/main...HEAD`: only `.md`/text/asset paths qualify. One executable file and it is no longer docs-only.

`auto-init-gate-cycle.py` already runs this same check automatically on the first commit to a new branch — a confidently docs-only diff arms `tiny` on its own, no `--init` needed. This manual path still matters for a branch that *starts* with code (so the hook already armed `small-medium`) but later turns out to be docs-only, or for confirming what the hook decided.

## Auto-detection

Detect the project's toolchain by checking for config files:

**Test runner:**
```
bun.lock* / bunfig.toml → bun test
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
*.sh files present + `shellcheck` on PATH → shellcheck <the *.sh files>
```

Ansible/IaC repos are easy to miss: they have no package manifest, so without
these lines the lint gate silently does nothing. If `.ansible-lint`/`.yamllint`
exist, the lint gate is `ansible-lint` + `yamllint .` — not a no-op.

`shellcheck` is additive, not exclusive: run it alongside whatever else this
table matched whenever the repo has `.sh` files at all (an Ansible repo with
shell scripts under `files/` or `scripts/` needs both ansible-lint AND
shellcheck — CI running four linters while the gate only reproduces one or
two is exactly the false-confidence gap this table exists to close). If
`shellcheck` isn't installed, say so in the gate's mode line rather than
silently skipping it.

If the project has a `lint` or `lint:fix` script in package.json, prefer that.

**Type checker:**
```
tsconfig.json → tsc --noEmit (or bun run typecheck if script exists)
pyproject.toml [tool.pyright] → pyright
pyproject.toml [tool.mypy] → mypy .
```

## Task tracking

Use your harness's task-tracking feature if it has one — one task per gate, created before running any of them, marked complete as each passes. This is the state that survives context compaction and agent handoffs; check it first when resuming work. If your harness has no such feature, keep a plain checklist in your working notes instead.

## Execution

Run each gate in order. After each gate passes:
1. **For gates 1, 7, and 8 only** — record it using the **Record a gate** block above, always passing `--branch "$BRANCH"` (resolved from `$WT`) so the gate lands on the worktree's branch, not whatever branch the invoking cwd happens to be on. The store is shared per-repo, so the path resolves correctly from any cwd inside the repo. Do **not** run `--record` for gates 2–6: it is refused by the hook and again by the store, and the refusal is a hard tool-call denial, not a no-op.
2. For the skill-gated gates (2–6) there is nothing to record by hand — but **check that the auto-record actually landed** before moving on (this hook mechanism is Claude Code specific; see the caveats below):

   ```bash
   python3 "$CLAUDE_PLUGIN_ROOT/scripts/record-gate.py" --oneline --branch "$BRANCH"
   ```

   The hook records on a PostToolUse event. A skip it did not expect — an unreadable or unwritable store, an ambiguous cross-worktree match — exits 2 and says so, so you should see it. The one skip that stays quiet is the normal opt-out: **no cycle for this branch at all**, which looks identical to a clean run. Catching that here costs one command; discovering it at `gh pr create` costs a full re-run of the chain.
3. Mark the corresponding task complete (task-tracking feature or plain checklist, per the note above)

On failure:
1. **Stop.** Do not advance to the next gate.
2. **Fix the issue.** Apply the minimal fix.
3. **Reset gate tracking** and re-run from gate 1. A fix for one gate can break an earlier one.

Lint and typecheck run last because the review/fix gates modify code.

## Self-review (gates 2-6, when grumpy is not installed)

When grumpy is not installed, perform the review yourself:

**Simplify (gate 2):** Run `git diff origin/main...HEAD` and check for:
- Dead code or unused imports
- Over-abstraction (helper functions called once, unnecessary indirection)
- Duplicated logic that could be consolidated
- Overly complex conditionals that could be simplified
- Comments that restate the code

Fix anything you find, then proceed.

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

**Simplify and the fix gates enforce this mechanically for `tests`/`lint`/`typecheck`, so you don't have to remember to.** `simplify`, `grumpy-fix-post-review`, and `grumpy-fix-post-imagine` all pass by *editing code*, not just reporting on it — see each one's own "no actionable findings, or findings fixed" criterion above. `record_gate()` clears any already-recorded `tests`/`lint`/`typecheck` the instant one of those three records, so a stamp from before the edit can never silently outlive it: `--status` shows them missing again, and `gh pr create` blocks until they're re-run. This closes exactly the gap this section used to only describe in prose — but it covers those three gates specifically, not every case. A manual edit you make yourself outside any gate skill, or a fix you apply from `grumpy-review`/`grumpy-imagine` findings without going through `/grumpy:fix`, still needs the full manual reset above; nothing watches for those.

A cleared gate isn't left looking identical to one that never ran: `--status` marks it `(cleared by <gate> at <timestamp>)`, and the `gh pr create` denial explains the same thing in prose, so you don't have to already know to go check `--status` to tell "never ran" from "ran, then got invalidated."

## Rules

- When uncertain about tier, default to Small-medium (full chain).
- The "no logic change" loophole: new guards, cleanup handlers, early returns, error checks are all logic changes. Not Tiny.
- Never skip gates. Never reorder gates. Never mark a gate passed without running it.

## Gotchas

- Gate ordering is load-bearing: earlier gates are cheaper to fix. Running typecheck before review wastes time when review finds logic bugs.
- Post-gate code changes invalidate ALL prior gates. Reset with `--init` and re-run from gate 1. This is mandatory, not advisory — `simplify`/`grumpy-fix-post-review`/`grumpy-fix-post-imagine` enforce it mechanically for `tests`/`lint`/`typecheck` (see "Post-gate changes" above), but a manual edit or a fix applied outside `/grumpy:fix` is not covered and still needs the full reset.
- When grumpy is not installed, the self-review fallback is significantly less thorough. Acknowledge this in the PR description.
- The simplify gate (gate 2) is the most commonly skipped. If no grumpy plugin is available, do the review manually — extract dead branches, remove over-abstraction, consolidate duplication.
- `--worktree` without `--route-from` is the quiet failure: the bash gates land on `$WT`'s branch and the skill gates land on this session's, so the chain never completes and nothing says why. `--status --branch "$BRANCH"` shows `Driven from:` when the route is in place.
- Gate tracking + enforcement are pure python (stdlib), so the `gh pr create` block works without `bun`. (`bun` may still be the project's *test* runner — that's a separate, per-project toolchain concern.)
- Enforcement is **local-hook state, not a hosted backend**: a PR opened with `--head owner:branch` (a fork) has no cycle in this checkout's shared store, no matter what the fork contributor's own hooks recorded on their side — it reads as "no cycle -> allow" and ships ungated. This system does not defend against unreviewed external contributions; a repo that accepts fork PRs needs a CI-side gate too, not just this hook.
