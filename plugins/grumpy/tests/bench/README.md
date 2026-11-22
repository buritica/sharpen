# grumpy review benchmark

Fixture repos for `scripts/bench-grumpy.py`, which runs grumpy's review and imagine
passes headlessly on a host, scores the report each pass writes, and compares runs. It
answers three questions the old single-fixture recall eval couldn't: does a prompt or
model change lose real findings, does it add noise, and does a review on Codex or pi
match one on Claude Code.

Local only. Nothing here runs in CI except the scorer's unit tests
(`scripts/tests/test_bench_grumpy.py`), which call no model.

## Running it

```sh
python3 scripts/bench-grumpy.py cases                       # list fixtures
python3 scripts/bench-grumpy.py commands --host codex       # print the exact command lines
python3 scripts/bench-grumpy.py run --host claude --model claude-opus-5-5 --pass all
python3 scripts/bench-grumpy.py run --host codex --cases billing-planted --repeat 1
python3 scripts/bench-grumpy.py compare .sharpen/bench/<before>/results.json .sharpen/bench/<after>/results.json
# after changing the scorer or a key: re-score saved reports without a paid rerun
python3 scripts/bench-grumpy.py rescore .sharpen/bench/<run>/results.json
python3 scripts/bench-grumpy.py score path/to/review.md --case billing-planted
```

Each run copies a case into a temp git repo (`before/` committed on `main`, `after/`
committed on `bench/<case>`), runs the pass there, and reads
`.claude/grumpy/bench/<case>/<pass>.md`. If the pass printed its report but couldn't
save it (headless Claude Code treats `.claude/` as a sensitive path), the printed
report is scored and the run is marked `scored-from-output`. The report, the host's output and
`results.json` land in `.sharpen/bench/<timestamp>-<host>-<model>/`, which is gitignored.
`results.json` records the host, model, CLI version, the exact command and a hash of the
pass's prompt files, so two runs can be told apart later.

`--repeat` (default 3) runs each case several times; models aren't deterministic, and the
spread across repeats is reported next to the mean. `compare` exits 1 when the candidate
loses recall, gains blocking noise, drops precision by more than 10 points, or has runs
that wrote no report. Thresholds are flags.

## Hosts

| Host | How the pass is invoked | Flags verified |
| --- | --- | --- |
| Claude Code | `claude -p --plugin-dir plugins/grumpy /grumpy:review`, so the working tree's prompts run, not the installed plugin | yes |
| Codex | `codex exec --sandbox workspace-write --skip-git-repo-check -`, with the pass's `SKILL.md` sent on stdin | match OpenAI's docs, no live run yet |
| pi | `pi --print --no-session`, with the pass's `SKILL.md` sent on stdin | match pi's docs, no live run yet |

Codex and pi get the skill body inline because skill discovery differs per host and
version, and inlining makes the run independent of what is installed. `--invoke inline`
does the same on Claude Code. `--cmd` replaces the whole command line (`{model}`,
`{grumpy}` and `{prompt}` are substituted), so a wrong flag can be fixed without a code
change. Claude Code runs get `acceptEdits` plus an allowlist of the tools a pass needs
(git, python3, file tools, Agent), not a permission bypass; Codex runs in its
`workspace-write` sandbox inside the temp repo.

## Cost

Six cases, four for review and two for imagine. `run --pass all` with the default three
repeats is 18 headless sessions per host and model. `--cases` and `--repeat 1` give a
quick check.

## Cases

| Case | Pass | What it measures |
| --- | --- | --- |
| `billing-planted` | review | Seven planted defects (swallowed exception, silent `None`, undefined name, pointless factory, stale comment, unhandled refund, fake test). The old eval's fixture with line ranges. |
| `billing-settled` | review | `billing-planted` plus a `/grumpy:fix` report whose `## Settled` list holds one valid entry (the fake test) and two invalid ones (no reason; a whole file). The fake test must stay out of the report and the other five must stay in. Replaces the old eval's `--expect-absent` check. The silent `None` return is acceptable here, not planted: the fix report's `## Fixed` entry claims the swallowed error is re-raised, which would fix both, so a review rightly merges them into one finding (every run did, while `billing-planted` scores it separately 6 of 6). |
| `clean-refactor` | review | A behavior-preserving rename. Any CRIT or WARN is noise. |
| `comment-only` | review | A docstring and comment fix with no executable change, the commit type issue #37 paid full rounds for. Any CRIT or WARN is noise. |
| `capability-bait` | review | A correct two-line fix with tests, in code with no logging or transactions. Missing capabilities are fine as NOTEs and noise as CRIT/WARN. |
| `imagine-concurrency` | imagine | A parallel bulk refresh: rate limit exceeded, retries with no backoff, a counter race, failures that vanish. |
| `imagine-first-deploy` | imagine | A NOT NULL column with no default, no down migration, code that breaks if it deploys before the migration, a crash on a missing account. |

## Writing a case

A case is a directory with `before/`, `after/` and `key.json`. Line numbers in the key
refer to files in `after/`. File basenames must be unique within a case.

```json
{
  "passes": ["review"],
  "description": "One sentence: what the change is and what a good review says.",
  "planted": [
    {"id": "swallowed-exception", "severity": "WARN", "file": "src/billing/charge.py",
     "lines": [18, 19], "keywords": ["swallow", "silent", "pass"]}
  ],
  "acceptable": [
    {"id": "no-backoff", "file": "src/billing/charge.py", "lines": [15, 21],
     "keywords": ["backoff", "sleep"], "max_severity": "NOTE"}
  ]
}
```

- **planted**: what a good pass must find. `severity` is the lowest rating that counts
  as right; a lower one is reported as `underrated`.
- **acceptable**: real issues that aren't planted, so finding them is not noise. With
  `max_severity`, a finding rated above it counts as `overrated` and against precision.
- **expect_absent**: planted ids the pass must hold back, such as one an earlier fix
  settled. They are left out of recall, and a finding that raises one anyway makes the
  run unclean and counts against precision; `compare` flags a rise in such runs.
- **artifacts**: `{"fix.md": "artifacts/fix.md"}` copies earlier pass output into the
  case's `.claude/grumpy/<branch>/` folder before the pass runs.
- **keywords**: a finding matches when it names the file, cites a line within 3 of the
  range (or no line at all), and contains one keyword. Pick words a reviewer would use
  for this issue and not for its neighbours.

The scorer reads only the severity sections of a rendered report: Critical Issues or
Bugs Found (CRIT), Serious Concerns (WARN), Questionable Decisions and Simplify This
(NOTE). Both passes aggregate every finding into those sections, so the detail sections
would double count.

The unit tests check every key against its fixture: planted ranges touch changed lines,
an ideal report scores 100% recall with no noise, and ids and basenames are unique.
