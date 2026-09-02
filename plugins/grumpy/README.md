# Grumpy

Adversarial code reviews from a principal engineer who's seen too many
production incidents and is tired of pretending your code is fine.

```sh
claude plugin marketplace add buritica/sharpen
claude plugin install grumpy@sharpen
```

## It argues with your diff, not just about it

A finding isn't a linter rule ID — it names the file, the consequence, and
whether it's a fact or a judgment call (illustrative excerpt, the shape
every `/grumpy:review` actually renders):

```markdown
## Critical Issues 🚨

- errors: swallows the network error, retry never fires [client.ts:142]

## Simplify This ✂️

- simplify: this abstraction feels wrong for a single call site [client.ts:88]
```

And it says so in a voice, not a checkbox:

> "You built a factory factory for something that happens once."
> "I'm going to pretend I didn't see this catch block that swallows exceptions."
> "This abstraction is solving a problem you don't have."

`--level grumpy|grumpier|linus` turns that up or down — `linus` drops all
hedging and backs every harsh line with the specific technical argument for
it. `/grumpy:fix` is what actually resolves what a review finds: it
dispatches tier-routed sub-agents (cheap model for a trivial fix, strongest
for anything unverifiable), and can defer a non-critical finding into a
`ponytail:` marker instead of forcing a fix at review time — see "Deferring
findings" below.

## Commands

| Command | Scope | Description |
| --- | --- | --- |
| `/grumpy:audit` | Whole project | Composes every grumpy review into one picture + a prioritized, honestly-sized improvement plan |
| `/grumpy:simplify` | Current diff | Code-quality thresholds judged against the merge base — new and regressed violations are fixed, legacy debt is reported, not failed |
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

## Simplify policy and config

`/grumpy:simplify` classifies every finding by comparing head against the
merge base, not by judging the current state of a file or function in
isolation. Only findings classified `new` or `regressed` block gate 2;
everything else — pre-existing debt the diff didn't make worse — is reported
but does not fail the gate. The verdict is always one of exactly three
phrasings, so a passing run with debt can never be mistaken for a clean one:
`Threshold compliant`, `Passes with legacy debt (N held, M improved, K
excepted)`, or `Blocked (N findings)`.

### Status

| Status | Meaning | Blocks? |
| --- | --- | --- |
| `compliant` | Under the healthy target at head | No |
| `improved` | Over threshold at base, measurably better at head | No — debt improved |
| `held` | Over at base, unchanged or within tolerance at head | No — debt held |
| `regressed` | Over at base and worse than tolerance allows at head | Yes |
| `new` | Over at head and not over (or absent) at base | Yes |
| `excepted` | Would block, but a documented debt record covers it | No — documented debt |

### Confidence

| Confidence | Can block? |
| --- | --- |
| `measured:<tool>` | Yes |
| `estimated` | Only for metrics whose estimation is mechanical — by default `loc_per_file`, `dead_code`, `any_unknown` (`block_on_estimate` in config). All other estimated metrics warn only. |
| `unmeasured` | Never. Reported as ⚪ with a one-line reason. |

### Healthy targets and default tolerances

| Metric | Healthy target | Tolerance |
| --- | --- | --- |
| Cyclomatic complexity | < 22 | +2 |
| Cognitive complexity | < 22 | +2 |
| Halstead difficulty | < 80 | Advisory only |
| LOC per file | Tiers [500, 1000, 1500] | +10 lines |
| Test coverage | 100% of changed lines | n/a |
| CRAP | < 25 | +5 |
| Surviving mutants | 0 | Advisory only |
| Dead code / redundant code / `any`/`unknown` | 0 per instance | n/a |

### LOC tiers

| Range | Meaning |
| --- | --- |
| < 500 | Healthy |
| 500 – 1,000 | Warning — growth needs a stated reason |
| 1,000 – 1,500 | Strong warning — new files should normally be split |
| > 1,500 | Hard block for **new** production files |

For a file that is already oversized, size alone doesn't block: net
reduction is `improved`, growth within tolerance is `held`, and growth
beyond tolerance is `regressed` — but `regressed` blocks only when the file
is at or above the warn tier (1,000). Below that it's reported, not failed.

Generated and vendor files are excluded by default: `.claude/**`, `.sharpen/data/**`, `vendor/**`,
`node_modules/**`, `third_party/**`, `dist/**`, `build/**`, `**/*.min.js`,
`**/*.min.css`, `**/*.lock`, `**/*.pb.go`, `**/*_pb2.py`,
`**/*_pb2_grpc.py`, `**/*.generated.*`, `**/__generated__/**`,
`**/*.g.dart`, `**/*.snap`, `**/*.map` — plus any file whose first 5 lines
carry a generated-header marker (`@generated`, `do not edit`,
`auto-generated`, `autogenerated`, `automatically generated`, `code
generated by`).

Test files (`tests/`, `__tests__/`, `spec/`, `test_*.py`, `*_test.go`,
`*.test.*`, `*.spec.*`, `conftest.py`, `testdata/`, `fixtures/`) never
block on size or complexity (`test_advisory` in config; a new `any` or a
dead helper in a test file still blocks). Extra basename globs go in
`test_patterns`.

Every judged finding carries a `blocking_reason` — `null` when it blocks or
is clean, otherwise the exact suppression(s) that applied (`advisory metric`,
`test file`, `estimated, not measured`, `unmeasured`, `below the warn tier;
growth needs a stated reason`, `documented debt: <reason>`). The report
quotes it instead of re-deriving the rule.

A diff that only *improves* legacy debt still verdicts as `Passes with
legacy debt` — the file or function remains over target. `Threshold
compliant` means nothing is over target at head.

