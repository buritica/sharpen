# sdlc UPDATE — v4.22.0

`/sdlc:init` reads this file first (step 0) to learn what this version adds and what an
existing repo may need wired up. If you are installing for the first time, there is
nothing to migrate — read "What a repo gets" below and skip the rest. Notes for older
releases move to [`CHANGELOG.md`](CHANGELOG.md) once superseded.

## New in 4.22.0 — the no-agent adapters run grumpy's review

`openai_adapter.py` and `local_llm_adapter.py` no longer send a prompt of their own. After
the manifest's gate commands pass, they run grumpy 2.16's `scripts/oneshot.py review`, which
builds its prompts from grumpy's `commands/review.md`, saves a real report to
`.sharpen/grumpy/<branch>/review.md` and leaves the same receipt an agent host leaves. So
`record-gate.py --verify review` now records gate 3 from them, and `--status` names the path:
`grumpy-review — <time> (via oneshot: openai gpt-6.2-astra)`.

- **They need grumpy.** Set `GRUMPY_PLUGIN_ROOT` to the grumpy plugin folder or pass
  `--grumpy <path>`; without it they stop with exit 2.
- **New variable names.** `SHARPEN_LLM_URL`, `SHARPEN_LLM_MODEL` and `SHARPEN_LLM_API_KEY`
  replace `LOCAL_LLM_URL`, `LOCAL_LLM_MODEL` and `LOCAL_LLM_API_KEY`, and `SHARPEN_LLM_MODEL`
  replaces `OPENAI_MODEL`. The old names still work for now and print a deprecation line.
  One precedence change rides along: the variable (`SHARPEN_LLM_MODEL`, or a legacy name
  mapped to it) now beats the manifest's `provider.model`, where before the manifest beat
  `OPENAI_MODEL`/`LOCAL_LLM_MODEL`. A repo that sets both switches to the variable's model
  on upgrade; unset one.
- **No more `gpt-4o-mini` or `llama3.1` defaults.** The model is `SHARPEN_LLM_MODEL`, else
  the manifest's `provider.model`, else, for OpenAI only, grumpy's `models.yaml`
  (`analysis.openai`, currently `gpt-6.2-astra`, unverified until measured). A local
  server has no default and needs a model named. Whatever is named goes to the provider
  as its model id, so a placeholder in the manifest fails the call with exit 1.
- **`generic_adapter.py` no longer attaches a "review" report.** It runs the gate commands
  and prints PASS/FAIL; a gate summary was never a review. `record-gate.py --attach-report`
  still attaches a report you produce yourself.

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
