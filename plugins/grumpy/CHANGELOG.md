# grumpy changelog

Version-specific migration notes. Nothing reads this file programmatically — it exists so a
past release's behavior change stays discoverable without bloating the current README.

## 2.15.0 — reports and receipts move to `.sharpen/grumpy/`

Every pass now writes its report, and its receipt, to `.sharpen/grumpy/<branch>/` instead of
`.claude/grumpy/<branch>/`. Claude Code treats `.claude/` as its own configuration, and a
headless session (`claude -p`, the benchmark) refuses a Write-tool save there, so the pass
printed its report; with no saved report, a pass leaves no receipt and earns no gate.

**grumpy 2.15 needs sdlc 4.21 or newer, or gates 2–6 never record.** sdlc 4.20 and older
look for receipts only under `.claude/grumpy/`, never find the ones 2.15 writes, and say
nothing about it: every review gate just stays open. Upgrade both plugins together
(`claude plugin marketplace update`, then restart the session). The other skew is safe:
sdlc 4.21 still reads receipts an older grumpy left under `.claude/grumpy/`.

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
