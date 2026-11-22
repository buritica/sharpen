# sdlc changelog

Version-specific migration notes older than the current release. `UPDATE.md` always covers
only the latest version (`/sdlc:init` reads it as step 0); an entry moves here once a newer
release supersedes it. Nothing reads this file programmatically — it exists so past behavior
changes stay discoverable without bloating either the README or `UPDATE.md`.

## 4.15.0 — each host declares itself, and plugin paths resolve everywhere

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
  `record-gate.py --verify <pass>` after each pass. It names the detected host and prints
  the absolute path of `record-gate.py`. It names any grumpy pass the manifest lacks, except
  `simplify` in a pre-4.15 manifest, which predates that capability. With a grumpy older than 2.9.0 on Claude Code, it arms an event-only cycle
  (Claude Code's `Skill` event can stamp it) instead of a receipt-backed one that could
  never complete. Other hosts have no such event, so they keep the receipt-backed cycle and
  are told gates 2-6 can't record until grumpy is upgraded to 2.9.0+ (`--attest` per gate
  as a last resort).
- **Profiles don't change tiers, and the docs now say so.** `docs/portable-core.md` claimed
  a host must not auto-arm a profile it can't complete; the code never did that, and with
  receipts it shouldn't. The `claude-enforced` profile, which the code never had, is gone
  from the docs.
