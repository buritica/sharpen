# grumpy test fixtures

- `simplify-config.example.json`: the example simplify policy that
  `test_simplify_policy.py` loads.
- `fix-scope/`: a manual check for `/grumpy:fix`'s fix-versus-new-capability
  sorting (below).

Review and imagine quality, including the settled-findings check that used to
live here, is measured by the benchmark in `../bench/` (`scripts/bench-grumpy.py`;
see `../bench/README.md`).

## Fix or new capability (`fix-scope/`)

`fix-scope/` holds a plan, a diff and a review with four findings.
`expected.json` gives the sorting `/grumpy:fix` should produce: the Critical
lock finding is asked about, never sorted on its own; the ordering bug and the
test gap are fixes; the event-and-red-exit finding is new capability, recorded
under `## New capability` and `## Settled` with no code marker. Run fix on a
scratch repo: the fixture ships no pre-images, so build them from the diff's
context lines (and write a small `src/notes/ingest.py` with the unlocked write
the review describes), commit them on main, apply `sample.diff` on a branch and commit it, so `$SCOPE` holds only the
diff's files and `ingest.py` is outside it; put the plan at
`.claude/sdlc/<branch>/plan.md` and the review at
`.claude/grumpy/<branch>/review.md`, and compare by hand. `ask` is the attended
answer; an unattended run fixes the lock here and lists it under Open
questions. It has no scorer: the benchmark scores review and imagine reports, and fix's
sorting output is a different shape. Whether a review raises missing capabilities as
blockers in the first place is what the benchmark's `capability-bait` case measures.
