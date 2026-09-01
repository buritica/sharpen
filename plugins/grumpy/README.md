# Grumpy

Adversarial code reviews from a principal engineer who's seen too many
production incidents and is tired of pretending your code is fine.

## Commands

| Command | Scope | Description |
| --- | --- | --- |
| `/grumpy:audit` | Whole project | Composes every grumpy review into one picture + a prioritized, honestly-sized improvement plan |
| `/grumpy:simplify` | Current diff | Hard code-quality thresholds (complexity, coverage, dead/redundant code, type safety) — fixes what fails |
| `/grumpy:review` | Current diff | Comprehensive code review across 7 parallel aspects |
| `/grumpy:imagine` | Current diff | Production imagination: UX, logs, metrics, concurrency, error paths |
| `/grumpy:edge-cases` | Current diff | Code, product, and security blind spots |
| `/grumpy:fix` | Conversation | Reads prior review (or audit plan) and dispatches tier-routed agents to fix findings |
| `/grumpy:architecture` | Whole project | Structure, coupling, scalability, libraries, and conventions |
| `/grumpy:security` | Whole project | Auth, data exposure, injection vectors, and dependency risks |
| `/grumpy:cleanup` | Whole project | Dead code, tech debt, and cruft — pick what to remove |
| `/grumpy:product` | Diff or whole project | Experience, outcomes, metrics, and delight |

## Audit & thrifty execution

`/grumpy:audit` is the comprehensive pass: it runs the other reviews
(`architecture`, `security`, `review`, `product`, `edge-cases`, `cleanup`),
dedups and synthesizes them, and emits a milestone-ordered improvement plan.
Each step is **honestly sized** — a step tagged small must carry a runnable
`accept:` command, or it's promoted to large — and tagged with an `exec:` tier.

`/grumpy:fix` then routes each step by that tier (the **thrifty split**: cheap
where it can, strong where it must), passing the resolved model to each fix
agent and escalating one tier on a failed `accept:` check:

- `exec: trivial` → Haiku · `exec: standard` → Sonnet · large/unverifiable → Opus
- `--max-tier <role>` clamps the ceiling (native dispatch tops at `opus`: `exec-trivial < exec < analysis`)

The role→model map lives in `models.yaml` (single switch point; Claude native,
Fable marked `wrapper-required` until a per-provider runner exists).

## Grumpy Levels

All commands support `--level` to control intensity:

| Level | Persona |
| --- | --- |
| `grumpy` (default) | Weary, exasperated, professional |
| `grumpier` | More sarcasm, less patience, actively annoyed |
| `linus` | Brutal, unfiltered technical honesty |

## Worktree targeting

Every command accepts `--worktree <path>` (alias `--path <path>`). When set, all git operations (diff, branch detection, artifact dir) run against that worktree instead of the current directory:

```bash
/grumpy:review --worktree ~/src/app/.claude/worktrees/feature-x
```

Use it when the invoking session's cwd isn't the worktree you want reviewed (orchestrators, parallel `isolation: worktree` agents, `grumpy:audit → grumpy:fix` against a target repo). Flag absent = current directory, unchanged.

## Portability beyond Claude Code

Every command's subagent-fan-out and task-tracking steps are written to be
agent-neutral: they name the capability ("spawn independent subagents in
parallel", "use your harness's task-tracking feature if it has one") rather
than Claude's own Task/TaskCreate/TaskUpdate tools, and explicitly fall back
to doing the work sequentially / as a plain checklist on a harness with no
such primitive. Two spots stay deliberately Claude-Code-specific rather than
rewritten to sound portable when they aren't:

- **`gate.md`**'s note on Skill-tool instruction caching (`sdlc` plugin) —
  a Claude Code prompt-caching quirk, not a general concept.
- **`dispatch.md`**'s gate-recording claim — invoking a mode via a
  skill/sub-command dispatch mechanism only actually records a gate today
  when that mechanism is Claude Code's own `Skill` tool, since the
  auto-record hook hasn't been ported elsewhere (see `sdlc`'s README, "Codex
  CLI support").

Every command also has a generated `skills/<name>/SKILL.md` — see `sdlc`'s
README, "Codex CLI support", for how generation and the CI staleness check
work.

## Upgrading

**Upgrading to 2.0.0 (Gemini Mode removed):** `--gemini`, `GRUMPY_MODEL`, and
`scripts/gemini.ts` are gone. If you had `GRUMPY_MODEL` set in your shell profile,
it is now silently ignored — every command runs the normal multi-agent pipeline
regardless. `models.yaml`'s role→model map is unaffected; it still drives the
real tier-routed dispatch `/grumpy:fix` uses.

`grumpy` composes with `sdlc` through a shared path contract
(`.claude/grumpy/<branch>/` and `.claude/sdlc/<branch>/`): review and imagine read a plan
`/sdlc:plan` writes, and sdlc's gate chain reads the reports written here. If you use both,
upgrade them together — a skew leaves one side reading a directory the other never wrote.

You won't enjoy the feedback. That's the point.
