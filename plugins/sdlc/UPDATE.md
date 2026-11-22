# sdlc UPDATE — v4.20.0

`/sdlc:init` reads this file first (step 0) to learn what this version adds and what an
existing repo may need wired up. If you are installing for the first time, there is
nothing to migrate — read "What a repo gets" below and skip the rest. Notes for older
releases move to [`CHANGELOG.md`](CHANGELOG.md) once superseded.

## New in 4.20.0 — CI no longer checks gate state

`ci-validate.py` and the `gates` CI job it backed are gone. They read the gate store in CI,
but the store is gitignored, so the job never had a store to read and always passed. sharpen
is local and pre-PR: the gate chain runs on your machine and blocks the PR (every path 4.16.0
gates, plus the pre-push floor); CI checks the code through `ci-pass`, and never gate
evidence.

- **If your CI has a `gates` job** that runs `plugins/sdlc/scripts/ci-validate.py`, delete
  the job and drop it from `ci-pass`'s `needs`. `/sdlc:init` never scaffolded one and the
  script only ever existed in sharpen's own checkout, so most repos have nothing to do.
- `record-gate.py --attach-review` and `--status` are unchanged: an attached review report is
  still shown locally.
- The README, this plugin's README, `docs/portable-core.md` and `/sdlc:gate` now describe
  the local gate as the product and state its limit: opt-out per branch, not adversarial.

## New in 4.19.0 — the gate runs the commands AGENTS.md declares

`/sdlc:init` (step 10) writes the repo's test, lint, format and typecheck commands into
the AGENTS.md managed block, but `/sdlc:gate` ignored them and re-detected from its own
table. On a repo whose real runner isn't the obvious one (this repo runs
`python3 scripts/run-tests.py`; the table said `pytest`), the gate ran something other
than what the repo says it runs.

- `/sdlc:gate` now reads the block first, through the new `scripts/declared_commands.py`
  (prints the declared commands as JSON; "not configured" reads back as null).
- **AGENTS.md wins** when it and the detection table disagree. The table only fills a
  command the block leaves null, or all of them in a repo with no block.
- The gate names the source of each command in its mode line, e.g.
  `Commands: AGENTS.md (test, lint); detected (typecheck)`.
- A block stamped by an older sdlc is still read (it is still the repo's declaration).
- A declared command whose program isn't installed (`command -v` can't find it) is
  reported as a wrong declaration: the gate runs the detected command for that run and
  suggests re-running `/sdlc:init` step 10. A declared command that starts and then
  fails fails the gate, whatever its exit code. Declared commands run from the repo root.
- Monorepo entries (`web: bun test`) run per area, split at the first colon; `format` is never run by a gate;
  shellcheck still runs alongside a declared `lint` that doesn't include it.
- A line edited inside the markers past parsing (`make test (needs docker)`, an item
  without backticks, `- Test:`, a duplicate entry) is reported as unreadable and falls
  back to detection, instead of failing silently.

**What an existing repo needs to do:** nothing. A repo without the block behaves exactly
as before. A repo with one now runs what it declares; if a declared command is wrong or
out of date, fix it by re-running `/sdlc:init` step 10, not by editing inside the markers.

## New in 4.18.0 — artifact folders keep themselves out of git

Gate state lives in `.sharpen/data/` (since the neutral-root move), but `/sdlc:init`'s
ignore block still listed only the `.claude/` folders, so a repo initialised by an earlier
version can have its gate state untracked and one `git add -A` away from a commit. And a
repo that never ran init (the auto-init hook arms a cycle on the first commit anyway, and
grumpy runs without sdlc) had nothing ignored at all.

- Every folder sharpen writes inside a repo now **ignores itself** when it is created:
  `.sharpen/data/`, `.claude/sdlc/`, `.claude/grumpy/` and `.claude/worktrees/` each get
  a `.gitignore` holding `*`. This works on every host, needs no init, and an existing
  folder gets the file on its next write. `.sharpen/` itself never gets one, because the
  committed `.sharpen/simplify.json` lives there.
- `/sdlc:init`'s ignore block adds `.sharpen/data/`, checks coverage with
  `git check-ignore` instead of by eye, and never adds a blanket `.sharpen/`.
- The top-level pi bundle's version now moves with sdlc, and `check-marketplace.py`
  enforces it (it had drifted to 4.12.1). The pi install URLs point at
  `github.com/buritica/sharpen` instead of a placeholder.

**What an existing repo needs to do:** nothing required; the next gate write adds
`.sharpen/data/.gitignore`. To make the repo's own `.gitignore` say so too, re-run
`/sdlc:init` (it appends only the missing `.sharpen/data/` line) or add it by hand. If
`git status` already shows `.sharpen/data/` as untracked, it clears on the next write.
A repo whose live gate state is still in the legacy `.claude/data/` gets no self-ignore
there (sharpen shares that folder and never marks it); keep its `.gitignore` line.
A repo that already *committed* any of these folders has to untrack them once: an
ignore file never untracks anything, so a tracked `gates.json` or `review.md` keeps
showing as modified, and a grumpy receipt refuses a dirty tree. Run
`git rm -r --cached .sharpen/data .claude/sdlc .claude/grumpy` (whichever exist) and
commit that.
Also dropped from this repo's own `.gitignore`: `.pr-body.md`, which nothing writes any more.

Two side effects of the folders now being *ignored* rather than merely untracked:

- `git clean -fdX` (a common `make clean`) now removes gate state and grumpy receipts in a
  repo that never ran init, as it already did in repos that did. A cycle cleaned that way
  loses its recorded gates; re-run the chain.
- A team that commits plans under `.claude/sdlc/` keeps the ones already tracked, but new
  ones are skipped by `git add -A`. Deleting `.claude/sdlc/.gitignore` doesn't last (the
  next command puts it back); replace its contents with `!*` instead, or `git add -f`.
- A plain `git worktree remove` (what `/sdlc:ship` runs) used to refuse a worktree
  holding untracked plans and reports. Now that they're ignored, it succeeds and deletes
  them with the worktree. Copy anything worth keeping out of `.claude/sdlc/<branch>/` or
  `.claude/grumpy/<branch>/` before shipping.

Update grumpy alongside (2.12.0+) so its report folder ignores itself too.

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
