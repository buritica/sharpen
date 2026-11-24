# grumpy changelog

Version-specific migration notes. Nothing reads this file programmatically — it exists so a
past release's behavior change stays discoverable without bloating the current README.

## 2.20.0 — review runs every aspect in one Opus agent on small and medium diffs

`/grumpy:review` used to start one subagent per aspect, up to seven, on every diff. It
now picks by size. A diff up to 60,000 characters (about 1,500 diff lines) runs
**single**: one subagent on the `analysis` role (Opus) works through every aspect in
turn. A larger diff still runs **fanout**, one subagent per aspect. `--single` and
`--fanout` override the rule, and the report's `_Model:_` line says which ran and why.

Most of a pass's cost is the agent reading the repository around the diff, and fanout
pays it once per aspect. In the first benchmark round (2026-11-23, Opus, two real-size
replays), fanout found 15 to 25 points more of the planted issues but cost three to four
times as much and raised blocking noise from about 1 finding per run to 5 to 11. Small
and medium diffs take the cheaper, quieter path; large ones keep the recall. The single agent runs on `analysis` whatever model the
session uses, because a Sonnet session running the aspects itself missed most planted
issues.

The benchmark takes `--mode single`, and `observed_mode` reads one subagent as `single`.

## 2.19.0 — the fix pass costs less: one fixer per file, judgment calls on Sonnet

`/grumpy:fix` dispatches one fixer per file, carrying every finding in that file, instead
of one per finding. A fixer's cost is mostly reading the file, its callers and its tests
before it edits, so fifteen findings in three files are now three fixers, not fifteen. A
finding that spans files waits for the tasks of the other files it touches.

