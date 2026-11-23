# sdlc UPDATE — v4.23.0

`/sdlc:init` reads this file first (step 0) to learn what this version adds and what an
existing repo may need wired up. If you are installing for the first time, there is
nothing to migrate — read "What a repo gets" below and skip the rest. Notes for older
releases move to [`CHANGELOG.md`](CHANGELOG.md) once superseded.

## New in 4.23.0 — the no-agent adapters run imagine too

`openai_adapter.py` and `local_llm_adapter.py` now run grumpy's imagine pass as well as its
review: when the manifest declares `imagine`, they run grumpy 2.17's `scripts/oneshot.py
imagine` after the review, which builds its prompts from grumpy's `commands/imagine.md`,
saves `.sharpen/grumpy/<branch>/imagine.md` and leaves a receipt, so
`record-gate.py --verify imagine` records gate 5 and `--status` says
`(via oneshot: <provider> <model>)` next to it.

- **Only a manifest that declares `imagine` changes.** Without it, the adapters behave as in
  4.22. With it, they need grumpy 2.17 or newer (an older grumpy's `oneshot.py` refuses
  `imagine` with exit 2), and check for it up front (below).
- **A review that earns nothing no longer ends the run.** Review and imagine are separate
  gates, so the imagine pass still runs, and the adapter exits 1. A setup error (exit 2: a
  bad key, model or URL) still stops both.
- **Imagine runs on the same commit as review, before any fix.** Nobody runs `/grumpy:fix`
  between them on this path, so the imagine report and receipt cover the pre-fix commit.
  That is deliberate (one run, both reports), not the agent chain's review → fix → imagine
  order. Once you commit fixes, imagine's gate sits on an older commit than `HEAD`, and
  while it is the last review gate recorded the PR is blocked as stale; re-run the adapter
  on the new `HEAD` so both passes see the fixed code.
- **`generic_adapter.py` skips `imagine` too.** An `adversarial` manifest no longer needs a
  shell command mapped for `imagine`; the adapter says the pass comes from grumpy.
- **An existing `x-host-command-map` entry for `imagine` (or `review`) is now ignored.** All
  three adapters skip it and log a line saying `x-host-command-map` maps it and that command
  is ignored. `openai_adapter.py` and `local_llm_adapter.py` run grumpy's paid `oneshot.py
  imagine` in its place, and `generic_adapter.py` runs neither. Remove the entry, or drop
  `imagine` from the manifest to keep the adapters from calling the API for it.
- **Declaring `imagine` needs grumpy 2.17.** The adapters check for grumpy's
  `scripts/oneshot_prompts.py` before any gate command runs, and stop with exit 2 naming
  grumpy 2.17 when it is missing, rather than paying for the review first.
- **The benchmark measures this path.** `scripts/bench-grumpy.py run --host oneshot` (in the
  sharpen repo) runs `oneshot.py` on the same fixtures as Claude Code, Codex and pi. No
  real-model numbers are recorded yet.

## Coming from 4.13 or earlier

Ten releases (4.14 to 4.23) ship together. In upgrade order, what each one asks of an existing repo
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
- **4.21 — plans and reports move out of `.claude/`** to `.sharpen/sdlc/` and
  `.sharpen/grumpy/`. Nothing to migrate: the old folders are still read, never written.
- **4.22 — the no-agent adapters run grumpy's review.** Only a repo that uses
  `openai_adapter.py` or `local_llm_adapter.py` needs anything: grumpy 2.16 reachable
  through `GRUMPY_PLUGIN_ROOT` or `--grumpy`, and the `SHARPEN_LLM_*` variable names.
- **4.23 — the no-agent adapters run imagine too** (above). Only a manifest that declares
  `imagine` changes, and it needs grumpy 2.17.

Upgrade grumpy to **2.17** at the same time. sdlc 4.23 with an older grumpy still records
gates (the adapters' review and imagine steps excepted), but grumpy 2.15 or newer with an
sdlc older than 4.21 never does.

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
