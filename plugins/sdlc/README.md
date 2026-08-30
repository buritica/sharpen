# sdlc

The full single-repo software lifecycle: new → spec → plan → build → **gate** →
ship → **CI** → **deploy**. The TDD workflow, the quality gate chain, and the
pipeline scaffolding/audit that makes CI the one authoritative enforcement point.

CI is the *automated enforcement surface* of the SDLC loop, not a sibling tool —
so `init` / `audit` / `secrets` (formerly the standalone `ci` plugin) live here,
reading one canonical spec at [`templates/spec.md`](templates/spec.md). The
governing idea: **enforcement is exactly one required CI check; hooks and the
gate chain are fast local *echoes*, never the source of truth.**

That spec is a statement of **capabilities, not templates** — it says what a
pipeline must do, never how to lint Python or which host to deploy to. Every
command here reads the repo and derives the answer for *that* repo. The files
under `templates/examples/` are one concrete instantiation, never a thing to
copy.

This plugin ships the gate commands **and** their enforcement — gate tracking, the PR-blocking hook, and skill auto-recording are pure-python scripts in `scripts/`, wired by `hooks/hooks.json` (auto-loaded). Pair with [sdlc-guardrails](../sdlc-guardrails/) only if you also want opt-in main-branch commit protection.

## Installation

```sh
claude /plugin install sdlc@sharpen
```

For enforcement too:

```sh
claude /plugin install sdlc-guardrails@sharpen
```

## Commands

| Command | Purpose |
|---------|---------|
| `/sdlc:new` | Bootstrap a brand-new repo: stack detection, README, `.gitignore`, `.env.op`, then delegate CI to `init` |
| `/sdlc:spec` | Write a spec (problem, goals, BDD scenarios, metrics) before implementing |
| `/sdlc:plan` | Classify scope, identify files, propose approach, open worktree |
| `/sdlc:gate` | Run the full gate chain (test → review → lint → typecheck) |
| `/sdlc:ship` | Push, open PR, optionally merge |
| `/sdlc:init` | Scaffold the CI pipeline + `ci-pass` required check + changed-files hooks + a reversible deploy workflow |
| `/sdlc:audit` | Read-only drift report: pipeline + enforcement posture vs the spec |
| `/sdlc:secrets` | Wire 1Password Environments-based secrets (read-only SA per tier) |
| `/sdlc:test-gaps` | Changed source files with no covering test |
| `/sdlc:test-critique` | Assertions that can't fail, over-mocking, unjustified skips |
| `/sdlc:test-flake` | Rerun the suite N times to surface intermittent failures |

## CI pipeline: `init` / `audit` (the enforcement surface)

`/sdlc:init` detects the stack(s), derives the idiomatic toolchain (researching
when unfamiliar), and instantiates the language-agnostic patterns in
[`templates/spec.md`](templates/spec.md): PR validation with concurrency +
least-privilege permissions + an always-runs path gate, a **`ci-pass` aggregator
job**, **branch protection requiring only `ci-pass`** (default-on; `--no-protect`
to skip), secret scanning, dependabot, and **pre-commit-only** changed-files
hooks (pre-push only when a genuinely slow check exists). It never wires
opinionated auto-fix that fights an intentional style.

`/sdlc:audit` is read-only. Beyond the structural CI checks it grades the
**enforcement posture** — is `ci-pass` actually a required check, or has
enforcement silently fallen back to git hooks? — plus the auto-fix footgun, gate
linter stack coverage, and whether `sdlc-guardrails` is active. This is the tool
that catches "CI isn't a required check" before it bites.

The `ci-pass` aggregator resolves the path-gated-required-check deadlock: marking
individual path-gated jobs required hangs a docs-only PR forever (the skipped job
never reports), so a single `if: always()` aggregator that fails on any real
failure becomes the one required context.

## Gate chain

Default order for Small-medium+ changes:

1. **Test** — auto-detects runner (bun/npm/pytest/cargo/go)
2. **Simplify** — review for dead code, over-abstraction, complexity
3. **Review** — correctness bugs, edge cases, security (uses grumpy if installed)
4. **Fix** — resolve review findings
5. **Imagine** — mental production walkthrough (uses grumpy if installed)
6. **Fix** — resolve imagination findings
7. **Lint** — auto-detects linter (biome/eslint/ruff/clippy)
8. **Typecheck** — auto-detects checker (tsc/pyright/mypy)

Tiny changes (≤3 lines of code): test + lint + typecheck.

Docs-only changes (no executable files, any size): test + lint + typecheck, all vacuously satisfied (nothing executable to verify). Skip the simplify/grumpy chain. Size does not promote a docs-only change to a code tier.

## Gate tracking (JSON, pure python — no bun)

