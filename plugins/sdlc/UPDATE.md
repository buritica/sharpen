# sdlc UPDATE — v4.21.0

`/sdlc:init` reads this file first (step 0) to learn what this version adds and what an
existing repo may need wired up. If you are installing for the first time, there is
nothing to migrate — read "What a repo gets" below and skip the rest. Notes for older
releases move to [`CHANGELOG.md`](CHANGELOG.md) once superseded.

## New in 4.21.0 — plans and reports move out of `.claude/`

Plans and specs are now written to `.sharpen/sdlc/<branch>/`, and grumpy 2.15 writes its
reports and pass receipts to `.sharpen/grumpy/<branch>/`. Headless Claude Code refuses to
write under `.claude/`, which it treats as its own configuration, so a pass run with
`claude -p` printed its report instead of saving it. `.sharpen/` already held gate state,
and no host treats it specially.

- **Nothing to migrate.** The old `.claude/sdlc/` and `.claude/grumpy/` folders are still
  read, and never written. A receipt in both places counts by whichever copy is newer. A
  plan or `fix.md` that only exists in the old folder is copied into `.sharpen/` the first
  time a pass appends to it (its Settled and Deferred entries come along). Other reports,
  specs and `base.json` are read from the new folder first and the old one when the new
  copy is missing.
- **Rolling back** to sdlc 4.20 and grumpy 2.14 means copying back what was written since,
  because they read only `.claude/`:
  `mkdir -p .claude/sdlc .claude/grumpy && cp -R .sharpen/sdlc/. .claude/sdlc/ && cp -R .sharpen/grumpy/. .claude/grumpy/`.
- **Upgrade grumpy to 2.15 with this release.** They share the path contract. With an
  older grumpy, receipts still record (the fallback above), but `/grumpy:review` and
  `/grumpy:imagine` look for the plan only under `.claude/sdlc/`, so they miss a plan this
  version wrote, and its `diff_base.py` misses the stack base (`base.json`) and resolves the
  base on its own.
- **`/sdlc:init` adds `.sharpen/sdlc/` and `.sharpen/grumpy/` to its ignore block.** Both
  folders also ignore themselves when created, so a repo that skips init stays clean.
  `.sharpen/` as a whole is still never ignored, because `.sharpen/simplify.json` is
  committed. The `.claude/sdlc/` and `.claude/grumpy/` lines stay, for checkouts that
  still hold old files there.
- **A team that commits its plans** keeps the ones already tracked under `.claude/sdlc/`.
  New plans land in `.sharpen/sdlc/`, which ignores itself; put `!*` in
  `.sharpen/sdlc/.gitignore` (deleting it doesn't last) to commit them there.
- `/sdlc:plan` worktrees stay under `.claude/worktrees/`. `git worktree add` creates them
  from a shell, which Claude Code's check on its file tools doesn't cover.

## Coming from 4.13 or earlier

Eight releases (4.14 to 4.21) ship together. In upgrade order, what each one asks of an existing repo
(full notes for each are in [`CHANGELOG.md`](CHANGELOG.md)):

- **4.14 — skill gates record from receipts.** Each grumpy pass leaves a receipt naming the
  commit it ran on, and `record-gate.py --verify <pass>` records the gate from it, on every
  host. Nothing to do; upgrade grumpy with sdlc (see the bottom of this list).
- **4.15 — each host declares itself.** The capability manifest becomes
  `.sharpen/data/capabilities.<host>.json`, and plugin paths in skills are spelled so they
  resolve on Claude Code, Codex and pi. Nothing to do; `docs/hosts.md` is the support matrix.
- **4.16 — every way to open a PR is gated**, plus a git pre-push hook as the floor under
  it. Nothing to do; auto-init installs the hook.
- **4.17 — a finished chain stops re-running.** `--carry` carries the gates over a clean
  rebase or a safe docs commit, and `--set-base` pins a stacked branch's review base.
- **4.18 — artifact folders ignore themselves.** If a repo committed any of them, untrack
  them once with `git rm -r --cached` (now naming the `.sharpen/` folders below).
- **4.19 — the gate runs the commands AGENTS.md declares.** Fix a wrong declared command by
  re-running `/sdlc:init` step 10.
- **4.20 — CI no longer checks gate state.** Delete a `gates` CI job if your repo has one.
- **4.21 — plans and reports move out of `.claude/`** (above).

Upgrade grumpy to **2.15** at the same time. sdlc 4.21 with an older grumpy still records
gates, but grumpy 2.15 with an older sdlc never does.

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
