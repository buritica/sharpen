# grumpy findings-format eval fixture

`sample.diff` + `golden.json` are used to check that the compact
pipe-delimited findings contract (see `review.md`/`imagine.md`) doesn't
lose recall relative to the old verbose per-agent markdown format.

`sample.diff` plants 7 issues spanning severities and aspects (a swallowed
exception, a silent `None` return, an undefined-name bug, an unnecessary
factory abstraction, a stale/misleading comment, a missing error handler,
and a fake test assertion). `golden.json` lists each with the keywords a
scorer should find in a real finding's text.

## Running the eval

There's no automated LLM call in CI (this repo is stdlib-only and LLM
output is non-deterministic) — this is a manual check to run whenever
`review.md`/`imagine.md`'s agent prompts change:

1. Spawn one agent per prompt variant against `sample.diff`, capture its raw
   output.
2. Score the new (pipe-format) variant's raw output directly — save it to a
   file and run:
   ```sh
   python3 scripts/eval-grumpy-findings.py <raw-output.txt> --label "<name>"
   ```
   `.txt`/non-`.json` files are parsed as `SEVERITY|file:line|text|FACT|DOMAIN`
   lines (`--format pipe` to force it); CONTEXT/HANDLED/malformed lines are
   skipped automatically, same as the real aggregator is instructed to do.
   For the old verbose-markdown variant (or any format eval-grumpy-findings.py
   doesn't parse), normalize by hand into a JSON list of
   `{"severity": "...", "file": "...", "text": "..."}` objects instead and
   score that (`.json` files are auto-detected).
3. Compare `recall` (must stay at 100% — a dropped planted issue is a
   regression) and `output size` (the metric this change is meant to
   improve).

`scripts/tests/test_eval_grumpy_findings.py` covers the scorer's matching and
pipe-line-parsing logic with fixed inputs — it doesn't call an LLM.

## Settled findings (`settled/`)

`settled/fix.md` is a fix report whose `## Settled` list holds three entries
against `sample.diff`: one valid entry for the planted `fake-test-assertion`,
one entry with no reason (for `unnecessary-factory`), and one that names a
whole file (for `stale-misleading-comment`). Only the first may hold anything
back. Run `/grumpy:review` (or imagine) on `sample.diff` in a scratch repo with
this file at `.claude/grumpy/<branch>/fix.md`, save the findings the rendered
report still lists as pipe lines, and score them:

```sh
python3 scripts/eval-grumpy-findings.py <rendered.txt> --expect-absent fake-test-assertion
```

Pass when recall is 100% over the other six, `fake-test-assertion` is absent,
and the report says `Settled findings held back: 1` and lists it. Also score
the sub-agents' raw output the usual way: the sub-agent prompts don't read the
settled list, so their recall must stay 100% on all seven.

## Fix or new capability (`fix-scope/`)

`fix-scope/` holds a plan, a diff and a review with four findings.
`expected.json` gives the sorting `/grumpy:fix` should produce: the Critical
lock finding is asked about, never sorted on its own; the ordering bug and the
test gap are fixes; the event-and-red-exit finding is new capability, recorded
under `## New capability` and `## Settled` with no code marker. Run fix on a
scratch repo holding these files (plan at `.claude/sdlc/<branch>/plan.md`,
review at `.claude/grumpy/<branch>/review.md`) and compare by hand. This one has
no scorer yet; area 6's benchmark is where a capability-bait scorer belongs.