Findings without an `exec:` hint (everything except `/grumpy:audit`'s steps) used to route
to `analysis` (Opus) every time. They now route by what the report says: Critical and
untagged (`fact`) Serious findings stay on `analysis`; Serious findings tagged `(judgment)`
and Questionable findings you choose to fix in Step 5 go to `exec` (Sonnet), except ones
that touch correctness, security, data loss or a trust boundary, which stay on `analysis`.
A file's task runs on the highest role any of its findings needs. Fixing a rename or a doc
on Opus paid the strong tier for nothing, and a review's Serious list often carries
several judgment calls.

Non-audit findings carry no `accept:` command, so a Sonnet fix has no machine check. A
finding now escalates when its fixer reports it `not fixed` or the orchestrator's read of
its hunk (`self-checked`) finds it doesn't address the finding: the orchestrator undoes its
edits, then retries it one rung up, once per finding.

- The summary says how many tasks ran on each model.
- To keep every review and imagine fix on Opus, pin `exec` to `opus` in
  `.sharpen/models.yaml`. That also moves audit's `exec: standard` steps; audit's
  `exec: trivial` steps stay on `exec-trivial` unless you pin that role too.
- A repo `.sharpen/models.yaml` that pinned `exec` lower than Sonnet now also covers
  judgment Serious and chosen Questionable fixes, which used to ignore it.
- A failed finding is undone on its own, by content, and any file it created is deleted.
  The undo is checked against a pre-task copy of the file, kept under
  `.sharpen/grumpy/<branch>/pre-task/`; a change no finding claims is undone too. If the
  edits can't be separated, the whole file goes back to its pre-task content and its good
  fixes are reapplied in the next wave; it lands under `## Uncertain` only when that wave
  can't settle it. Nothing is stashed.
- A finding that passed is re-checked whenever its file changes later (an undo, a retry, a
  cross-file task), and so is a finding lifted by another finding's fix.
- The run stops before its first edit when the worktree has uncommitted changes, so they
  never land in the fix commit.
- No receipt is written while a Critical sits under `## Not fixed` or `## Uncertain`
  (before, only when it was blocked elsewhere), so gate 4/6 waits for it.

## 2.18.0 — the fix pass picks its own stage

`/grumpy:fix` now writes its receipt with `receipt.py fix --stage auto`, which reads the stage
from the receipts already on disk: the stage of the pass still owed a fix (its receipt is
newer than that stage's last fix receipt; imagine's also newer than review's, so one left
from before a reset doesn't count), imagine first when both are. That is the order sdlc's
`determine_gate` fills the fix gates in once both pass gates are recorded. When
neither is owed, a repeated fix keeps the last fix's stage.

Before, the stage was whichever of review and imagine ran last, so after the first fix the
next one stamped imagine again. The no-agent adapters (sdlc 4.23's `openai_adapter.py` and
`local_llm_adapter.py`) run both passes before any fix: the first fix earned gate 6 and gate
4 could only record from a hand-run `receipt.py fix --stage review`. Now the first fix run
earns gate 6 and the next one gate 4, with no flag. The agent chain's order (review, fix,
imagine, fix) gets the same stages as before.

- An explicit `receipt.py fix --stage review|imagine` still works as before.
- The receipt line on stdout names the stage it wrote.
- Nothing to migrate.

## 2.17.0 — imagine with no agent installed, and a benchmark host for it

`python3 <grumpy>/scripts/oneshot.py imagine` runs the imagine pass against a chat API, the
way 2.16 ran review: every prompt is built from `commands/imagine.md` at run time (the
persona, Step 2's five simulation prompts, the severities, the Audit Discipline, Step 3 and
the Personality Guidelines), one call per simulation (`happy-path`, `state`, `concurrency`,
`observability`, `docs`, the names `--aspects` takes) plus one aggregation call. It saves
`.sharpen/grumpy/<branch>/imagine.md` with `_Model:_` and `_Analyzed:_` lines it writes
itself, keeps the passes' lines in `imagine-passes.jsonl`, and writes the receipt, so
`record-gate.py --verify imagine` records gate 5. The same rules as review apply: an aspect
that fails or answers in prose leaves the report stamped `_Incomplete:_` and no receipt.

- Imagine's passes return CONTEXT and `HANDLED` lines as well as findings; the parser keeps
  them (the labels come from the prompts themselves) so the aggregation can fill the
  narrative sections and Strengths. A sixth agent, a renamed one or a lost placeholder in
  `imagine.md` stops the run with exit 2 until `IMAGINE_AGENTS` names it.
- An imagine pass that dropped malformed lines and kept no finding line counts as errored
  (no receipt), since its findings may be the lines that were dropped; CONTEXT lines alone
  no longer hide that. Review's rule is unchanged. The `_Incomplete:_` stamp now says "no
  pass lines" rather than "no finding lines".
- The prompt extraction for both passes moved from `oneshot.py` to
  `scripts/oneshot_prompts.py`; `oneshot.py` still exposes the same names.
- Imagine's default model on this path is `models.yaml`'s `analysis.openai`, the same as
  review's: there is no session model to fall back to.
- The no-agent preamble now asks for one short line, never an empty message, when a pass
  has nothing to report. An empty answer still counts as a provider that answered nothing.
- `scripts/bench-grumpy.py` (repo-level) gains `--host oneshot` with `--provider
  openai|compatible`, so this path is measured on the same fixtures as Claude Code, Codex
  and pi. No real-model run is recorded yet; `models.yaml`'s `openai` column stays
  unverified until one is.

## 2.16.0 — review with no agent installed (`scripts/oneshot.py`)

`python3 <grumpy>/scripts/oneshot.py review` runs the review pass against a chat API instead
of a coding agent: OpenAI (`--provider openai`, key in `OPENAI_API_KEY`) or any
OpenAI-compatible server such as Ollama (`--provider compatible`, `SHARPEN_LLM_URL`). It
builds every prompt from `commands/review.md` at run time, one finding call per aspect plus
one aggregation call, saves `.sharpen/grumpy/<branch>/review.md` and writes the usual
receipt, so `record-gate.py --verify review` records gate 3. Simplify, fix and (until
2.17) imagine still need an agent host. The diff and changed files go to the
provider; see the README's "No agent installed".

- It earns the gate only for a complete review: an aspect that fails or answers without
  finding lines (prose, or an empty answer) leaves the report, stamped `_Incomplete:_`, but
  no receipt (exit 1); the receipt names the commit that was diffed (`write_receipt` takes
  the reviewed `head` and refuses any other); and a detached HEAD, the default branch, or a
  provider rejecting the key or model (exit 2) stop it before anything more is spent, with
  the passes still queued cancelled.
- Nothing paid for is lost: the finding passes' lines are saved to
  `.sharpen/grumpy/<branch>/review-passes.jsonl` before the aggregation call. Malformed
  finding lines are counted and named to the aggregator; a diff or file cut at the caps is
  marked. Each finished pass and each retry logs one line (duration, tokens, request id);
  a 429 waits for `Retry-After`; git failures print git's reason. `--jobs` defaults to 1 for
  `compatible` (local servers serialize anyway) and 4 for `openai`; `--provider` defaults to
  `SHARPEN_LLM_PROVIDER`, else `openai`.
- `receipt.py` gains `named_branch()` and `require_clean()` (shared with `oneshot.py`), and
  refuses an `executor` without a non-empty `host`.
- Receipts may carry an `executor` (`{host, provider, model, aspects}`); `oneshot.py` writes
  it and sdlc 4.22's `--status` shows host, provider and model next to the gate. Agent hosts
  don't write it.
- `models.yaml` has an `openai` column (API ids). `oneshot.py` reads `analysis.openai`
  when `--provider openai` gets no `--model`. Unverified until the benchmark measures it.
- sdlc 4.22's `openai_adapter.py` and `local_llm_adapter.py` call `oneshot.py`, so upgrade
  both plugins together.

## 2.15.0 — reports and receipts move to `.sharpen/grumpy/`

Every pass now writes its report, and its receipt, to `.sharpen/grumpy/<branch>/` instead of
`.claude/grumpy/<branch>/`. Claude Code treats `.claude/` as its own configuration, and a
headless session (`claude -p`, the benchmark) refuses a Write-tool save there, so the pass
printed its report; with no saved report, a pass leaves no receipt and earns no gate.

**grumpy 2.15 needs sdlc 4.21 or newer, or gates 2–6 never record.** sdlc 4.20 and older
look for receipts only under `.claude/grumpy/`, never find the ones 2.15 writes, and say
nothing about it: every review gate just stays open. Upgrade both plugins together
(`claude plugin marketplace update`, then restart the session). The other skew,
sdlc 4.21 with an older grumpy, still records gates (sdlc 4.21 reads receipts an older
grumpy left under `.claude/grumpy/`), but that grumpy misses a plan sdlc 4.21 wrote and
resolves the stack base on its own. When a legacy receipts folder exists for the branch,
`receipt.py` now says on stderr that an sdlc 4.20 or older would not see the new receipt.

- `/grumpy:fix` reads each report from the new folder, and from the old one when the new
  folder has no copy of that report, so a review run before the upgrade is still found. review and imagine read the settled list in `fix.md`
  the same way.
- review, imagine, simplify and fix read the sdlc plan from `.sharpen/sdlc/<branch>/plan.md`,
  falling back to `.claude/sdlc/`, and `diff_base.py` reads sdlc's `base.json` from
  `.sharpen/sdlc/<branch>/` first. Upgrade sdlc to 4.21 alongside: it writes plans and the
  base file to the new folder, and reads receipts from both.
- `/grumpy:simplify` also excludes `.sharpen/grumpy/**` and `.sharpen/sdlc/**` from
  measurement by default.
- `.sharpen/grumpy/` ignores itself when created (a `.gitignore` holding `*`). `.sharpen/`
  as a whole never does, because it holds the committed `simplify.json`.

## 2.14.0 — current models, a Codex column, review routes

`models.yaml` moves to the current Claude ids (`claude-opus-5-5`, `claude-sonnet-5-5`,
`claude-haiku-5-5`) and drops the `wrapper_required` machinery. Audit stays on opus:
Claude Code now accepts a `fable` alias, but it resolves to Fable 5.1 unless
`ANTHROPIC_DEFAULT_FABLE_MODEL` is set and the benchmark doesn't cover audit yet, so
moving audit to it waits for a measurement. The `audit` role now names the model to start
an audit session on, since synthesis runs in the session.

Each role gains a `codex` column: the model to start a Codex session on, because Codex
can't choose a model per subagent. The ids come from OpenAI's Codex models page; which one
fits which role is unverified until the benchmark has measured it on Codex. pi has no
column and runs everything on the session's model.

**Behavior changes:**

- `/grumpy:review` reads the map. Its finding passes run on the `analysis` role, so on
  Claude Code they are dispatched on `opus` instead of inheriting the session's model. A
  review started from a Sonnet session now costs Opus rates for its passes. A rejected or
  rate-limited pass reruns once on the session's model.
- A repo can override single values in a committed `.sharpen/models.yaml`. The lookup
  and per-role, per-column merge rules are written once, at the top of `models.yaml`. To
  pay less, pin `analysis` lower there, knowing that it also lowers audit's dimension
  reviews and fix's escalation ceiling.
- Review and imagine reports gain a `_Model:_` line, fix's summary names each task's
  model and any escalation, and audit's run header names the model of every dimension.
- If an override makes two rungs of fix's ladder the same alias, a failed check is
  reported as blocked instead of escalating to the same model.

`/grumpy:imagine` is not routed. On the benchmark run behind this change (Claude Code,
Sonnet session, 3 repeats; the table is in PR #60) opus matched or beat the session model
on review but lost recall on one imagine case. 3 repeats is too small a sample to call, so
imagine stays on the session's model until a larger rerun. On Claude Code the benchmark's
`--model` sets the session model; review's passes follow the `analysis` role, so to compare
models for review, pin `analysis` with `bench-grumpy.py run --analysis <alias>`.

## 2.13.0 — a review benchmark replaces the recall eval

The one-fixture recall eval (`scripts/eval-grumpy-findings.py`, `tests/fixtures/sample.diff`,
`golden.json`) is gone. Its scorer counted a planted issue as found when any finding in the
same file used any of its keywords, including words like `none` and `except`, ignored line
numbers, and let one finding claim several issues, so its 100% could be earned by findings
about something else. It also scored raw sub-agent lines rather than the rendered report
`/grumpy:fix` reads, and had no imagine fixture.

`scripts/bench-grumpy.py` replaces it: seven fixture repos under `tests/bench/` (planted
issues for review and imagine, plus clean, comment-only and capability-bait cases, and `billing-settled`, which carries
the old eval's `--expect-absent` settled-list check), a
line-aware one-to-one scorer that reports recall, precision and blocking noise, a headless
runner for Claude Code, Codex and pi, `compare` for before/after runs, and `rescore` to
re-score saved reports after a scorer or key change. Results are
written to the gitignored `.sharpen/bench/`. No skill or command behavior changed, so an
in-flight gate cycle is unaffected.

## 2.12.0 — the report folder ignores itself

Every pass that creates `.claude/grumpy/<branch>/` now also writes
`.claude/grumpy/.gitignore` holding `*` (one shell line beside each command's `mkdir -p`,
and `receipt.py` does the same), so reports and receipts stay out of git in a repo whose
own `.gitignore` never listed them, on every host. An existing `.gitignore` there is left
alone. Pairs with sdlc 4.18.0, which does the same for `.sharpen/data/`, `.claude/sdlc/`
and `.claude/worktrees/`.

## 2.11.0 — stacked branches review their own layer, fix sorts out new capability

`/grumpy:fix` sorts each finding as a fix or as new capability before acting on it, and
records new-capability findings in its report (`## New capability`, with a ready-to-paste
issue) instead of building them; `--file-issues` also files them where `gh` is available.
Its report gains a `## Settled` list (`file:line | finding | decision | reason`) that
`/grumpy:review` and `/grumpy:imagine` read, holding back a finding only when an entry
names the same spot and problem with a reason, never a Critical one, and always listing
what they held back. Both verdicts gain "ship it and file the rest" and "split it". The
sub-agent prompts and their pipe format are unchanged. Nothing to migrate: an older
`fix.md` without `## Settled` holds nothing back.

Every gate pass (`simplify`, `review`, `imagine`, `fix`) now diffs against the base
`scripts/diff_base.py` prints: sdlc's resolved base (`.claude/sdlc/<branch>/base.json`,
sdlc 4.17.0+), else `git config branch.<branch>.sdlcBase`, else the open PR's base, else
the branch's local upstream parent, else the default branch. On a stacked branch the passes
review only its own layer, and `/grumpy:fix` refuses to edit an existing file outside that
diff (it records such a finding as new capability instead; a Critical there is asked
about, and fixed here on an unattended run). Each fix run now writes its own receipt
(`receipt.py fix --stage review|imagine` -> `fix-review.json` / `fix-imagine.json`), so
one run can't earn both fix gates with sdlc 4.17.0+; it refreshes `fix.json` too, so an
older installed sdlc still records the fix gates.

What changes without sdlc 4.17: the passes no longer always diff against the default
branch. With no base file they still try `branch.<branch>.sdlcBase`, the open PR's base
and a local upstream branch first, so a branch whose PR targets another branch, or that
tracks a local parent, now reviews only its own layer. A pass that can't run
`diff_base.py` says so and falls back to the default branch. A base.json or `sdlcBase`
that no longer resolves, or a failed `gh` call, prints a `[grumpy]` line instead of
falling through silently, and each report names the base it used and where it came from.

`/grumpy:fix` picks its receipt's stage from the review and imagine receipts' timestamps,
writes no receipt while a Critical is blocked on another branch, and records the base,
reverted out-of-scope edits and open questions in `fix.md`. A stageless `receipt.py fix`
removes earlier stage receipts.

## 2.10.0 — plugin paths resolve on every host

Every path a pass reads from its own plugin (`scripts/receipt.py`,
`scripts/simplify_policy.py`, `models.yaml`) is now spelled
`${GRUMPY_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}`. Under pi, simplify and fix used to read
sdlc's directory (sdlc's extension set the shared variable for the whole process), so
simplify always reported lines of code as unmeasured and fix never read `models.yaml`.
`/grumpy:fix` and `/grumpy:cleanup` no longer assume Claude Code's AskUserQuestion: they
use a structured question tool when the host has one and ask in chat otherwise. sdlc 4.15.0
reads `scripts/receipt.py`'s presence to tell whether this grumpy writes receipts.

## 2.9.0 — every pass leaves a receipt

`/grumpy:simplify`, `/grumpy:review`, `/grumpy:fix` and `/grumpy:imagine` now end by running
`scripts/receipt.py <pass> --worktree <path>`, which writes
`.claude/grumpy/<branch>/receipts/<pass>.json` with the branch and its current HEAD
(schema: `schemas/grumpy-receipt.v1.schema.json`). sdlc 4.14.0+ records gates 2 through 6
from these receipts instead of from the `Skill` tool event, so the gates now record on
Codex, fx and pi as well as Claude Code. The receipt is evidence that the pass ran, not a
security boundary. It also names the pass's report and its sha256: no saved report, no
receipt, and sdlc refuses a receipt whose report is missing, and warns when it changed. A detached HEAD is refused, because a gate is keyed by branch, and so
is a dirty tree: commit the pass's edits first, or the commit that follows would make
every gate stale. Under pi, a new one-line extension exports `GRUMPY_PLUGIN_ROOT` so the
passes can find `receipt.py` (sdlc's extension owns `CLAUDE_PLUGIN_ROOT` there).

## 2.8.0 — `/grumpy:imagine` gains documentation coverage

`/grumpy:imagine` now dispatches a 5th simulation agent (Documentation Coverage) alongside
happy path, state transitions, concurrency, and observability. It checks for new/changed
user-facing behavior (API, CLI flag, config key, env var) with no documentation anywhere in
the diff, stale docs describing behavior the diff just changed, and operational knowledge
(failure modes, migrations, rollback paths) an on-call engineer couldn't find written down.
Findings use three new DOMAIN values — `docs-gap`, `stale-docs`, `ops-gap` — and land under a
new "## Documentation Coverage" report section with matching subsections. `/grumpy:review`
already had an equivalent `docs` aspect; nothing there changed. No migration needed for an
in-flight gate cycle — the gate key and auto-record mechanism are unchanged, only what the
skill produces when it runs.

## 2.7.0 — `ponytail:` markers, defer, `--file-issues`

`/grumpy:fix` gained a third disposition alongside fixed and disputed: **defer**. A
non-critical finding eligible per a fixed severity/confidence table (never Critical, Serious
only when judgment-tagged in a handful of aspects/domains and only alongside a filed issue,
Questionable freely) gets a `ponytail:` comment at its site instead of a fix, replacing the
old "logged but not fixed" dead end. `--file-issues` opens a deduped GitHub issue per
deferral that needs one; without it, the persisted fix report carries a ready-to-paste
`## Would file` entry instead. `/grumpy:review` and `/grumpy:imagine` skip re-raising a
finding already covered by an untriggered marker. See "Deferring findings" above for the
full eligibility table — nothing about an in-flight gate cycle needs migrating; the gate keys
and auto-record mechanism are unchanged.

## 2.6.0 — diff-aware simplify

Gate 2 now passes with legacy debt — a PR that merely touches an already-oversized file or an
already-complex function no longer fails for that alone; only findings the diff makes `new`
or `regressed` against the merge base block. The verdict wording changed to the three fixed
phrasings above (`Threshold compliant`, `Passes with legacy debt (...)`, `Blocked (...)`), so
anything scripting against the old free-form text needs updating. The measurement sub-agents
now return JSON lines (`metric`, `file`, `symbol`, `base`, `head`, `confidence`) judged by
`simplify_policy.py`, instead of the old pipe-delimited lines. `.sharpen/simplify.json` is
optional; if it's absent, the previous thresholds are used as the healthy targets, unchanged.
An already-recorded `simplify` gate on an in-flight branch stays recorded — the gate key and
the auto-record mechanism are unchanged; only what the skill does when it runs changed.

## 2.0.0 — Gemini Mode removed

`--gemini`, `GRUMPY_MODEL`, and `scripts/gemini.ts` are gone. If you had `GRUMPY_MODEL` set in
your shell profile, it is now silently ignored — every command runs the normal multi-agent
pipeline regardless. `models.yaml`'s role→model map is unaffected; it still drives the real
tier-routed dispatch `/grumpy:fix` uses.
