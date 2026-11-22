# sdlc UPDATE — v4.15.0

`/sdlc:init` reads this file first (step 0) to learn what this version adds and what an
existing repo may need wired up. If you are installing for the first time, there is
nothing to migrate — read "What a repo gets" below and skip the rest. Notes for older
releases move to [`CHANGELOG.md`](CHANGELOG.md) once superseded.

## New in 4.15.0 — each host declares itself, and plugin paths resolve everywhere

- **Plugin paths in skill text work on Claude Code again.** Claude Code fills in only the
  braced `${CLAUDE_PLUGIN_ROOT}` in a plugin's command text and does not export it to the
  agent's shell, so the unbraced `$CLAUDE_PLUGIN_ROOT` calls in `/sdlc:gate` pointed at
  `/scripts/...`. Every plugin path is now spelled with the plugin's own variable first,
  `${SDLC_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}`, and `scripts/check-marketplace.py` enforces it.
- **pi stops sharing one plugin root.** The pi extension used to set `CLAUDE_PLUGIN_ROOT` to
  sdlc's directory for the whole process, so grumpy's simplify and fix looked in the wrong
  plugin. It now exports `SDLC_PLUGIN_ROOT` and `SHARPEN_HOST=pi`, and no extension sets the
  shared name.
- **The capability manifest names the real host.** `scripts/claude-session-start.py` is now
  `scripts/session-start.py`. It writes `.sharpen/data/capabilities.<host>.json`, with the
  host from `SHARPEN_HOST`, else Claude Code's `CLAUDECODE=1`, else `unknown` (Codex reports
  `unknown` until a signal is verified). It also records `x-recording` (does this host sweep
  receipts by itself) and `x-receipts` (does the installed grumpy write them), and lists
  `simplify` as a capability.
- **Auto-init says how gates record on this host.** The arming message tells a Claude Code
  or pi session that receipts record at the end of the turn, and anyone else to run
  `record-gate.py --verify <pass>` after each pass. It names any grumpy pass the manifest
  lacks. With a grumpy older than 2.9.0 on Claude Code, it arms an event-only cycle
  (Claude Code's `Skill` event can stamp it) instead of a receipt-backed one that could
  never complete. Other hosts have no such event, so they keep the receipt-backed cycle and
  are told gates 2-6 can't record until grumpy is upgraded to 2.9.0+ (`--attest` per gate
  as a last resort).
- **Profiles don't change tiers, and the docs now say so.** `docs/portable-core.md` claimed
  a host must not auto-arm a profile it can't complete; the code never did that, and with
  receipts it shouldn't. The `claude-enforced` profile, which the code never had, is gone
  from the docs.
- **A host support matrix**, [`docs/hosts.md`](../../docs/hosts.md), generated from
  `scripts/hosts.py`, says what Claude Code, Codex, pi and the instructions-only hosts
  (Gemini, Cursor, Copilot, `npx skills add`) each do (sharpen#46).

**What an existing repo needs to do:** nothing. A Claude Code session started before the
upgrade still has its old `capabilities.claude.json` read until the next session writes
`capabilities.claude-code.json`; both live under the gitignored `.sharpen/data/`. Upgrade
grumpy (2.10.0) and sdlc-guardrails (1.4.5) alongside sdlc.

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