### Config: `.sharpen/simplify.json`

An optional file committed at the repo root. `.sharpen/data/` is gitignored
per-run state; `.sharpen/` itself is the neutral, committable root. An
absent file means the defaults above apply unchanged.

- `thresholds` and `tolerance` merge per metric — set only the ones you want
  to override.
- `languages` is keyed by lowercase file extension without the dot, and
  holds `thresholds`/`tolerance` overrides scoped to that language.
- `exclude` and `test_patterns` extend their default lists; they do not
  replace them.
- `advisory`, `block_on_estimate`, `test_advisory`, and `debt` replace the
  defaults outright.
- `dead_code`, `redundant_code`, and `any_unknown` are judged per occurrence
  and take no `thresholds`/`tolerance` entry — the config is rejected if you
  add one.
- An `exclude` entry starting with `!` un-excludes a path the defaults (or
  an earlier pattern) would drop, e.g. `"!apps/foo/build/**"`.
- `debt[].path` is matched exactly (fnmatch against the repo-relative path);
  it does not suffix-match, so write the full path or a deliberate glob.
- Each `debt` record needs `path` (a glob) and `reason` (non-empty), and
  may optionally set `metric` and `issue`.

```json
{
  "thresholds": {
    "loc_per_file": [500, 1000, 1500],
    "cyclomatic": 22,
    "cognitive": 22,
    "coverage": 100
  },
  "tolerance": {
    "loc_per_file": 10,
    "cyclomatic": 2,
    "cognitive": 2,
    "crap": 5
  },
  "languages": {
    "rs": { "thresholds": { "cyclomatic": 30 } },
    "go": { "thresholds": { "cyclomatic": 30 } }
  },
  "exclude": ["**/*_pb2.py", "assets/**"],
  "advisory": ["halstead", "mutants"],
  "debt": [
    {
      "path": "src/main.rs",
      "metric": "loc_per_file",
      "reason": "legacy monolith; routing seam extracted in #34, rest tracked in #12",
      "issue": "#12"
    }
  ]
}
```

### CLI

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/simplify_policy.py" config
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/simplify_policy.py" loc --base <merge-base>
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/simplify_policy.py" judge --loc simplify-loc.json   # agent JSON lines on stdin
```

`judge --loc` folds the `loc` report (its findings, excluded paths, merge
base) into the judged output. Every output's `summary` records `verdict`,
`base`, `config_source`, `version` (the plugin version that judged),
`confidence` counts (measured / estimated / unmeasured), and `excluded` —
including agent findings on excluded paths, which are never judged. A
`judge` with nothing to judge returns verdict `unmeasured`, not `compliant`.
`.claude/**` and `.sharpen/data/**` are excluded by default so agent scratch
and artifacts are never measured as source.

Artifacts land next to `simplify.md`'s other output: `.claude/grumpy/<branch>/simplify-loc.json`
and `.claude/grumpy/<branch>/simplify-findings.json`.

## Deferring findings (`ponytail:` markers)

`/grumpy:fix` doesn't just fix or drop a finding — some findings get
**deferred**: left in place with a one-line comment marking the ceiling and
the trigger for revisiting it. The convention is borrowed verbatim from
[ponytail](https://github.com/DietrichGebert/ponytail), so it reads the same
whether or not that plugin is installed:

```
# ponytail: full table scan, add an index when accounts pass 10k
// ponytail: in-memory queue, move to a real broker past 1k msgs/sec
```

A marker with no observable trigger — a number, a metric, a condition
someone can check — isn't a valid deferral. "Later" doesn't count.

Eligibility is a fixed table, not a judgment call the fix agent makes per
finding:

| Finding | Deferrable? |
| --- | --- |
| 🚨 Critical | Never |
| ⚠️ Serious, fact-based | Never |
| ⚠️ Serious, judgment call, naming a performance/simplification/UX/observability/logging/metrics/concurrency concern (review.md's `simplify` aspect, or imagine.md's `ux-observability`/`logging-gap`/`metric-gap`/`rate-limit`/`concurrency` domains) | Yes — **and an issue must be filed or offered** |
| ⚠️ Serious, judgment call, any other aspect | Never |
| 🤔 Questionable | Yes |

Anything touching correctness, security, data loss, or trust-boundary
validation is never deferred, regardless of tag.

`/grumpy:review` and `/grumpy:imagine` read existing markers back: a
finding already covered by a valid, un-triggered marker at that site is not
re-raised on the next pass. `/grumpy:simplify` treats a marker the same way
it treats a documented `.sharpen/simplify.json` debt record, but only for
its own estimated findings (dead code, redundant code, unnecessary
abstractions) — never for a `measured:<tool>` metric like complexity or
coverage.

Pass `--file-issues` to `/grumpy:fix` to open a GitHub issue for each
deferred finding that needs one (`gh` required; deduped by search before
creating). Without the flag, or without `gh`, the fix report still lists a
ready-to-paste `## Would file` entry per deferral, so nothing is lost.

No separate ledger command ships here — `grep -rnE '(#|//|--) ?ponytail:' .`
lists every marker in the repo, and `/ponytail:ponytail-debt` is the ledger
when the ponytail plugin is installed.

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

Version-specific migration notes live in [`CHANGELOG.md`](CHANGELOG.md).

`grumpy` composes with `sdlc` through a shared path contract
(`.claude/grumpy/<branch>/` and `.claude/sdlc/<branch>/`): review and imagine read a plan
`/sdlc:plan` writes, and sdlc's gate chain reads the reports written here. If you use both,
upgrade them together — a skew leaves one side reading a directory the other never wrote.

You won't enjoy the feedback. That's the point.
