# grumpy changelog

Version-specific migration notes. Nothing reads this file programmatically — it exists so a
past release's behavior change stays discoverable without bloating the current README.

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
