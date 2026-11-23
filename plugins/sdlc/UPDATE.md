# sdlc UPDATE — v4.24.0

`/sdlc:init` reads this file first (step 0) to learn what this version adds and what an
existing repo may need wired up. If you are installing for the first time, there is
nothing to migrate — read "What a repo gets" below and skip the rest. Notes for older
releases move to [`CHANGELOG.md`](CHANGELOG.md) once superseded.

## New in 4.24.0 — a restack that resolved a conflict can carry

`record-gate.py --carry --reason "<what the resolution did>"` now covers a restack that had to
resolve a conflict (sharpen#65). Before, a rewrite carried only when git replayed it cleanly or
the branch's own changed lines came out byte-for-byte the same, so two stack layers editing one
line (a README line that lists the scripts, say) left the upper layer's gates stale after every
restack, even when the resolution just kept both edits.

- **With `--reason`, a rewrite can carry as `manual`.** The lines counted are those where the
  branch's own +/- lines differ from the reviewed ones (a line diff of the two patches, file by
  file), against the same 30-line cap that covers every manual carry in the cycle. What the
  layers below changed elsewhere never counts, and neither does renumbered release bookkeeping
  that would carry on its own. A line both layers edited counts on both sides of the branch's
  patch, so one conflicting line spends 4. It still cannot add a file or change a binary one. The reason is recorded and shown in
  `--status`, and tests, lint and typecheck re-open as after every carry.
- **Without `--reason`, the refusal names the flag**, as it already did for commits on top.
- **Nothing to do in an existing repo.** Re-run `/sdlc:init` step 10 to pick up the reworded
  carry line in the AGENTS.md block, or leave it: the old line is still true.

## Coming from 4.13 or earlier

Eleven releases (4.14 to 4.24) ship together. In upgrade order, what each one asks of an existing repo
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
- **4.23 — the no-agent adapters run imagine too.** Only a manifest that declares
  `imagine` changes, and it needs grumpy 2.17.
- **4.24 — a restack that resolved a conflict can carry** with `--carry --reason` (above).
  Nothing to do.

Upgrade grumpy to **2.17** at the same time. sdlc 4.24 with an older grumpy still records
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
