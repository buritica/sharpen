# Grumpy

Adversarial code reviews from a principal engineer who's seen too many
production incidents and is tired of pretending your code is fine.

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

**Upgrading to 2.6.0 (diff-aware simplify):** gate 2 now passes with legacy
debt — a PR that merely touches an already-oversized file or an
already-complex function no longer fails for that alone; only findings the
diff makes `new` or `regressed` against the merge base block. The verdict
wording changed to the three fixed phrasings above (`Threshold compliant`,
`Passes with legacy debt (...)`, `Blocked (...)`), so anything scripting
against the old free-form text needs updating. The measurement sub-agents
now return JSON lines (`metric`, `file`, `symbol`, `base`, `head`,
`confidence`) judged by `simplify_policy.py`, instead of the old
pipe-delimited lines. `.sharpen/simplify.json` is optional; if it's absent,
the previous thresholds are used as the healthy targets, unchanged. An
already-recorded `simplify` gate on an in-flight branch stays recorded — the
gate key and the auto-record mechanism are unchanged; only what the skill
does when it runs changed.

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
