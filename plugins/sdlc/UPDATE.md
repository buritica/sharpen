# sdlc UPDATE — v4.11.0

`/sdlc:init` reads this file first (step 0) to learn what this version adds and what an
existing repo may need wired up. If you are installing for the first time, there is
nothing to migrate — read "What a repo gets" below and skip the rest.

## New in 4.11.0 — the contract lives in AGENTS.md

Step 10 of `/sdlc:init` used to append a three-line gate reminder to `CLAUDE.md`
and nothing else. It now renders `templates/agents-sdlc.md` with the toolchain it
derived and upserts the result into `AGENTS.md` between `<!-- sdlc:begin -->` /
`<!-- sdlc:end -->` markers, and makes `CLAUDE.md` include it via `@AGENTS.md`.

On a repo that already has sdlc scaffolding, re-run `/sdlc:init`: the block is
added (or replaced, if a previous 4.11 run wrote one), hand-written content in
either file is preserved, and the old `## Run gates before every PR` section in
`CLAUDE.md` is removed because the block carries it. If `CLAUDE.md` holds other
repo rules, move them to `AGENTS.md` so non-Claude hosts read them too.
`scripts/agents_md.py --root . --check` shows drift without writing.

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
