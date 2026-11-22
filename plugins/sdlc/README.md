# sdlc

The full single-repo software lifecycle: new → spec → plan → build → **gate** →
ship → **CI** → **deploy**. The TDD workflow, the local quality gate chain, and
the pipeline scaffolding/audit for the repo's own CI.

Two layers, two jobs, and they never swap:

- **The gate chain is local, and it is sharpen's product.** It runs on the
  developer's machine before a PR exists, records to a gitignored store, and
  blocks opening a PR, by any path `pr_paths.py` knows, until review is done,
  with a git pre-push hook as the floor. It is opt-out per branch and not
  an adversarial control: it stops an agent talking itself past its process,
  not a person editing the store. Nothing about it is sent to CI.
- **CI checks the code.** `init` / `audit` / `secrets` (formerly the
  standalone `ci` plugin) scaffold and grade a pipeline against one canonical
  spec at [`templates/spec.md`](templates/spec.md), with exactly one required
  check, `ci-pass`, for tests, lint, typecheck and build. It never reads gate
  state.

That spec is a statement of **capabilities, not templates** — it says what a
pipeline must do, never how to lint Python or which host to deploy to. Every
command here reads the repo and derives the answer for *that* repo. The files
under `templates/examples/` are one concrete instantiation, never a thing to
copy.

This plugin ships the gate commands **and** their enforcement — gate tracking, the PR-blocking hook, and receipt-based skill-gate recording are pure-python scripts in `scripts/`, wired by `hooks/hooks.json` (auto-loaded). Pair with [sdlc-guardrails](../sdlc-guardrails/) only if you also want opt-in main-branch commit protection.

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