- **A host support matrix**, [`docs/hosts.md`](https://github.com/buritica/sharpen/blob/main/docs/hosts.md), generated from
  `scripts/hosts.py`, says what Claude Code, Codex, pi and the instructions-only hosts
  (Gemini, Cursor, Copilot, `npx skills add`) each do (sharpen#46).

**What an existing repo needs to do:** nothing. A Claude Code session started before the
upgrade still has its old `capabilities.claude.json` read until the next session writes
`capabilities.claude-code.json`; both live under the gitignored `.sharpen/data/`. Upgrade
grumpy (2.10.0) and sdlc-guardrails (1.4.5) alongside sdlc.

## 4.14.0 — skill gates record from receipts, on every host

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

## 4.13.0 — `/sdlc:ship` drops the post-merge confirmation loop

`/sdlc:ship` no longer runs a post-merge confirmation window; that loop was specific to one
consumer repo, which now owns it in its own `.claude/skills/sdlc/SKILL.md` overlay. Nothing
in an existing repo needs to change. (This release shipped without an `UPDATE.md` section;
4.14.0 adds this note retroactively.)

## 4.12.2 — the Rust lint reference lints test code

`templates/stacks.md` told `/sdlc:init` to render `cargo clippy -- -D warnings`, which does not lint test code. It now renders `cargo clippy --all-targets -- -D warnings`. A repo initialised under 4.12.1 or earlier therefore has a documented lint command weaker than the one this version renders; re-running `/sdlc:init` updates the `Commands` block (edits outside the `sdlc:begin`/`sdlc:end` markers are untouched). Nothing in an existing repo breaks — the stricter flag only adds coverage, and a repo whose test code already passes it sees no change. Repos that want the stricter form now without re-running init can edit the `lint:` line themselves.

## 4.12.1 — host-neutral skill-gate completion adapters

Automatic skill-gate recording now has a host-neutral `record_skill_completion()` interface. The Claude `PostToolUse` hook is a thin adapter over the shared route resolution, ordering, invalidation, and atomic store write behavior. A future host adapter must call it only from a trustworthy successful post-skill callback; it must not turn arbitrary manual recording into hook evidence. fx and Codex currently have no such Sharpen callback, so their documented reason-required `record-gate.py --attest` fallback remains unchanged.

## 4.12.0 — deferring findings with `ponytail:` markers

`/grumpy:fix` (a `grumpy` 2.7.0+ requirement, not something this plugin implements itself) can now defer a non-critical finding into a `ponytail:` comment instead of forcing a fix at review time, and `--file-issues` can open a GitHub issue for the ones that need one. Gates 4 and 6's "all critical findings resolved" pass criterion is unchanged — a deferral is never accepted for a Critical finding, on this repo or any other consuming sdlc. Nothing about the gate keys, the store, or the enforcement hook changed; this is purely about what counts as "resolved" for a Serious or Questionable finding. See the grumpy plugin's own README, "Deferring findings", for the full eligibility table.

Moved here from `UPDATE.md` by 4.12.2, which superseded it. It had been left in place when 4.12.1 released, so for one release the file claimed to cover v4.12.0 while the plugin shipped 4.12.1 — this entry corrects that rather than quietly dropping the notes.

## 4.11.0 — the contract lives in AGENTS.md

Step 10 of `/sdlc:init` used to append a three-line gate reminder to `CLAUDE.md` and nothing
else. It now renders `templates/agents-sdlc.md` with the toolchain it derived and upserts the
result into `AGENTS.md` between `<!-- sdlc:begin -->` / `<!-- sdlc:end -->` markers, and makes
`CLAUDE.md` include it via `@AGENTS.md`. On a repo that already has sdlc scaffolding, re-run
`/sdlc:init`: the block is added (or replaced, if a previous 4.11 run wrote one), hand-written
content in either file is preserved, and the old `## Run gates before every PR` section in
`CLAUDE.md` is removed because the block carries it. `scripts/agents_md.py --root . --check`
shows drift without writing; with no other flags it only compares the version stamped inside
the block against the installed sdlc, and `/sdlc:gate` runs that check once per cycle.

## 4.10.4 — `/simplify` replaced by `/grumpy:simplify`

The gate-2 recorder was a Claude-Code-only bundled skill, invisible to every other host —
unlike gates 3-6, which have always come from the portable `grumpy` plugin. The `simplify`
gate *key* is unchanged (stored JSON, `--record`/`--attest` arguments, all identical); only
the skill that records it changed. Re-running the chain after this upgrade means running
`/grumpy:simplify` where older docs still say `/simplify`.

## 4.5.0 — docs-only commits auto-arm `tiny` instead of `small-medium`

`auto-init-gate-cycle.py` now diffs the branch against `origin/main`/`origin/master`
(falling back to local `main`/`master`) before arming a *new* cycle, and picks `tiny` when
every changed file matches a docs/text/asset allowlist. This only affects the tier a *new*
cycle starts at — an in-flight cycle is untouched, and the tier is decided once, at arm time;
a docs-only first commit followed by a real code commit does **not** auto-upgrade the tier
back to `small-medium` (same as any other post-gate change, a manual
`/sdlc:gate --init small-medium` is required). If you were relying on every branch starting
`small-medium` regardless of content, that assumption no longer holds for docs-only branches.

## 4.2.1 — skill-gated gates are enforced in the store

Until 4.2.0 the block on manually recording `simplify`/`grumpy-*` lived only in the
PreToolUse hook, so any form it couldn't parse — an `eval`, an inline `python3 -c`, a
generated script — recorded the gate. Those gates are now refused by `record_gate()` itself.
Four things to know (`/simplify` below is this release's actual command name, later replaced
by `/grumpy:simplify` in 4.10.4):

- An in-flight cycle needs no migration; the store format is unchanged and the remaining
  gates record normally when their skills run.
- A one-line fix after the chain completes really does mean re-running `/simplify` and the
  `grumpy` skills, since there is no longer a way to hand-stamp the re-run.
- Piping a payload to `auto-record-skill-gate.py` from Bash is now blocked; call it from
  python if you are testing the hook.
- **If you don't have `/simplify` and grumpy installed, this is the release where that stops
  being survivable.** `auto-init-gate-cycle.py` starts a `small-medium` cycle on the first
  commit to any non-default branch, and gates 2-6 can now only be recorded by their skills —
  the manual `--record` that used to slip through is gone. Install the skills, or start the
  branch with `/sdlc:gate --init tiny` where the change genuinely qualifies. Editing
  `gates.json` by hand is the last resort and the store does not stop you; it is a deliberate
  hole, not an oversight.

## 2.2.0 — per-worktree store

Main-checkout users are unaffected — the path is identical across versions. But if you had
an in-flight gate cycle on a **linked worktree**, its state lived in that worktree's
`.claude/data/gates.json` and won't be seen at the new shared location. Re-run `/sdlc:gate`
on that branch to re-establish the cycle (gates are cheap to re-run and reset on any code
change anyway).
