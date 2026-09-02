# sharpen

Three Claude Code plugins for shipping software: an SDLC where one required CI check is the gate, an adversarial code reviewer, and opt-in branch protection.

```sh
claude plugin marketplace add buritica/sharpen
claude plugin install sdlc@sharpen
```

| Plugin | What it does |
|---|---|
| [sdlc](plugins/sdlc/) | `new → spec → plan → gate → ship → CI → deploy`. Scaffolds your CI and deploy pipelines by reading the repo. Makes `ci-pass` the one required check. |
| [grumpy](plugins/grumpy/) | Review from a principal engineer who's been paged at 3am too many times. Fans out specialized reviewers, then fixes what they find. |
| [sdlc-guardrails](plugins/sdlc-guardrails/) | Blocks direct commits to your default branch. Opt-in per repo. |

`sdlc` works alone. Add `grumpy` and the gate chain runs real reviews instead of self-review. Add `sdlc-guardrails` and `main` gets protected. They find each other by command availability, so nothing breaks when one is missing.

Pairs with [ponytail](https://github.com/DietrichGebert/ponytail), which shapes code *before* the gate chain sees it, as an external peer plugin — nothing from it is vendored here. `grumpy` honors its `ponytail:` comment convention as documented debt, and `/grumpy:fix` can defer non-critical findings into a marker instead of fixing everything at review time.

## Why one gate

Most repos enforce quality in three places: git hooks, CI, and whatever the developer remembers. None is designated the source of truth, so enforcement settles wherever it lands by accident. Usually a git hook. Everyone believes it's CI.

This picks one. A single required check, `ci-pass`, at the boundary to shared state. Pre-commit hooks and the local gate chain are echoes for fast feedback; they can be skipped, and merge safety never depends on them.

That split also kills the "hook or CI?" argument. CI enforces. Everything else echoes.

## Capabilities, not templates

[`templates/spec.md`](plugins/sdlc/templates/spec.md) says what a pipeline must do. It never says how to lint Python or where to deploy. Each command reads your repo and derives the answer, researching the toolchain when it doesn't recognize your stack. The files under `templates/examples/` are one worked instance, not something to copy.

This is the part that matters. Template-shaped tooling has to name something concrete, so it only ever fits repos shaped like the author's.

## Portable core protocol

The current packages are Claude Code plugins, but their future cross-agent contract is documented in [Portable core protocol](docs/portable-core.md): neutral capability manifests, structured review evidence, and adapter boundaries for model, forge, CI, and host lifecycle integrations.

## Gates you can't skip quietly

The chain records to a JSON store keyed by branch. Review gates record only when their skill actually runs: `record-gate.py --record grumpy-review` is refused by the hook, and again by the store behind it. While gates are missing, `gh pr create` is blocked. It's opt-out per branch, so no cycle means no enforcement.

The limit, stated in the code where it matters: this stops an agent talking itself past its own process. It is not an adversarial control.

## Requirements

`python3` for gate tracking and enforcement, stdlib only, nothing to install. `gh` for the ship commands. Everything else comes from the repo you point it at.

## Contributing

This repo runs its own gates. `/sdlc:gate` before every PR, and the hook blocks `gh pr create` until the chain finishes.

Plugin changes need the version bumped in both `plugins/<name>/.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`, in the same PR. A merged change without the bump is live in the repo and undelivered to users.

```sh
python3 scripts/run-tests.py
python3 scripts/check-marketplace.py
```

## License

MIT. See [LICENSE](LICENSE).