`/sdlc:gate` records gate state to one JSON file **shared across every worktree of the repo**, keyed by branch: `<main-checkout>/.sharpen/data/gates.json` (override with `$SDLC_GATES_PATH`). The path is resolved via `git rev-parse --git-common-dir`, which points at the main checkout's `.git` from any linked worktree, so every worktree and any cwd inside the repo reads and writes the same file. Existing installs that only have `<main-checkout>/.claude/data/gates.json` keep using that file until `.sharpen/data/` exists, so an upgrade does not hide active cycles. A cycle recorded in one worktree is therefore visible when the PR is created from another, while two branches checked out in two worktrees stay isolated by their branch key — no `$SDLC_*` pinning footgun.

### Portable adapters

Non-Claude hosts can use the same gate evidence with a v1 capability manifest. The manifest declares a non-empty subset of `plan`, `review`, `imagine`, `fix`, `test`, `lint`, `typecheck`, and `ship`; the adapter resolves the highest supported profile (`baseline`, `review`, or `adversarial`). Supply `x-host-command-map` for any capability whose command is host-specific:

```json
{
  "protocol_version": "1",
  "provider": {"name": "my-agent", "agent": "my-agent", "model": "model-id"},
  "capabilities": ["test", "lint", "typecheck"],
  "x-host-command-map": {
    "test": "python3 -m unittest",
    "lint": "ruff check .",
    "typecheck": "mypy ."
  }
}
```

On a named feature branch with an initialized gate cycle, run `python3 plugins/sdlc/scripts/generic_adapter.py capabilities.json`. It executes the selected profile's commands and attaches a validated, synthetic review report to the gate store. `local_llm_adapter.py` uses the same manifest and gates, then delegates a diff review to an OpenAI-compatible local endpoint; set `LOCAL_LLM_URL` and optionally `LOCAL_LLM_MODEL` and `LOCAL_LLM_API_KEY`. For this adapter, declared `review` is fulfilled by the delegated LLM rather than an `x-host-command-map.review` command. A failed gate, invalid or failed delegated review, or inability to attach the report exits non-zero; attaching a report never marks a gate complete.

The `scripts/` are stdlib python (`gate_store.py` + `shell_parse.py` + five hook/CLI scripts), so tracking **and** enforcement run on any box with `python3`:

- `gate_store.py` — the store plus the rules. `record_gate()` refuses a skill-gated gate unless the caller passes `authorized=True`, so the rule holds for every caller that goes through it: the CLI, the hooks, an inline `python3 -c` that imports the module. A direct write to `gates.json` still bypasses it — the guard is against an agent talking itself past its own process gate, not against an adversary.
- `record-gate.py` (CLI) — `--init`/`--record`/`--status`/`--oneline`; the locked read-modify-write that `/sdlc:gate` drives.
- `enforce-sdlc-gates.py` (PreToolUse) — blocks `gh pr create` until all required gates for the branch's tier pass. Opt-in: no cycle for the branch → allowed; an unreadable store fails closed.
- `block-direct-gate-record.py` (PreToolUse) — blocks manual `--record` of skill-gated gates (`simplify`, `grumpy-*`) before the command runs, with a message pointing at the skill, and blocks hand-driving `auto-record-skill-gate.py`. It reads argv via `shell_parse.py`, so wrapped forms are caught; anything it can't resolve is left to the store, which refuses it there.
- `auto-record-skill-gate.py` (PostToolUse Skill) — records the matching gate after `/simplify`, `/grumpy:review`, `/grumpy:imagine`, `/grumpy:fix`. The only caller that passes `authorized=True`. It skips when the Skill call itself reported an error. Note what it can and cannot attest: PostToolUse fires when the *tool* returns, and a Skill call returns the skill's instructions — so this proves the skill was invoked, not that the agent then followed it. The guard stops an agent talking itself past its own process gate; it is not adversarial. When the current branch has no cycle it will adopt a cycle from another checked-out branch only if there is exactly one candidate — a gate stamped on the wrong branch cannot be corrected.
- `auto-init-gate-cycle.py` (PostToolUse Bash) — starts a gate cycle on the first `git commit` to a non-default branch, which is what makes enforcement opt-out rather than opt-in. Defaults to `small-medium`, auto-downgraded to `tiny` when every file changed since `main`/`master` confidently matches a docs/text/asset allowlist (conservative by construction — an unrecognized extension or an undeterminable diff keeps `small-medium`). It announces the arming (exit 2 — visible, non-blocking; the commit already ran), because for anyone who never runs `/sdlc:gate` that is the only notice of which tier landed and how to widen or narrow it. A commit that leaves the branch with *no* cycle says so the same way; genuinely routine outcomes stay quiet.

`hooks/hooks.json` is auto-loaded when the plugin is installed. Run the suite with `python3 tests/test_gate.py` from this directory, or `python3 scripts/run-tests.py` from the marketplace repo root to run every plugin's tests.

**Upgrading to 4.5.0 (docs-only commits auto-arm `tiny` instead of `small-medium`):** `auto-init-gate-cycle.py` now diffs the branch against `origin/main`/`origin/master` (falling back to local `main`/`master`) before arming a *new* cycle, and picks `tiny` when every changed file matches a docs/text/asset allowlist. This only affects the tier a *new* cycle starts at — an in-flight cycle is untouched, and the tier is decided once, at arm time; a docs-only first commit followed by a real code commit does **not** auto-upgrade the tier back to `small-medium` (same as any other post-gate change, a manual `/sdlc:gate --init small-medium` is required — see Gate chain above). If you were relying on every branch starting `small-medium` regardless of content, that assumption no longer holds for docs-only branches.

