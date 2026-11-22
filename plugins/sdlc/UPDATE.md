# sdlc UPDATE — v4.17.0

`/sdlc:init` reads this file first (step 0) to learn what this version adds and what an
existing repo may need wired up. If you are installing for the first time, there is
nothing to migrate — read "What a repo gets" below and skip the rest. Notes for older
releases move to [`CHANGELOG.md`](CHANGELOG.md) once superseded.

## New in 4.17.0 — a finished chain stops re-running (sharpen#37)

A two-line fix once took 13 gate rounds. Five changes cut the rounds that review nothing new:

- **The chain is done when gate 8 records.** `gate.md` used to list "fixes from
  review/imagine" as changes that reset the chain, though those fixes are gates 4 and 6
  of it. It now says plainly: don't re-run review to check gate 4's or gate 6's own fixes.
- **`record-gate.py --carry`.** When a commit after the last review gate makes the chain
  stale, `--carry` checks whether it could have changed what the review saw. It carries
  the review gates over a clean rebase (the branch's own changed lines are unchanged; context a lower
  layer rewrote around them doesn't count),
  release bookkeeping (version fields and release notes renumbered while restacking), a
  change to trailing whitespace or blank lines only, safe docs (READMEs, changelogs, prose under `docs/`, images,
  never a command, skill, template, hook or other file an agent executes), or a small
  comment or rename fix with `--reason` (at most 30 changed lines, no new files). A carry
  is recorded as a carry and shown in `--status`, and it re-opens tests, lint and
  typecheck. Anything else still needs a reset. `/sdlc:ship`'s rebase note now points
  at it.
- **A review base for stacked branches.** The chain resolves the base your branch's
  review covers: `record-gate.py --set-base <ref>`, else the open PR's base (via `gh`),
  else the branch's upstream parent, else main. `--carry`, the round counter and
  `--status` use it, and grumpy 2.11+ passes read it from `.claude/sdlc/<branch>/base.json`,
  so a stacked branch no longer reviews (or edits) the layers below it. With an older
  grumpy the passes still diff against the default branch. `--carry` takes the reviewed
  patch against the base it was reviewed on, so a parent that moved between review and
  restack is not read as the branch's own change.
- **One fix run earns one fix gate.** A fix receipt that earned one fix gate can't earn
  the other; grumpy 2.11+ writes `fix-review.json` and `fix-imagine.json` separately.
- **An advisory round counter.** Each `--init` that replaces a cycle with earned skill
  gates is a new round. From round 2, `--init` and `--status` say which round it is and
  how much the branch grew since round 1; from round 3 they suggest shipping and filing
  the rest, or splitting. It never blocks.

**What an existing repo needs to do:** re-run `/sdlc:init` to refresh the managed
`AGENTS.md` block, which now carries the completion rule and `--carry`. Nothing else: the
store gains a `rounds` key on the next `--init` (or auto-init) and a `carries` key on
the first `--carry`, and older cycles read fine. Only 4.17.0 and later read carries: restart sessions,
update pi, and run `/sdlc:gate` once to refresh the pre-push hook, or an older copy will
still call a carried chain stale.

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
