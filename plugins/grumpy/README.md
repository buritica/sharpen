# Grumpy

Adversarial code reviews from a principal engineer who's seen too many
production incidents and is tired of pretending your code is fine.

```sh
claude plugin marketplace add buritica/sharpen
claude plugin install grumpy@sharpen
```

## A finding, not a lint rule

Every line names the file, the consequence, and whether it's fact or
judgment (illustrative — the shape a real `/grumpy:review` renders):

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
it.

`/grumpy:fix` resolves what a review finds: tier-routed sub-agents (cheap
model for a trivial fix, strongest for anything unverifiable), one retry at
a higher tier if a fix's own check fails, and — for anything non-critical —
the option to defer into a `ponytail:` marker instead of forcing a fix at
review time. See "Deferring findings" below.

## Commands

| Command | Scope | Description |
| --- | --- | --- |
| `/grumpy:audit` | Whole project | Composes every grumpy review into one picture + a prioritized, honestly-sized improvement plan |
| `/grumpy:simplify` | Current diff | Code-quality thresholds judged against the merge base — new and regressed violations are fixed, legacy debt is reported, not failed |
| `/grumpy:review` | Current diff | Comprehensive code review across 7 parallel aspects |
| `/grumpy:imagine` | Current diff | Production imagination: UX, logs, metrics, concurrency, error paths, docs coverage |
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
- Findings from review, imagine and the other passes carry no tier, so they route by
  severity: Critical and fact-tagged Serious → Opus, judgment-tagged Serious and
  Questionable findings you chose to fix → Sonnet
