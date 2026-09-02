# sdlc changelog

Version-specific migration notes older than the current release. `UPDATE.md` always covers
only the latest version (`/sdlc:init` reads it as step 0); an entry moves here once a newer
release supersedes it. Nothing reads this file programmatically — it exists so past behavior
changes stay discoverable without bloating either the README or `UPDATE.md`.

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