**Upgrading to 4.2.1 (skill-gated gates are enforced in the store):** until 4.2.0 the block on manually recording `simplify`/`grumpy-*` lived only in the PreToolUse hook, so any form it couldn't parse — an `eval`, an inline `python3 -c`, a generated script — recorded the gate. Those gates are now refused by `record_gate()` itself. Four things to know:

- An in-flight cycle needs no migration; the store format is unchanged and the remaining gates record normally when their skills run.
- A one-line fix after the chain completes really does mean re-running `/simplify` and the `grumpy` skills, since there is no longer a way to hand-stamp the re-run.
- Piping a payload to `auto-record-skill-gate.py` from Bash is now blocked; call it from python if you are testing the hook.
- **If you don't have `/simplify` and grumpy installed, this is the release where that stops being survivable.** `auto-init-gate-cycle.py` starts a `small-medium` cycle on the first commit to any non-default branch, and gates 2-6 can now only be recorded by their skills — the manual `--record` that used to slip through is gone. Install the skills, or start the branch with `/sdlc:gate --init tiny` where the change genuinely qualifies. Editing `gates.json` by hand is the last resort and the store does not stop you; it is a deliberate hole, not an oversight.

**Upgrading from 2.2.0 (per-worktree store):** main-checkout users are unaffected — the path is identical across versions. But if you had an in-flight gate cycle on a **linked worktree**, its state lived in that worktree's `.claude/data/gates.json` and won't be seen at the new shared location. Re-run `/sdlc:gate` on that branch to re-establish the cycle (gates are cheap to re-run and reset on any code change anyway).

## Composability

- **grumpy** installed? Gates 3-6 use `/grumpy:review`, `/grumpy:fix`, `/grumpy:imagine`. Without grumpy, the agent performs self-review and announces the fallback at the start of the gate — but note that self-review **cannot record** gates 3-6: only the auto-record hook can, when the skill actually runs. With the hooks registered and grumpy absent, a `small-medium`/`significant` cycle cannot complete, so install grumpy or use the `tiny` cycle where it genuinely applies.
- **sdlc hooks registered** (from `hooks/hooks.json`)? `gh pr create` is blocked until all required gates pass for the current branch. Without hooks, the gate chain is advisory.
- **sdlc-guardrails** installed? Adds opt-in main-branch commit protection (independent of gate tracking, which this plugin owns).
- Works with any language/framework — auto-detects toolchain from lock files and config.

## Worktree targeting

`/sdlc:gate` accepts `--worktree <path>` (alias `--path <path>`): branch detection and gate recording run against that worktree instead of the current directory. Use it when the invoking session's cwd isn't the worktree being gated (orchestrators, parallel worktree agents). Flag absent = current directory, unchanged.

`--branch` carries the bash-verifiable gates (`tests`, `lint`, `typecheck`) to the target. The skill-gated ones (`simplify`, `grumpy-*`) are stamped by a PostToolUse hook that only sees the cwd the skill ran in — the *driving* session, not the target. So `--init` also writes a route:

```bash
record-gate.py --init small-medium --branch "$TARGET_BRANCH" --route-from "$PWD"
```

The hook resolves its own worktree root and, when a cycle is routed from it, records there — outright, without consulting the local branch. That makes cross-worktree gating deterministic with any number of live worktrees, where the previous "adopt the one other checked-out cycle" heuristic gave up at two. A worktree drives at most one branch, but several worktrees may drive the same one, so two agents gating the same target both land their gates there instead of evicting each other. The route survives `--init` resets (it is not gate state), shows up in `--status` as `Driven from: <path>`, and is dropped by `record-gate.py --unroute` or by the driving session initializing a cycle for its own branch. **Without `--route-from`, gates 2–6 land on the driving session's branch instead** — and skill-gated gates have no manual `--record` to undo it with.

## Core principles

- TDD strict: failing test → minimal pass → refactor
- Always work in worktrees, never edit main
- Re-run full gate chain after any post-gate code change
- Squash merge, conventional commits

## Upgrading

Public releases of this plugin start here — the version history predates the `sharpen`
marketplace, so there is nothing to migrate from. Install and run `/sdlc:init`.

Breaking changes in future releases are documented in [`UPDATE.md`](UPDATE.md), which
`/sdlc:init` reads as step 0.

One standing constraint: **`sdlc` and `grumpy` share a path contract** —
`.claude/sdlc/<branch>/` and `.claude/grumpy/<branch>/`. grumpy's review and imagine read a
plan `/sdlc:plan` writes; sdlc's gate chain reads what grumpy writes. Upgrade them
together. Nothing enforces it, because a marketplace has no dependency field.