- One fixer per file, holding all of that file's findings, not one per finding
- `--max-tier <role>` clamps the ceiling (fix's ladder tops at the `analysis` role: `exec-trivial < exec < analysis`)

The role→model map lives in `models.yaml`. Each role has the Anthropic API id,
the Claude Code alias passed to subagents (`opus`, `sonnet`, `haiku`), and the
model to start a Codex session on, since Codex (like pi) can't pick a model per
subagent, plus the OpenAI API id the no-agent path calls (`openai`, see "No agent
installed"). Review runs its finding passes on the `analysis` role; imagine stays
on the session's model until a larger benchmark run settles which model suits
it; fix routes by `exec:` tier, or by severity and fact/judgment when there is none; audit dispatches its dimension reviews on
`analysis`. A repo can override single values in a committed
`.sharpen/models.yaml`; the lookup and merge rules are at the top of
`models.yaml`, and every report names the model that ran. Lowering `analysis`
there lowers review's passes, audit's dimensions and fix's ceiling together.

## Measuring review quality

`scripts/bench-grumpy.py` (repo-level, not shipped in the plugin) benchmarks the
review and imagine passes. It runs the real pass headlessly on Claude Code, Codex
or pi, or with no agent through `scripts/oneshot.py` (`--host oneshot`, see "No agent
installed"), against fixture repos in `tests/bench/` (small planted ones plus two
real sharpen layers replayed at full size), scores the report the pass writes against
each case's answer key, records what each run cost and whether its passes ran in one
session or as subagents, and compares runs:

- **recall**: planted issues found, matched by file, a line window and a keyword,
  one finding per issue
- **precision**: of the distinct, located CRIT/WARN findings, the share that are
  planted or known-acceptable within their severity cap. Duplicates (a second finding
  for an issue already claimed) and findings with no file reference are counted on
  their own, not in precision.
- **blocking noise**: CRIT/WARN findings that match nothing
- **overrated**: acceptable findings rated above their cap (a missing log line flagged
  as a must-fix), counted against precision and compared on their own

Clean, comment-only and capability-bait cases have no planted issues, so any
blocking finding there is noise of the kind that makes review loops run long.
Results go to the gitignored `.sharpen/bench/`; nothing runs in CI. See
[`tests/bench/README.md`](tests/bench/README.md).

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

An optional file committed at the repo root. `.sharpen/data/` is per-run
state that ignores itself (sharpen writes a `.gitignore` holding `*` inside
it); `.sharpen/` itself is the neutral, committable root, so never ignore it
as a whole. An
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
python3 "${GRUMPY_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}/scripts/simplify_policy.py" config
python3 "${GRUMPY_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}/scripts/simplify_policy.py" loc --base <merge-base>
python3 "${GRUMPY_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}/scripts/simplify_policy.py" judge --loc simplify-loc.json   # agent JSON lines on stdin
```

`judge --loc` folds the `loc` report (its findings, excluded paths, merge
base) into the judged output. Every output's `summary` records `verdict`,
`base`, `config_source`, `version` (the plugin version that judged),
`confidence` counts (measured / estimated / unmeasured), and `excluded` —
including agent findings on excluded paths, which are never judged. A
`judge` with nothing to judge returns verdict `unmeasured`, not `compliant`.
`.claude/**`, `.sharpen/data/**`, `.sharpen/grumpy/**` and `.sharpen/sdlc/**` are
excluded by default so agent scratch and artifacts are never measured as source.

Artifacts land next to `simplify.md`'s other output: `.sharpen/grumpy/<branch>/simplify-loc.json`
and `.sharpen/grumpy/<branch>/simplify-findings.json`.

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

## Stacked branches review their own layer

`simplify`, `review`, `imagine` and `fix` diff against the base
`scripts/diff_base.py` prints, not always main. sdlc 4.17.0+ resolves it for
the whole gate chain and writes `.sharpen/sdlc/<branch>/base.json` (`.claude/sdlc/`
before sdlc 4.21, still read)
(`record-gate.py --set-base <parent>` pins it); without that file grumpy
applies the same order itself: `git config branch.<branch>.sdlcBase`, the open
PR's base (via `gh`), the branch's local upstream parent, then the default branch.
On a stack (main <- A <- B <- yours) the passes review only your layer, and
`/grumpy:fix` refuses to modify an existing file outside it: a finding there
that this diff didn't cause is recorded as new capability. A Critical, or a bug
this diff caused in a file it didn't touch, is asked about (fix here, or in the
branch that owns the file); an unattended run fixes it here, the one case where
fix edits another layer's file. A Critical the person sends to the owning branch
holds back fix's receipt, so gates 4 and 6 wait for that fix.

To see which base a pass will use and why, run `python3 scripts/diff_base.py
--why` from the plugin root (with `--worktree <path>` if needed). Each pass also
writes it at the top of its report. A source that is set but doesn't resolve (a
base.json or `sdlcBase` naming a renamed or deleted parent) or a failed `gh`
call prints a `[grumpy]` line and falls through to the next source. Without
sdlc's base.json, each pass makes its own `gh pr view` call (10 s timeout).

Each fix run writes its own receipt, `receipt.py fix --stage review` (gate 4)
or `--stage imagine` (gate 6), so with sdlc 4.17+ one run can never earn both fix
gates. It refreshes `fix.json` too, which an older sdlc reads. `/grumpy:fix`
passes `--stage auto`, which reads the stage from the receipts already written:
the stage of the pass still owed a fix, imagine first when both are (the order
sdlc fills the fix gates in). Before 2.18 the stage was whichever of review and
imagine ran last, so when both ran before any fix (the no-agent path below)
every fix stamped imagine and gate 4 needed `--stage review` by hand. A stageless `receipt.py fix`
removes earlier stage receipts so the fresh `fix.json` is the one read.

## Keeping review loops short (sharpen#37)

A review on a fix branch tends to ask for things the branch never set out to do, and
every one built there needs its own review round. Three rules keep the loop short:

- **Fix or new capability.** `/grumpy:fix` sorts each finding before acting on it. A
  finding whose remedy adds behavior the branch didn't set out to provide (a new flag,
  event, probe, endpoint or file; a guard for a gap that predates the branch), and that
  this diff didn't cause, is **new capability**: it is not built and leaves no marker in
  the code. Every host gets it recorded in the fix report under `## New capability`,
  with a ready-to-paste issue. With `--file-issues` and `gh`, it is also filed. A
  Critical finding is never sorted this way: fix asks the person where to fix it, and an
  unattended run fixes it here.
- **Settled findings.** The fix report keeps a `## Settled` list: one line per decided
  finding (new capability, `ponytail:` deferral, or declined by the person), each with
  its `file:line` and a reason. `/grumpy:review` and `/grumpy:imagine` hold back a
  finding only when an entry names the same spot and the same problem with a reason, and
  never a Critical one. The report says how many were held back and lists them, so
  nothing disappears silently. An entry for a whole file or category, or one with no
  reason, settles nothing.
- **Split guidance.** The review and imagine verdicts can now say "ship it and file the
  rest" (nothing Critical left, only new capability or nits) or "split it" (the fixes
  would add files outside the plan or outgrow the original diff).

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
such primitive. One spot stays deliberately Claude-Code-specific rather than
rewritten to sound portable when they aren't:

- **`dispatch.md`**'s note that dispatching (rather than inlining) a mode
  matters for gates only on a legacy cycle, where Claude Code's `Skill` hook
  is the recorder. On a receipt-backed cycle the pass's receipt records the
  gate on any host.

Every command also has a generated `skills/<name>/SKILL.md` — see `sdlc`'s
README, "Codex CLI support", for how generation and the CI staleness check
work.

### pi (coding agent)

`grumpy` is skills-only — no hooks to port — so under pi it installs as a pi
package whose `pi` manifest exposes `./skills` directly:

```sh
pi install git:...sharpen --dir plugins/grumpy
```

Its one extension (`extensions/grumpy-env.ts`) only exports
`GRUMPY_PLUGIN_ROOT`, so each pass finds its own files (`scripts/receipt.py`,
`scripts/simplify_policy.py`, `models.yaml`). No pi extension sets the shared
`CLAUDE_PLUGIN_ROOT`; see [`docs/hosts.md`](../../docs/hosts.md). The gate chain still works:
`/sdlc:gate` reads grumpy's `review`/`imagine` reports the same way it does
under Claude, and skill-gated gates record from the receipt each pass writes
(see `docs/pi.md` and sdlc's `gate.md`). In the top-level bundle, grumpy's `audit` skill is
excluded because pi skill names are flat and sdlc's `audit` wins the collision;
install grumpy alone for its `audit` skill.

## No agent installed (`scripts/oneshot.py`)

With no coding agent at all, the review and imagine passes can still run against a chat
API:

```sh
export OPENAI_API_KEY=...                      # read from the environment only
python3 <grumpy>/scripts/oneshot.py review --provider openai
python3 <grumpy>/scripts/oneshot.py imagine --provider openai

export SHARPEN_LLM_URL=http://localhost:11434/v1   # Ollama, llama.cpp, vLLM, LM Studio
export SHARPEN_LLM_API_KEY=...                     # only if the server wants a bearer token
python3 <grumpy>/scripts/oneshot.py review --provider compatible --model <a model you pulled>
```

`<grumpy>` is the grumpy plugin folder: a clone of sharpen's `plugins/grumpy`, or the
installed plugin's cache folder. Arm a gate cycle first if you want the gate
(`python3 <sdlc>/scripts/record-gate.py --init small-medium`), or `--verify review` has
nothing to record into (the same goes for `--verify imagine`). Commit first: the pass
reviews `HEAD`, refuses a dirty tree, and refuses a detached HEAD or the default branch
before it calls anything.

When both passes ran before any fix, run `/grumpy:fix` once for the fixes (it reads both
reports and earns gate 6, which sdlc opens first once both pass gates are recorded), then
once more for gate 4: each fix run earns one fix gate, and the stage is picked for you.

`--provider` defaults to `SHARPEN_LLM_PROVIDER`, else `openai`: **a bare `oneshot.py review`
(or `imagine`) sends your code to OpenAI.** Set `SHARPEN_LLM_PROVIDER=compatible` on a machine that should
never do that. `--dry-run` prints the finding-pass prompts and a count of the calls it would
make (not the aggregation prompt, which depends on the passes' answers) and calls nothing;
`--aspects` picks review aspects, or imagine's simulation passes (`happy-path`, `state`,
`concurrency`, `observability`, `docs`); `--worktree` points it at another checkout; `--jobs` is how
many finding passes run at once (4 for `openai`, 1 for `compatible`: a local single-GPU
server serializes prompt evaluation, and parallel prompts only queue behind each other while
every socket timeout keeps ticking); `--timeout` is the seconds per call (300). sdlc's
`openai_adapter.py` and `local_llm_adapter.py` run the gate commands from a capability
manifest and then this, once for each of `review` and `imagine` the manifest declares.

**A local server needs a context window that fits the prompt.** Each finding pass is the
diff (up to 200k characters) plus the changed files' text (up to another 200k) plus the
command's own instructions. Only `model` and `messages` are sent, so the server's default
context applies: Ollama's default `num_ctx` is a few thousand tokens and it truncates
silently, which drops the instructions at the top and makes the pass answer in prose
(reported as "errored", no receipt) or review a fragment. Raise the server's context (for
Ollama, a `Modelfile` with `PARAMETER num_ctx`, or `OLLAMA_CONTEXT_LENGTH`) to cover the
prompt, or narrow the review with `--aspects` and smaller diffs.

**It is the same review, run differently.** The prompts are built from
`commands/review.md` each run, not written separately: the persona, the aspect list, Step 3's
finding-pass template and its `CRIT`/`WARN`/`NOTE` severities, the Audit discipline, Step 4
with its settled-findings rules and report template, and the Personality Guidelines. It sends
one finding call per aspect (in parallel), saves what they returned to
`.sharpen/grumpy/<branch>/review-passes.jsonl`, then one aggregation call, saves
`.sharpen/grumpy/<branch>/review.md` with `_Model:_` and `_Base:_` lines it writes itself
(the model line names the aspects that ran), and leaves the same receipt an agent host
leaves, plus an `executor` naming the path, provider, model and aspects. `record-gate.py
--verify review` records gate 3 from it, and `--status` says the gate came from `oneshot`. If
`review.md` changes in a way the extraction can't follow, the script stops with exit 2 and
`tests/test_oneshot.py` fails; it never falls back to a prompt of its own.

**Imagine works the same way, from `commands/imagine.md`.** The persona, Step 2's five
simulation prompts (one call each: `happy-path`, `state`, `concurrency`, `observability`,
`docs`), the severities Agent 1 defines, the Audit Discipline, all of Step 3 (the section
mapping, settled findings and the report template) and the Personality Guidelines. Each
pass's CONTEXT and `HANDLED` lines are kept along with its findings, so the aggregation can
fill the narrative sections and Strengths. The passes' lines go to
`.sharpen/grumpy/<branch>/imagine-passes.jsonl`, the report to `imagine.md` with the
`_Model:_` and `_Analyzed: <base>, from <source> — HEAD at <sha>_` lines written by the
script, and the receipt earns gate 5 through `record-gate.py --verify imagine`. A sixth
agent in `imagine.md`, a renamed one, or a lost placeholder stops the run with exit 2
until `IMAGINE_AGENTS` in `scripts/oneshot_prompts.py` names it. Imagine uses the same default model as
review: there is no session model on this path, so `models.yaml`'s `analysis.openai`.

While it runs, stderr gets one line per finished pass (duration, finding lines, tokens when
the server reports them, the served model if it differs, the request id) and one per retry,
so a hung server and a slow model look different. A 429 or 5xx is retried once, after the
server's `Retry-After` when it sends one (capped at 60 seconds) and a short backoff
otherwise; a 400/401/403/404 stops the run and cancels the passes still queued.

**What it can't do compared with an agent host:**

- It sees only the diff and the changed files' text at `HEAD` (capped), never the rest of
  the repo, so it can't follow a call site or check a caller it wasn't shown.
- One model does every aspect: no per-pass model routing from `models.yaml`.
- It doesn't append the review or imagine note to the sdlc plan (review's Step 6,
  imagine's Step 5).
- Simplify and fix edit code, so gates 2, 4 and 6 still need an agent or a person. Gates 3
  and 5 can be earned here.
- Whether it reviews as well as an agent host is not measured yet. `bench-grumpy.py run
  --host oneshot --provider openai` (or `compatible --model <id>`) measures it on the same
  fixtures as the agent hosts; no run against a real model has been recorded, so the
  `openai` model in `models.yaml` stays marked unverified until one is.

**Your code leaves the machine with `--provider openai`.** The diff and the changed files'
full text are sent to OpenAI. To keep the code local, run a model on your own machine and
use `--provider compatible`. The key is read from the environment only: it is never written
to the report, the receipt or a log line, and provider error text is redacted before it is
printed.

**It earns the gate only for a complete pass.** If any aspect's call fails or answers
without finding lines (prose, or nothing at all: an empty answer is a provider that reviewed
nothing, not a clean pass), the report is still saved so you can read it, stamped
`_Incomplete: <aspects>_`, but no receipt is written: re-run to earn gate 3 (or 5). Finding lines a
pass got wrong (a missing `:line`, a four-field line) are dropped, counted on stderr and
named to the aggregator, so the report says the aspect may be incomplete rather than
passing the rest off as the whole truth. A diff or file cut at the context caps is marked
where it was cut and said on stderr. The receipt names the commit that was diffed, and the
run refuses it if `HEAD` moved meanwhile. If the aggregation call fails, the finding passes'
lines are still in `review-passes.jsonl` (or `imagine-passes.jsonl`); nothing paid for is
lost.

The model: `--model`, else `SHARPEN_LLM_MODEL`, else (from sdlc's adapters) the manifest's
`provider.model`, else `models.yaml`'s `analysis.openai` for `--provider openai`, for
both passes. `compatible` has no default.

Exit codes: 0 report and receipt written. 1 the pass earned nothing: a dirty tree, an empty
diff, a git failure, an aspect that failed or answered without finding lines, an unusable aggregation,
`HEAD` moving during the run, or a refused receipt. 2 setup: an unknown flag, provider or
aspect, no key, no model, a detached `HEAD` or the default branch, a provider that rejects the
request as unauthorized or unknown (400, 401, 403, 404), or an extraction error.

## Upgrading

Version-specific migration notes live in [`CHANGELOG.md`](CHANGELOG.md).

`grumpy` composes with `sdlc` through a shared path contract
(`.sharpen/grumpy/<branch>/` and `.sharpen/sdlc/<branch>/`): review and imagine read a plan
`/sdlc:plan` writes, and sdlc's gate chain reads the reports written here. Before grumpy
2.15 and sdlc 4.21 both lived under `.claude/`, which headless Claude Code refuses to
write. The old folders are still read: a receipt counts by whichever copy is newer, a
plan or `fix.md` found only there is copied into `.sharpen/` before a pass appends to
it, and other reports are read from there when the new copy is missing.

**grumpy 2.15+ needs sdlc 4.21+, or gates 2–6 never record.** An older sdlc looks for
receipts only under `.claude/grumpy/`, never sees the ones grumpy 2.15 writes to
`.sharpen/grumpy/`, and doesn't say so. Upgrade both together. (sdlc 4.21 with an older
grumpy is fine: it still reads `.claude/grumpy/`.)

You won't enjoy the feedback. That's the point.
