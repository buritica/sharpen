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
`.sharpen/grumpy/bench/<case>/<pass>.md`, and only there. If the pass printed its report but
couldn't save it, the printed report is scored and the run is marked `scored-from-output`. The report, the host's output and
`results.json` land in `.sharpen/bench/<timestamp>-<host>-<model>/` (`default` with no
`--model`, `/` in a model id becomes `_`, and a second run in the same second gets a
`-2` suffix), which is gitignored; `--out` picks another root and `--keep` leaves each
temp repo behind for inspection. `results.json` is rewritten after every run, and
records the host, model, CLI version, the exact command, and hashes of the pass's prompt
files and of each case key, so two runs can be told apart later. Each run's entry says
how every finding was classified (`assigned`).

Every run gets one `status`: `scored`, `scored-from-output`, or a failure: `no-report`,
`unparsed` (no severity headings, or content the parser couldn't read), `timeout`
(`--timeout`, default 1200 seconds per run; the host's whole process group is killed),
`exit-nonzero`, `cli-missing`, `error`, or `no-saved-report` after a `rescore`. `run`
exits 0 when every run scored, 1 when any failed, was interrupted or nothing ran, and 2
when the host CLI is missing or the invocation is invalid (checked, with every key,
before the first paid session).

`--repeat` (default 3) runs each case several times; models aren't deterministic, and the
spread across repeats is reported next to the mean. `compare` exits 1 when the candidate,
against the baseline:

- loses recall (`--recall-drop`, default 0);
- gains blocking noise or overrated findings (`--noise-rise`, default 0);
- gains duplicate findings by more than one per run (`--dup-rise`);
- drops precision by more than 10 points (`--precision-drop`, default 0.1);
- has a higher share of failed runs or of runs that re-raised a settled finding;
- or is missing a case/pass the baseline has, or has no runs at all.

A run fails when it wrote no report, timed out (even with a half-written report), raised
an error, or produced a report with none of the severity headings the scorer reads.
Runs scored from printed output are counted in `from_output`; a rise is printed as a
note, not a regression, because whether a host may save under `.claude/` depends on its
permission setup as much as on the prompt.

## Hosts

| Host | How the pass is invoked | Flags verified |
| --- | --- | --- |
| Claude Code | `claude -p --plugin-dir plugins/grumpy /grumpy:review`, so the working tree's prompts run, not the installed plugin | yes |
| Codex | `codex exec --sandbox workspace-write --skip-git-repo-check -`, with the pass's `SKILL.md` sent on stdin | match OpenAI's docs, no live run yet |
| pi | `pi --print --no-session`, with the pass's `SKILL.md` sent on stdin | match pi's docs, no live run yet |

Codex and pi get the skill body inline because skill discovery differs per host and
version, and inlining makes the run independent of what is installed. `--invoke inline`
does the same on Claude Code. `--cmd` replaces the whole command line (`{model}`,
`{grumpy}`, `{claude_tools}` and `{prompt}` are substituted; `{model}` must be its own
token after its flag, so both drop when no model is given), so a wrong flag can be fixed
without a code change. Claude Code runs get `acceptEdits` plus an allowlist of what a
review or imagine pass needs (git, python3, read-only file tools, file writes scoped to
the report folder with `Edit(.sharpen/grumpy/**)`, Agent), not a permission bypass; Codex runs in its
`workspace-write` sandbox inside the temp repo.

## Cost

Seven cases, five for review and two for imagine. `run --pass all` with the default
three repeats is 21 headless sessions per host and model. `--cases` and `--repeat 1` give a
quick check.

Before you run it:

- **Inline runs read the generated SKILL.md.** Codex, pi and `--invoke inline` send
  `skills/<pass>/SKILL.md`, not `commands/<pass>.md`. After editing a command, run
  `python3 scripts/generate-skill.py --write-all-in plugins/grumpy` first or you bench
  the old prompt.
- **An installed grumpy can shadow the working tree.** `claude -p --plugin-dir` adds the
  working tree's grumpy but doesn't unload an installed one, and your installed hooks
  and settings run in the bench session too. Bench where grumpy isn't installed, or
  compare only runs made on the same machine with the same installs.
- `--invoke native` exists only for Claude Code; Codex and pi always run inline.
- **On Claude Code, `--model` sets the session, not review's passes.** Review
  dispatches its passes on `models.yaml`'s `analysis` role. To compare models for
  review, pin it: `run --host claude --pass review --analysis sonnet`, then
  `--analysis opus`. The run records the pin as `analysis` in `results.json`. Imagine
  runs on the session's model, so `--model` is what changes it.
- The fixture code is excluded from ruff (`pyproject.toml`) and from `/grumpy:simplify`
  (`.sharpen/simplify.json`), because it plants bugs on purpose. Check a new case by
  running the unit tests, which validate every key against its files.

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
     "lines": [18, 19], "keywords": ["swallow", "silent", "bare except"]}
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
  case's `.sharpen/grumpy/<branch>/` folder before the pass runs.
- **keywords**: a finding matches when it names the file, cites a line within 3 of the
  range (or no line at all), and contains one keyword at a word start, ignoring case
  (`log` matches "logging" and `factory` matches "FormatterFactory", but `log` doesn't
  match "catalog"). A finding that cites the file without a line matches on keyword
  alone, so pick words a reviewer would use for this issue and not for its neighbours:
  avoid short stems like `pass` or `except` that also mean something else. A keyword
  that starts with punctuation, such as `` `pass` ``, matches anywhere.
- **passes**: optional, defaults to both review and imagine. Set it, or a review-only
  case also runs, and scores every planted issue as a miss, under imagine.

`load_key` refuses a key with duplicate ids, an unknown severity or pass, or an
`expect_absent` id that names no planted issue, so a typo can't silently skew a score.

The scorer reads only the severity sections of a rendered report: Critical Issues or
Bugs Found (CRIT), Serious Concerns (WARN), Questionable Decisions and Simplify This
(NOTE). Both passes aggregate every finding into those sections, so the detail sections
would double count.

The unit tests check every key against its fixture: planted ranges touch changed lines,
an ideal report scores 100% recall with no noise, and ids and basenames are unique.
