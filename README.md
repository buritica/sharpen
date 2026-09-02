# sharpen

An opinionated SDLC with one authoritative gate, and an adversarial reviewer that means it.

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

That's the real text a `PreToolUse` hook writes — pure Python, no LLM call, so there's no talking it out of blocking. And the gates it's tracking aren't rubber stamps: `/grumpy:review` and `/grumpy:imagine` are real subagent passes over your actual diff, and `/grumpy:fix` is what resolves what they find.

## How it works

1. `/sdlc:plan` opens a worktree off `origin/main`, classifies the change's size, and writes a plan.
2. `/sdlc:gate` runs the chain for that size — a docs-only tweak passes on 3 near-vacuous checks, a real change runs all 8:
   `test → simplify → review → fix → imagine → fix → lint → typecheck`.
3. A non-critical finding can be deferred instead of fixed — a `ponytail:` marker at the site, visible in the PR, never silent, never for anything critical.
4. `/sdlc:ship` pushes, opens the PR, and squash-merges once `ci-pass` — the one required check — is green.

Every step is a real command you can run by hand and inspect. None of it is a black box that just reports "done."

## The plugins

| Plugin | What it does |
|---|---|
| [sdlc](plugins/sdlc/) | `new → spec → plan → gate → ship → CI → deploy`. Scaffolds your CI and deploy pipelines by reading the repo. Makes `ci-pass` the one required check. |
| [grumpy](plugins/grumpy/) | Review from a principal engineer who's been paged at 3am too many times. Fans out specialized reviewers, then fixes what they find. |
| [sdlc-guardrails](plugins/sdlc-guardrails/) | Blocks direct commits to your default branch. Opt-in per repo. |

`sdlc` works alone, running self-review when nothing else is installed. Add `grumpy` and the gate chain runs real reviews instead. Add `sdlc-guardrails` and `main` gets protected. They find each other by command availability at runtime, so nothing breaks when one is missing — and nothing here requires installing all three.

## grumpy argues with your diff

It doesn't render findings as a bulleted report and call that a review:

> "You built a factory factory for something that happens once."
> "I'm going to pretend I didn't see this catch block that swallows exceptions."
> "This abstraction is solving a problem you don't have."

`--level grumpy|grumpier|linus` turns that up or down. It's not just flavor — a review that reads like a linter's word salad gets skimmed, not acted on. See [`plugins/grumpy/README.md`](plugins/grumpy/README.md) for what a finding actually looks like and how `/grumpy:fix` resolves it.

## One gate, no quiet skip

Most repos enforce quality in three places: git hooks, CI, and whatever the developer remembers. None is designated the source of truth, so enforcement settles wherever it lands by accident — usually a git hook, while everyone believes it's CI.

This picks one: a single required check, `ci-pass`, at the boundary to shared state. Pre-commit hooks and the local gate chain are fast local echoes; they can be skipped, and merge safety never depends on them.

That gate can't be faked from the inside, either. The chain records to a JSON store keyed by branch, and a review gate records only when its skill actually runs — `record-gate.py --record grumpy-review` is refused, by the hook and by the store behind it. While gates are missing, `gh pr create` stays blocked. The limit is opt-out per branch, and it's stated plainly in the code: this stops an agent talking itself past its own process. It's not an adversarial control.

## Capabilities, not templates

[`templates/spec.md`](plugins/sdlc/templates/spec.md) says what a pipeline must do, never how to lint Python or where to deploy. Each command reads your repo and derives the answer, researching the toolchain when it doesn't recognize your stack. The files under `templates/examples/` are one worked instance, not something to copy — template-shaped tooling has to name something concrete, so it only ever fits repos shaped like the author's.

## Cross-host, not just Claude Code

`sdlc` and `grumpy`'s commands are written host-neutral on purpose — "spawn a subagent if your harness supports it," never a hardcoded Claude tool call. Every command also ships a generated `SKILL.md`, the shared format Codex CLI, Gemini CLI, Cursor, and Copilot all read.

Codex CLI support is **live-verified**, not inferred from docs: a real `gh pr create` got blocked and a real gate cycle got armed on an actual Codex session, hooks firing the same way they do under Claude Code. One gap is confirmed, not hypothetical — see [`plugins/sdlc/README.md`, "Codex CLI support"](plugins/sdlc/README.md#codex-cli-support). The full cross-agent contract, current state and future shape both, is in [`docs/portable-core.md`](docs/portable-core.md).

## Pairs with ponytail

[ponytail](https://github.com/DietrichGebert/ponytail) shapes code *before* the gate chain ever sees it — an external peer plugin, nothing from it vendored here. `grumpy` honors its `ponytail:` comment convention as documented debt, and `/grumpy:fix` can defer non-critical findings into a marker instead of fixing everything at review time. Neither plugin requires the other; skip ponytail entirely and deferral still works exactly the same way.

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

Plugin changes need the version bumped in both `plugins/<name>/.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`, in the same PR — a merged change without the bump is live in the repo and undelivered to users.

```sh
python3 scripts/run-tests.py
python3 scripts/check-marketplace.py
```

## License

MIT. See [LICENSE](LICENSE).
