# sharpen

<p align="center"><em>An opinionated SDLC with one authoritative gate, and an adversarial reviewer that means it.</em></p>

Three Claude Code plugins that make review and enforcement real instead of aspirational: [sdlc](plugins/sdlc/) runs `plan → gate → ship` and makes one CI check the only thing that can block a merge, [grumpy](plugins/grumpy/) is the reviewer that actually reads the diff, and [sdlc-guardrails](plugins/sdlc-guardrails/) keeps commits off your default branch. Point them at any repo — they read the codebase and derive the toolchain, they don't ship one.

```sh
claude plugin marketplace add buritica/sharpen
claude plugin install sdlc@sharpen
```

That's the whole install. `/sdlc:init` scaffolds CI for whatever language it finds; nothing to configure first.

## What actually changes

Without `sdlc`, "review before merge" is a norm — real when someone remembers, gone the day they don't. With it, review is a recorded gate, and merging is blocked until it's recorded:

```
$ gh pr create --title "feat: add refund flow" ...

[gate] enforce: SDLC gates incomplete for branch "feature/refund-flow" (tier "small-medium").

Completed (3/8): tests, lint, typecheck
Missing (5): simplify, grumpy-review, grumpy-fix-post-review, grumpy-imagine, grumpy-fix-post-imagine

Run /sdlc:gate to finish the chain, then retry.
```

That message is the real text a `PreToolUse` hook writes — pure Python, no LLM call, so it can't be talked out of blocking. The gate chain itself isn't decoration either: `/grumpy:review` and `/grumpy:imagine` are real subagent passes over your actual diff, and `/grumpy:fix` is what resolves what they find, not a rubber stamp that records a passing gate on its own say-so.

## How it works

1. `/sdlc:plan` opens a worktree off `origin/main`, classifies the change's size, and writes a plan.
2. `/sdlc:gate` runs the chain for that size — a docs-only tweak passes on 3 near-vacuous checks, a real change runs all 8:
   `test → simplify → review → fix → imagine → fix → lint → typecheck`.
3. Findings that don't need fixing today don't have to vanish either — a non-critical one can be deferred with a `ponytail:` marker instead, visible in the PR, never silent, never for anything critical.
4. `/sdlc:ship` pushes, opens the PR, and squash-merges once `ci-pass` — the one required check — is green.

Every step above is a real command you can run by hand and inspect; nothing here is a black box that just reports "done."

## The plugins

| Plugin | What it does |
|---|---|
| [sdlc](plugins/sdlc/) | `new → spec → plan → gate → ship → CI → deploy`. Scaffolds your CI and deploy pipelines by reading the repo. Makes `ci-pass` the one required check. |
| [grumpy](plugins/grumpy/) | Review from a principal engineer who's been paged at 3am too many times. Fans out specialized reviewers, then fixes what they find. |
| [sdlc-guardrails](plugins/sdlc-guardrails/) | Blocks direct commits to your default branch. Opt-in per repo. |

`sdlc` works alone, running self-review when nothing else is installed. Add `grumpy` and the gate chain runs real reviews instead. Add `sdlc-guardrails` and `main` gets protected. They find each other by command availability at runtime, so nothing breaks when one is missing — and nothing here requires you to install all three.

## The reviewer has a voice, on purpose

`grumpy` doesn't render findings as a bulleted report and call it a review — it argues with the diff:

> "You built a factory factory for something that happens once."
> "I'm going to pretend I didn't see this catch block that swallows exceptions."
> "This abstraction is solving a problem you don't have."

`--level grumpy|grumpier|linus` turns that up or down. It's not just flavor — the persona is what stops a review from reading like a linter's word salad and reading like something a human would actually act on. `/grumpy:fix` then dispatches the real fixes, tier-routed by cost (`haiku` for trivial, `opus` for anything unverifiable), and escalates once if a fix's own acceptance check fails.

## Cross-host, not just Claude Code

`sdlc` and `grumpy`'s commands are written host-neutral on purpose — no hardcoded "spawn a Task tool," just "spawn a subagent if your harness supports it." Every command also ships a generated `SKILL.md`, the shared format Codex CLI, Gemini CLI, Cursor, and Copilot all read. Codex CLI support is **live-verified**, not inferred from docs: `PreToolUse`/`PostToolUse` hooks fire the same way they do under Claude Code, confirmed by watching a real `gh pr create` get blocked and a real gate cycle get armed on an actual Codex session. See [`plugins/sdlc/README.md`, "Codex CLI support"](plugins/sdlc/README.md#codex-cli-support) for the one confirmed gap (Codex's own `Skill` tool call doesn't record gates 2-6 the same way) and the full picture in [`docs/portable-core.md`](docs/portable-core.md).

## Why one gate

Most repos enforce quality in three places: git hooks, CI, and whatever the developer remembers. None is designated the source of truth, so enforcement settles wherever it lands by accident. Usually a git hook. Everyone believes it's CI.

This picks one. A single required check, `ci-pass`, at the boundary to shared state. Pre-commit hooks and the local gate chain are echoes for fast feedback; they can be skipped, and merge safety never depends on them.

That split also kills the "hook or CI?" argument. CI enforces. Everything else echoes.

## Capabilities, not templates

[`templates/spec.md`](plugins/sdlc/templates/spec.md) says what a pipeline must do. It never says how to lint Python or where to deploy. Each command reads your repo and derives the answer, researching the toolchain when it doesn't recognize your stack. The files under `templates/examples/` are one worked instance, not something to copy.

This is the part that matters. Template-shaped tooling has to name something concrete, so it only ever fits repos shaped like the author's.

## Gates you can't skip quietly

The chain records to a JSON store keyed by branch. Review gates record only when their skill actually runs: `record-gate.py --record grumpy-review` is refused by the hook, and again by the store behind it. While gates are missing, `gh pr create` is blocked. It's opt-out per branch, so no cycle means no enforcement.

The limit, stated in the code where it matters: this stops an agent talking itself past its own process. It is not an adversarial control.

## Pairs with ponytail

[ponytail](https://github.com/DietrichGebert/ponytail) shapes code *before* the gate chain ever sees it — an external peer plugin, nothing from it vendored here. `grumpy` honors its `ponytail:` comment convention as documented debt, and `/grumpy:fix` can defer non-critical findings into a marker instead of fixing everything at review time. Neither plugin requires the other; skip ponytail entirely and deferral still works exactly the same way.

## Portable core protocol

The current packages are Claude Code plugins, but their future cross-agent contract is documented in [Portable core protocol](docs/portable-core.md): neutral capability manifests, structured review evidence, and adapter boundaries for model, forge, CI, and host lifecycle integrations.

## Requirements

`python3` for gate tracking and enforcement, stdlib only, nothing to install. `gh` for the ship commands. Everything else comes from the repo you point it at.

## Uninstall

```sh
claude plugin remove sdlc
claude plugin remove grumpy
claude plugin remove sdlc-guardrails
```

Removing `sdlc` doesn't touch a repo's own `AGENTS.md` contract or its `.sharpen/data/gates.json` history — both are plain files that outlive the plugin that wrote them.

## Contributing

This repo runs its own gates. `/sdlc:gate` before every PR, and the hook blocks `gh pr create` until the chain finishes.

Plugin changes need the version bumped in both `plugins/<name>/.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`, in the same PR. A merged change without the bump is live in the repo and undelivered to users.

```sh
python3 scripts/run-tests.py
python3 scripts/check-marketplace.py
```

## License

MIT. See [LICENSE](LICENSE).
