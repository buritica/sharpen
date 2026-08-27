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
2. Normalize each agent's findings into a JSON list of
   `{"severity": "...", "file": "...", "text": "..."}` objects.
3. Score each with:
   ```sh
   python3 scripts/eval-grumpy-findings.py <candidates.json> --label "<name>"
   ```
4. Compare `recall` (must stay at 100% — a dropped planted issue is a
   regression) and `output size` (the metric this change is meant to
   improve).

`scripts/tests/test_eval_grumpy_findings.py` covers the scorer's matching
logic itself with fixed inputs — it doesn't call an LLM.