1. **Test** — the `test` command AGENTS.md declares, else auto-detects the runner (bun/npm/pytest/cargo/go)
2. **Simplify** — review for dead code, over-abstraction, complexity — thresholds are judged against the merge base, so pre-existing debt is reported rather than failed
3. **Review** — correctness bugs, edge cases, security (uses grumpy if installed)
4. **Fix** — resolve review findings (a non-critical finding may instead be deferred with a `ponytail:` comment, per `/grumpy:fix`'s eligibility table; critical findings are never deferred)
5. **Imagine** — mental production walkthrough (uses grumpy if installed)
6. **Fix** — resolve imagination findings (same deferral option as step 4)
7. **Lint** — the declared `lint` command, else auto-detects the linter (biome/eslint/ruff/clippy)
8. **Typecheck** — the declared `typecheck` command, else auto-detects the checker (tsc/pyright/mypy)

The declared commands are the ones `/sdlc:init` writes into the AGENTS.md managed block (`scripts/declared_commands.py` reads them back). When they and the detection table disagree, AGENTS.md wins, and the gate says which source each command came from.

Tiny changes (≤3 lines of code): test + lint + typecheck.

Docs-only changes (no executable files, any size): test + lint + typecheck, all vacuously satisfied (nothing executable to verify). Skip the simplify/grumpy chain. Size does not promote a docs-only change to a code tier.

## Gate tracking (JSON, pure python — no bun)

`/sdlc:gate` records gate state to one JSON file **shared across every worktree of the repo**, keyed by branch: `<main-checkout>/.sharpen/data/gates.json` (override with `$SDLC_GATES_PATH`). The path is resolved via `git rev-parse --git-common-dir`, which points at the main checkout's `.git` from any linked worktree, so every worktree and any cwd inside the repo reads and writes the same file. Existing installs that only have `<main-checkout>/.claude/data/gates.json` keep using that file until `.sharpen/data/` exists, so an upgrade does not hide active cycles. A cycle recorded in one worktree is therefore visible when the PR is created from another, while two branches checked out in two worktrees stay isolated by their branch key — no `$SDLC_*` pinning footgun.

### Portable adapters

Every native host (Claude Code, Codex, pi) runs the session-start adapter, `scripts/session-start.py`, which writes `.sharpen/data/capabilities.<host>.json`. It names the host honestly (`SHARPEN_HOST`, else Claude Code's `CLAUDECODE=1`, else `unknown`), lists the grumpy passes it found, and records whether this host sweeps receipts on its own (`x-recording`) and whether the installed grumpy writes receipts at all (`x-receipts`). Auto-init reads it to tell you how gates 2 to 6 record here; with a grumpy that predates receipts it arms an event-only cycle on Claude Code only, and everywhere else says to upgrade grumpy. A profile is recorded on the cycle for diagnostics only; the tier alone decides which gates are required. [`docs/hosts.md`](../../docs/hosts.md) is the per-host support matrix.

Non-Claude hosts can use the same gate evidence with a v1 capability manifest. The manifest declares a non-empty subset of `plan`, `simplify`, `review`, `imagine`, `fix`, `test`, `lint`, `typecheck`, and `ship`; the adapter resolves the highest supported profile (`baseline`, `review`, or `adversarial`). Supply `x-host-command-map` for any capability whose command is host-specific:

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

The `scripts/` are stdlib python (`gate_store.py`, `gate_receipts.py`, `gate_convergence.py`, `shell_parse.py`, `pr_paths.py` and the hook/CLI scripts), so tracking **and** enforcement run on any box with `python3` 3.9 or newer:

- `gate_store.py` — the store plus the rules. `record_gate()` refuses a skill-gated gate unless the caller passes `authorized=True`, so the rule holds for every caller that goes through it: the CLI, the hooks, an inline `python3 -c` that imports the module. A direct write to `gates.json` still bypasses it — the guard is against an agent talking itself past its own process gate, not against an adversary.
- `gate_receipts.py` — receipt-backed evidence: where a grumpy pass's receipt lives, whether it earns a gate (right branch, a commit on it, newer than the cycle and than the receipt of the pass it follows), and which review gates went stale after a later commit.
- `gate_base.py` — resolves the base a review round covers, for stacked branches: `--set-base` (git config `branch.<b>.sdlcBase`), then the open PR's base via `gh`, then the branch's local upstream parent, then the default branch. `--init`, auto-init, `--carry` and `--set-base` resolve it, `--status` prints the stored one, and it writes `.claude/sdlc/<branch>/base.json` for the grumpy passes.
- `gate_convergence.py` — keeps a finished chain from re-running for commits that can't change what the review saw (sharpen#37). `--carry` classifies the commits since the last review gate as a clean rebase (a conflict-free `git merge-tree` replay, or the branch's own changed lines unchanged, each side against the base it was reviewed or restacked on), release bookkeeping (an explicit list of version fields, render stamps and release notes), trailing-whitespace or blank-line only, safe docs (an explicit safe list that never includes a file an agent executes), or a small comment fix with a stated reason (30 changed lines across all manual carries in a cycle, no new files). A carry is recorded and shown in `--status` as a carry, and re-opens tests, lint and typecheck. It also keeps the advisory round counter: each `--init` that replaces a cycle with earned skill gates is a new round, and from round 2 `--init` and `--status` report it with the branch's growth.
- `record-gate.py` (CLI) — `--init` (with `--new-rounds`)/`--record`/`--verify`/`--carry`/`--set-base`/`--status`/`--oneline`; the locked read-modify-write that `/sdlc:gate` drives. `--verify <pass>` records a skill gate from the receipt its grumpy pass left, on any host.
- `pr_paths.py` — the one answer to "does this tool call open a PR, for which branch, in which checkout?", shared by every host: `gh pr create` and its `gh pr new` alias, `gh api` POSTs to `repos/<o>/<r>/pulls` and the graphql `createPullRequest` mutation, `glab mr create`, `hub pull-request`, `git push -o merge_request.create`, and MCP `create_pull_request`/`create_merge_request` tools. A head the shell would expand (`--head "$(git branch --show-current)"`, `$B`, `{}`) or `HEAD`/`@` resolves to the checked-out branch instead of reading as an unknown branch. When the cwd isn't the PR's checkout (an MCP call, `-R owner/repo`, a cloud session sitting above several repos), it finds the checkout whose remote matches, comparing by path suffix so a host, URL or nested GitLab group spelling still matches. A cwd checkout whose own store has a cycle for the PR's branch is judged first, so a sibling clone of an upstream can't capture `-R upstream/repo` from a gated fork. MCP heads are read from `head`, `source_branch`, `sourceBranch`, `sourceRefName`, `headRefName` or `head_branch` (`refs/heads/` stripped); with none, the cwd's branch is judged, with a note. When none matches and the cwd is a checkout, it judges that checkout, with a note; only from a non-checkout cwd does a named repo with no checkout in reach allow, with a note. It does not try to read opaque scripts, user gh aliases, `curl` or `$GH`-style spellings; the pre-push floor covers those.
- `enforce-sdlc-gates.py` (PreToolUse on `Bash`, plus `Monitor` and MCP PR tools) — blocks every PR path `pr_paths.py` recognizes until all required gates for the branch's tier pass. On a receipt-backed cycle it also blocks when a commit has landed since the last review gate, naming the stale gates and pointing at `--carry`. Opt-in: no cycle for the branch → allowed (an MCP PR says so, since a branch pushed only through MCP never arms a cycle); an unreadable store fails closed.
- `block-direct-gate-record.py` (PreToolUse on `Bash` and `Monitor`) — blocks manual `--record` of skill-gated gates (`simplify`, `grumpy-*`) before the command runs, with a message pointing at the skill, and blocks hand-driving `auto-record-skill-gate.py`. It reads argv via `shell_parse.py`, so wrapped forms are caught; anything it can't resolve is left to the store, which refuses it there.
- `auto-record-skill-gate.py` (PostToolUse Skill) — records the matching gate after `/grumpy:simplify`, `/grumpy:review`, `/grumpy:imagine`, `/grumpy:fix`. On a receipt-backed cycle it records only once the pass's receipt exists; `--verify` and the receipt sweep reach the same `authorized=True` recorder. It skips when the Skill call itself reported an error, and redirects (rather than silently no-oping) a stale invocation of an old, renamed skill name (`gate_store.RENAMED_SKILLS`). Note what it can and cannot attest: PostToolUse fires when the *tool* returns, and a Skill call returns the skill's instructions — so this proves the skill was invoked, not that the agent then followed it. The guard stops an agent talking itself past its own process gate; it is not adversarial. When the current branch has no cycle it will adopt a cycle from another checked-out branch only if there is exactly one candidate — a gate stamped on the wrong branch cannot be corrected. Recording `simplify` or either fix gate also clears any already-recorded `tests`/`lint`/`typecheck` (`gate_store.CODE_MUTATING_GATES`) — those three pass by editing code, so a bash-gate stamp from before the edit no longer verifies anything, and the hook surfaces the clear loudly rather than leaving it to `--status` to notice later. **Receipt-backed cycles** (every cycle created since 4.14.0): the Skill event fires when the instructions load, before the work, so it records only when the pass's receipt already exists; in practice `--verify` or the Stop sweep records the gate.
- `sweep-skill-receipts.py` (Stop; pi `agent_end`) — at the end of each turn, records every skill gate whose grumpy receipt is already on disk and names the branch's HEAD or an ancestor of it (a later fix may have committed). It says so when the store can't be read. Never blocks, never touches a legacy cycle.
- `auto-init-gate-cycle.py` (PostToolUse Bash) — starts a gate cycle on the first `git commit` to a non-default branch, which is what makes enforcement opt-out rather than opt-in. Defaults to `small-medium`, auto-downgraded to `tiny` when every file changed since `main`/`master` confidently matches a docs/text/asset allowlist (conservative by construction — an unrecognized extension or an undeterminable diff keeps `small-medium`). It announces the arming (exit 2 — visible, non-blocking; the commit already ran), because for anyone who never runs `/sdlc:gate` that is the only notice of which tier landed and how to widen or narrow it. A commit that leaves the branch with *no* cycle says so the same way; genuinely routine outcomes stay quiet.

- `pre-push.py` (git pre-push, installed by `install-pre-push.py`) — the host-agnostic floor: for each branch pushed, the same missing/stale check the PR hook runs, so a PR opened by a script, an alias, another tool or the web UI after a push still meets it. Warns by default; `git config sharpen.prePush block` makes it refuse the push (one-shot escape: `SDLC_ALLOW_PUSH=1 git push`), `off` silences it. Even in block mode it lets the push through, with a message, when the gate store is unreadable or the script itself fails: a push hook must never wedge a push, and the PR hook still fails closed on the same store. A `git push origin HEAD` resolves to the checked-out branch. It reaches an instructions-only host only where the sdlc scripts are installed on the machine and the shim is in the repo (for example by a Claude Code, Codex or pi session); `npx skills add` ships no scripts. `install-pre-push.py` writes a path-free shim to the repo's common hooks dir (all worktrees), atomically, and records the scripts dir in `git config sharpen.sdlcScripts`; every `/sdlc:gate` run and every auto-armed cycle run it, so the path refreshes after a plugin update. The shim fails open when `python3` isn't on the hook's PATH. It never overwrites another hook: when husky or `core.hooksPath` own the hooks it prints a chain line (safe under `sh -e`, and a no-op on machines without sdlc), and when lefthook does it prints a `lefthook.yml` command with `use_stdin: true`, since lefthook regenerates its hook files and passes stdin only when asked.

`hooks/hooks.json` is auto-loaded when the plugin is installed. Run the suite with `python3 tests/test_gate.py` from this directory, or `python3 scripts/run-tests.py` from the marketplace repo root to run every plugin's tests.

Version-specific migration notes older than the current release live in [`CHANGELOG.md`](CHANGELOG.md); the current release's own upgrade note is [`UPDATE.md`](UPDATE.md), read by `/sdlc:init` as step 0.

## AGENTS.md is the contract

`/sdlc:init` writes the SDLC contract — lifecycle, tiers, gate enforcement, the
derived test/lint/format/typecheck commands, PR conventions, artifact paths —
into `AGENTS.md` between `<!-- sdlc:begin -->` / `<!-- sdlc:end -->` markers,
and makes `CLAUDE.md` include it with a single `@AGENTS.md` line. Re-running
init replaces only the marked block; everything outside it is yours. Hosts that
read `AGENTS.md` directly (Codex, Gemini, Cursor, Copilot) get the same
contract Claude Code gets through the include. The block is inline in
`AGENTS.md` rather than a separate included file on purpose: those hosts do
not resolve `@` includes, so an include there would be inert text for exactly
the readers `AGENTS.md` exists to serve. The first line inside the block stamps
the sdlc version that rendered it; `scripts/agents_md.py --check` with no
other flags compares that stamp against the installed plugin (exit 1 when
stale — `/sdlc:gate` runs this once per cycle and mentions it), and with the
command flags it reports full drift without writing. Command flags repeat
once per area in a monorepo; `--no-claude` manages AGENTS.md only. The
template lives at `templates/agents-sdlc.md`. Repos that already carried the older
`## Run gates before every PR` reminder in `CLAUDE.md` (a heading, a fenced
`/sdlc:gate`, and two sentences) have it migrated on the next init run; a
symlinked `CLAUDE.md` or `AGENTS.md` is refused rather than written through.

## Codex CLI support

**Live-verified against an installed Codex CLI session** (not inferred from
docs): Codex reads this plugin's own `hooks/hooks.json` directly, with no
translation layer needed. `${CLAUDE_PLUGIN_ROOT}` resolves in the hook
command string exactly as it does under Claude Code; `SessionStart` runs
`session-start.py` (then named `claude-session-start.py`); a `PreToolUse` hook genuinely denies — confirmed
by watching `enforce-sdlc-gates.py` block a real `gh pr create` on an
incomplete gate cycle, full multi-line reason and all, on stdout AND stderr,
with no schema-validation fallout from the Claude-shaped JSON envelope; and
`PostToolUse` genuinely fires — confirmed by watching `auto-init-gate-cycle.py`
arm a cycle on a real `git commit`. An earlier revision of this plugin shipped
a separate `hooks/codex-hooks.json` manifest and host-detection logic in
`hook_out.py`, built defensively against public docs alone before this could
be tested live. Both were removed once live testing showed the theoretical
risk they hedged against (an unrecognized host choking on Claude's stdout
JSON shape) didn't hold up — `enforce-sdlc-gates.py` and
`auto-init-gate-cycle.py` work on Codex via the same `hooks/hooks.json` every
other host reads, no per-host manifest or env var required.

**MCP PR tools (4.16.0):** the second `PreToolUse` entry in `hooks/hooks.json`
(matcher `Monitor|mcp__.*create_(pull|merge)_request$`) is live-verified on Claude
Code 2.1.330 (verified before the `$` anchor was added, which only keeps
`create_pull_request_with_copilot`, a remote-agent PR with no local branch, out): a headless session's MCP `create_pull_request` call and a `Monitor`
call running `gh pr create` were both blocked with the full reason. On Codex it is
**not yet live-verified**. Codex's hooks docs say `PreToolUse` fires for MCP tools
under the same `mcp__<server>__<tool>` names and call tool hooks "a useful guardrail,
not a complete enforcement boundary". The git pre-push floor still applies there.

**The gap this used to be, and how receipts close it**: event-driven
recording (`auto-record-skill-gate.py`) depends on a distinct `Skill` tool
call firing `PostToolUse`. Live-tested on Codex, a grumpy review genuinely ran
and `grumpy-review` was never recorded; fx (sharpen#41) fires no `PostToolUse`
at all. Since sdlc 4.14.0, a new gate cycle is receipt-backed instead: each
grumpy pass ends by writing a receipt naming the branch's HEAD
(`plugins/grumpy/scripts/receipt.py`), and `record-gate.py --verify <pass|all>`
records the gate from it on any host. Claude Code's `Stop` hook and pi's
`agent_end` run the same sweep automatically; on Codex or fx, run `--verify`
yourself after each pass. See `gate.md`'s "How skill gates record: receipts
(every host)" section for the exact commands, the staleness rule, and the
`SDLC_GATE_EVIDENCE=events` opt-out.

**Still unverified on Codex** (see [`docs/hosts.md`](../../docs/hosts.md),
"Verifying Codex", for the exact checks): whether Codex fills in
`${CLAUDE_PLUGIN_ROOT}` inside skill text the way it does in hook commands,
whether its `Stop` hook fires, and which environment variable identifies it.
Until the last is known, a Codex session declares its host as `unknown` and
auto-init tells it to run `--verify` after each pass.

Command prose was the other porting axis: `commands/*.md` files originally
named Claude's own tools literally (`Task tool`, `TaskCreate`, `Skill tool`),
which a harness without those exact tools can't follow as written. Every
command in `sdlc`, `grumpy`, and `sdlc-guardrails` has been rewritten to
describe the capability conditionally instead (spawn a subagent if your
harness supports it, otherwise do the work yourself sequentially) — see
[`../grumpy/README.md`](../grumpy/README.md), "Portability beyond Claude
Code", for the one spot (`dispatch.md`'s legacy-cycle gate-recording note)
that stays explicitly Claude-Code-only rather than being rewritten to sound
portable when the underlying mechanism isn't.

**Every `commands/<name>.md` also has a generated `skills/<name>/SKILL.md`**
— the cross-host format Codex CLI, Gemini CLI, Cursor, and Copilot all read
(`name` + `description` frontmatter, same body). These are produced by
`python3 scripts/generate-skill.py <path-to-commands-file>` (or
`--write-all-in <plugin-dir>` for all of a plugin's commands at once) from
the repo root, using `scripts/frontmatter.py` — a parser scoped to this
repo's actual frontmatter shapes, not a general YAML parser. **Generated
files are committed, not built at install time** — a Claude Code plugin
marketplace has no build step other hosts would run. `scripts/check-marketplace.py`
fails if any `skills/*/SKILL.md` is missing or stale relative to its source
command, so regenerate (`--write-all-in <plugin-dir>`) after editing a
command's frontmatter or body and before opening a PR. Known limitation: the
generated body still references `$ARGUMENTS` Claude-command-style — there is
no cross-host argument-passing convention yet, so a SKILL.md consumer sees
the same placeholder a Claude Code user would.

This closes a real, previously-dormant gap: the session-start adapter's
capability detection (see "Portable adapters" above) has always checked for
`plugins/grumpy/skills/{review,imagine,fix}/SKILL.md` to declare those
capabilities in the v1 manifest, but until these files existed that check
never found anything.

**Update**: generating the files alone didn't fully close it. `detect_capabilities()`
built the sibling-plugin path by going one level up from its own
`${CLAUDE_PLUGIN_ROOT}` — correct only in a flat dev checkout where
`plugins/sdlc` and `plugins/grumpy` sit as direct siblings. This repo's own
`CLAUDE.md` already documents Claude Code's plugin cache as version-nested
(`cache/<marketplace>/<plugin>/<version>/`) in its "Hook authoring" note, and
that's now directly confirmed for Codex CLI too — a live installed Codex
plugin cache showed the join silently resolving to
`cache/sharpen/sdlc/grumpy/...` (one level too shallow) and never matching.
`review`/`imagine`/`fix` were still silently absent from every real manifest;
the gap this section describes stayed open even after this PR shipped,
invisible to the unit tests because they stub `path_exists` rather than
exercising real path construction. Now fixed to try both the flat-checkout
path and a version-glob for the nested one, with an `SDLC_<PLUGIN>_ROOT` env
override (e.g. `SDLC_GRUMPY_ROOT`) as an escape hatch if a third host's cache
layout doesn't match either shape — point it directly at the sibling
plugin's root without waiting on a new sdlc release.

This still builds a cross-plugin path from `${CLAUDE_PLUGIN_ROOT}`, which
CLAUDE.md's "Hook authoring" note otherwise tells contributors not to do —
deliberately, here: that note's alternative ("detect sibling capabilities by
command availability, the way `/sdlc:gate` detects grumpy") relies on an
agent consulting its own list of available skills, and this file is a plain
subprocess with no such list. Filesystem detection, widened rather than
abandoned, is what's actually available to it. Also not fixed: multiple
cached versions of the sibling plugin aren't disambiguated — the glob
matches any version directory with the skill file present, so a stale
leftover after an upgrade could false-positive a capability that's no
longer there.

If `review`/`imagine`/`fix` are missing from the manifest and it's not
obvious why, set `SDLC_DEBUG=1` before the SessionStart hook runs: it prints
one stderr line per skill-backed capability naming which candidate path (if
any) matched, so you don't have to re-derive the flat/nested/override paths
by hand the way finding this bug in the first place required. Off by
default — this hook is quiet on success by design.

## pi (coding agent) support

pi has no Claude-style `PreToolUse`/`PostToolUse`/`SessionStart` hook events, so
this plugin ships a small pi **extension** (`extensions/sdlc-hooks.ts`) that
replicates the hook layer and its interactive gate confirmations. It does not
reimplement any gate logic — it translates pi lifecycle events into the Claude
hook payloads the pure-stdlib `scripts/*.py` already read on stdin, spawns the
existing script, and maps its exit code / stderr back to a pi decision:

| Claude hook | pi event | adapter action |
|---|---|---|
| `SessionStart` | `session_start` | run `session-start.py` (the extension exports `SHARPEN_HOST=pi` first) |
| `PreToolUse` Bash | `tool_call` (bash) | run `enforce-sdlc-gates.py` + `block-direct-gate-record.py`; block on non-zero exit |
| `PostToolUse` Bash | `tool_result` (bash) | run `auto-init-gate-cycle.py`; surface its notice |
| `Stop` | `agent_end` | run `sweep-skill-receipts.py`; surface its notice |
| `PostToolUse` Skill | — | not needed: skill gates record from receipts |

**Gate confirmations are replicated, not dropped.** When a hook denies (e.g. an
ungated `gh pr create`), pi shows an interactive confirmation with the reason
and only an interactive human in the TUI can override. Headless (print/rpc/json)
mode has no human to ask, so it **fails closed** — the tool call is blocked with
`terminate: true`, and the model can never self-bypass.

`SDLC_PLUGIN_ROOT` and `SHARPEN_HOST=pi` are exported into `process.env` at
extension load; pi's bash tool spawns shells inheriting `process.env`, so the
shared SKILL.md bodies, which spell every path
`${SDLC_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}`, run verbatim. No extension sets
the shared `CLAUDE_PLUGIN_ROOT` (see [`docs/hosts.md`](../../docs/hosts.md)).

Install as a pi package (`pi install git:...sharpen`, or `--dir plugins/sdlc` for
this plugin alone). The pi manifest is `package.json`'s `pi` key; the top-level
`package.json` bundles all three plugins. `scripts/check-marketplace.py` keeps
the pi manifest's version in sync with `plugin.json` and verifies every hook
script the extension references still exists.

**No `Skill` tool, and gates 2–6 still record**: each grumpy pass writes a
receipt, the gate chain runs `record-gate.py --verify <pass>`, and the
`agent_end` sweep catches a skipped one. grumpy's own pi extension exports
`GRUMPY_PLUGIN_ROOT` so the passes find `receipt.py`. `--attest` stays the last resort. See [`docs/pi.md`](../../docs/pi.md) for
the full contract and the flat-name `audit` skill collision in the top-level
bundle.

## Composability

- **grumpy** installed? Gates 3-6 use `/grumpy:review`, `/grumpy:fix`, `/grumpy:imagine`. Without grumpy, the agent performs self-review and announces the fallback at the start of the gate — but note that self-review **cannot record** gates 3-6: they record only from the receipt a real grumpy pass leaves. With the hooks registered and grumpy absent, a `small-medium`/`significant` cycle cannot complete, so install grumpy or use the `tiny` cycle where it genuinely applies.
- **sdlc hooks registered** (from `hooks/hooks.json`)? Opening a PR (any path in `pr_paths.py`) is blocked until all required gates pass for the branch. Without hooks, the gate chain is advisory apart from the git pre-push floor.
- **sdlc-guardrails** installed? Adds opt-in main-branch commit protection (independent of gate tracking, which this plugin owns).
- Works with any language/framework — runs the commands AGENTS.md declares, and auto-detects the toolchain from lock files and config for anything it doesn't.

## Worktree targeting

`/sdlc:gate` accepts `--worktree <path>` (alias `--path <path>`): branch detection and gate recording run against that worktree instead of the current directory. Use it when the invoking session's cwd isn't the worktree being gated (orchestrators, parallel worktree agents). Flag absent = current directory, unchanged.

`--branch` carries the bash-verifiable gates (`tests`, `lint`, `typecheck`) to the target. The skill-gated ones (`simplify`, `grumpy-*`) are recorded by `--verify`, the Stop sweep or the PostToolUse hook, all of which start from the cwd they run in — the *driving* session, not the target. So `--init` also writes a route:

```bash
record-gate.py --init small-medium --branch "$TARGET_BRANCH" --route-from "$PWD"
```

The hook resolves its own worktree root and, when a cycle is routed from it, records there — outright, without consulting the local branch. That makes cross-worktree gating deterministic with any number of live worktrees, where the previous "adopt the one other checked-out cycle" heuristic gave up at two. A worktree drives at most one branch, but several worktrees may drive the same one, so two agents gating the same target both land their gates there instead of evicting each other. The route survives `--init` resets (it is not gate state), shows up in `--status` as `Driven from: <path>`, and is dropped by `record-gate.py --unroute` or by the driving session initializing a cycle for its own branch. **Without `--route-from`, gates 2–6 land on the driving session's branch instead** — and skill-gated gates have no manual `--record` to undo it with.

## Core principles

- TDD strict: failing test → minimal pass → refactor
- Always work in worktrees, never edit main
- A change after the chain resets it, unless `--carry` shows it can't change what the review saw (see `commands/gate.md`, "Carry")
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

What sharpen writes inside a repo stays out of git without any setup:
`.sharpen/data/`, `.claude/sdlc/`, `.claude/grumpy/` and `.claude/worktrees/` each get
a `.gitignore` holding `*` the moment they are created, on every host. `/sdlc:init` also
lists them in the repo's `.gitignore`. The one exception is a repo still on the legacy
`.claude/data/` store, a folder sharpen shares and never marks; init's block covers it.
`.sharpen/simplify.json` is config and stays tracked, so never ignore `.sharpen/` as a
whole. A team that commits its plans replaces `.claude/sdlc/.gitignore`'s contents with
`!*` (deleting it doesn't last: the next command writes it back).
