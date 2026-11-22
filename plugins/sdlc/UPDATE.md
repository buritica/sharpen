# sdlc UPDATE — v4.14.0

`/sdlc:init` reads this file first (step 0) to learn what this version adds and what an
existing repo may need wired up. If you are installing for the first time, there is
nothing to migrate — read "What a repo gets" below and skip the rest. Notes for older
releases move to [`CHANGELOG.md`](CHANGELOG.md) once superseded.

## New in 4.14.0 — skill gates record from receipts, on every host

Gates 2 through 6 (`simplify` and the grumpy passes) used to be stamped by a `PostToolUse`
hook on the `Skill` tool call. That fired when the skill's instructions loaded, before any
work ran (sharpen#53), it did not fire at all on a cached repeat call (sharpen#52), and
hosts with no such event (Codex, fx, pi) could not record the gates without `--attest`.

A new gate cycle is now **receipt-backed**:

- Each grumpy pass (needs grumpy 2.9.0+) ends by writing
  `.claude/grumpy/<branch>/receipts/<pass>.json`, naming the branch, its current HEAD and
  the sha256 of the report the pass saved. A receipt whose report is gone doesn't count;
  one whose report changed afterwards records with a warning.
- `record-gate.py --verify <simplify|review|fix|imagine|all>` records the gate from a
  receipt that names a commit on the branch (HEAD, or an ancestor once a later fix has
  committed), is newer than the cycle, and (for a fix gate) is newer than the receipt
  of the pass it follows. It works the same on every host.
- Claude Code runs the same check on its `Stop` hook and pi on `agent_end`, so nothing
  extra is needed there. The `Skill` hook still fires, but now only records once the
  receipt exists.
- `enforce-sdlc-gates` blocks `gh pr create` when the last review gate was earned on
  an older commit than HEAD, and names the stale gates. Re-run the chain on the new
  HEAD. A rebase counts, so rebase before the chain (`/sdlc:ship` now says so).
  `--status` and `--oneline` show the same warning, so it doesn't surface first at
  `gh pr create`.

**What an existing repo needs to do:** nothing. A cycle that was already open keeps the old
event-only recording until it is re-initialised, and receipts are written under the
already-gitignored `.claude/grumpy/` directory. Update grumpy alongside sdlc: an older
grumpy writes no receipt, so `--verify` will say "no receipt yet". To keep event-only
recording for new cycles, set `SDLC_GATE_EVIDENCE=events`.

## What a repo gets

`/sdlc:init` scaffolds, and `/sdlc:audit` grades, against
[`templates/spec.md`](templates/spec.md):

- **PR validation** with a path gate, concurrency, and least-privilege permissions.
- **`ci-pass`** — the single aggregator job that branch protection requires. This is the
  one authoritative enforcement point; marking path-gated jobs required directly would
  hang a docs-only PR forever.
- **A deploy workflow** (pattern 8) when the repo ships a runtime — derived from the
  repo's own files, on the self-managed vs platform-managed axis. Reversible by
  construction: a retried smoke check, and a rollback guarded on the deploy step actually
  having succeeded.
- **Secret scanning, dependabot, and changed-files pre-commit hooks** as fast local echoes
  — never the enforcement point.
- **Three-tier secrets** via `/sdlc:secrets`, one read-only service account per tier, so a
  leaked CI token cannot reach production.

## Re-running on a repo that already has sdlc scaffolding

`init` is idempotent: it reads what exists, diffs against the spec, and offers to merge in
only what's missing. It does not overwrite hand-tuned steps.

Run `/sdlc:audit` first to see the gap as a severity table. The finding most likely to be
present in a hand-written deploy workflow is **P8-A**: a rollback step conditioned on a
bare `if: failure()` rather than `failure() && <deploy-step>.outcome == 'success'`. Bare
`failure()` fires when *any* earlier step failed — a dependency install, a secret fetch, a
notification with a stale token — so the workflow reverts a production release that was
never touched. Fix that one first.

## Gate cycles

The chain records to `.sharpen/data/gates.json` (an existing install that still only has
`.claude/data/gates.json` keeps using that file until the newer path exists), keyed by
branch and shared across every worktree of the repo. Two things worth knowing before you
run it:

- **Gates 2–6 are skill-gated.** They are recorded only when their skill actually runs.
  `record-gate.py --record grumpy-review` is refused by the hook and again by the store.
  If `grumpy` is not installed, a `small-medium` cycle cannot complete — `/sdlc:gate` says
  so before it initializes rather than after you've done the work.
- **A reset clears the skill-gated gates too**, and they can only be re-earned by running
  their skills again. That is correct — a gate that passed against different code proved
  nothing — but it means "just re-init" is not a cheap reflex. Batch your fixes and reset
  once.
