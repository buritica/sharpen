# grumpy review benchmark

Fixture repos for `scripts/bench-grumpy.py`, which runs grumpy's review and imagine
passes headlessly on a host, scores the report each pass writes, and compares runs. It
answers five questions the old single-fixture recall eval couldn't: does a prompt or
model change lose real findings, does it add noise, does a review on Codex or pi match
one on Claude Code, does the no-agent path (`oneshot.py` against a chat API) match
any of them, and what does each of them cost in tokens, dollars and time.

Local only. Nothing here runs in CI except the scorer's unit tests
(`scripts/tests/test_bench_grumpy.py`), which call no model.

## Running it

```sh
python3 scripts/bench-grumpy.py cases                       # list fixtures
python3 scripts/bench-grumpy.py commands --host codex       # print the exact command lines
python3 scripts/bench-grumpy.py run --host claude --model claude-opus-5-5 --pass all
# an eval: every model with every mode, same cases, one results.json
python3 scripts/bench-grumpy.py run --host claude --model sonnet,opus --mode inline,fanout \
    --cases replay-declared-commands,replay-oneshot-imagine
python3 scripts/bench-grumpy.py run --host codex --cases billing-planted --repeat 1
python3 scripts/bench-grumpy.py run --host oneshot --provider openai          # OPENAI_API_KEY set
python3 scripts/bench-grumpy.py run --host oneshot --provider compatible --model qwen3-coder  # SHARPEN_LLM_URL set
python3 scripts/bench-grumpy.py compare .sharpen/bench/<before>/results.json .sharpen/bench/<after>/results.json
# after changing the scorer or a key: re-score saved reports without a paid rerun
python3 scripts/bench-grumpy.py rescore .sharpen/bench/<run>/results.json
python3 scripts/bench-grumpy.py score path/to/review.md --case billing-planted
```

Each run copies a case into a temp git repo (`before/` committed on `main`, `after/`
committed on `bench/<case>`), runs the pass there, and reads
`.sharpen/grumpy/bench/<case>/<pass>.md`, and only there. If the pass printed its report but
couldn't save it, the printed report is scored and the run is marked `scored-from-output`. The report, the host's output and
`results.json` land in `.sharpen/bench/<timestamp>-<host>-<model>/` (`<host>-<provider>` on `oneshot`, `default`
with no `--model`, `/` in a model id becomes `_`, several models are joined with `+` and a
pinned mode is appended, and a second run in the same second gets a
`-2` suffix), which is gitignored; `--out` picks another root and `--keep` leaves each
temp repo behind for inspection. `results.json` is rewritten after every run, and
records the host, the `--model` value as given (a comma-separated string in an eval),
`mode`, `variants` (each `model/mode` an eval ran), `analysis`, the CLI version, the exact
command per pass (keyed `<pass> @model/mode` in an eval), and hashes of the pass's prompt
files and of each case key, so two runs can be told apart later. Each run's entry says
how every finding was classified (`assigned`), what it spent (`usage`), how its passes
ran (`observed_mode`), the `analysis` alias it pinned and the `pinned_mode` it asked for,
if any.

## Cost, time and how the passes ran

Every run records what it spent, as far as the host says. Claude Code runs with
`--output-format json`, so a run's `usage` has its cost in dollars (list price), its
tokens (input, cache reads, cache writes and output, also split per model, so the session
and review's opus subagents show apart), its turns and how many subagents it started.
`oneshot` records the total tokens each pass's call reports (read from its `pass <name>:
done` lines only, so a quoted provider error is never counted), and no cost. Its
aggregation call is not counted yet: `oneshot.py` logs no usage for it, so oneshot token
totals run low by that one call. A run where only some calls reported tokens is marked
`partial` and noted under the table. Codex and pi print no usage in the mode the bench
runs them, so their runs record none. A count Claude Code should have reported and
didn't (no `modelUsage`, a renamed or null key) is recorded as missing, never as 0, and
a Claude or oneshot row where no run recorded usage gets a note. A Claude Code run
that times out records none either: its JSON prints only when the run ends, and the
timeout kills it first (a `oneshot` timeout keeps the tokens it logged). The means leave
such runs out, so the summary counts them as `unpriced` and prints a note under the
table when a row's cost is the mean of only some of its runs. Every run also records
`duration_s`.

`observed_mode` says how the passes actually ran: `inline` (the session ran every pass
itself), `single` (one subagent ran them all, review's default for a diff up to 60,000
characters), `fanout` (it started more than one) or `oneshot`. This matters because headless
Claude Code usually runs the passes inline, while an interactive session usually fans
out, and on a real-size diff fan-out cost 4 to 11 times as much in the runs that
prompted this (2026-11-23). `--mode` asks for one: `inline`, `single` or `fanout` appends a line
saying so to Claude Code's system prompt, and `auto` (the default) leaves it to the
host. The line is a request, not a guarantee, so the summary notes runs whose
`observed_mode` differs from the mode they asked for; their row's label is wrong for
them. On Claude Code `observed_mode` comes from `subagent_stats.spawned` in the JSON
result and nothing else; a run that recorded usage without that key gets a note, and
`modes` counts only runs that scored (a run that died on its first call spawned nothing
either way). Only Claude Code takes a pinned mode; the others have one way to run.

`--model` and `--mode` each take a comma-separated list, and `run` then tries every model
with every mode: an eval in one `results.json`. Each combination summarizes on its own
row (`case/pass @model/mode`), so the table puts recall, noise, cost and time side by
side. The summary prints mean `cost`, `tokens`, `time` and `agents` (subagents) next to
the quality columns.

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
- spends more than 50% more (`--cost-rise`, default 0.5): mean cost where both runs
  recorded one, otherwise mean tokens, but only when both ran on the same host (hosts
  count tokens differently; a cross-host compare says so in a note). When one side
  recorded spend and the other didn't (an older baseline, a candidate whose runs all
  timed out or lost their usage) or the baseline recorded 0, the check can't run, and a
  note says so instead of passing quietly;
- has a higher share of failed runs or of runs that re-raised a settled finding;
- or is missing a case/pass the baseline has, or has no runs at all.

A run that took more than 1.5 times as long, or whose passes ran differently (inline,
single or fanout on one side and another mode on the other; how often each mode ran
doesn't count, so a different `--repeat` alone prints nothing), is printed as a note.

Rows match by name. A plain row (`case/pass`) also matches the other side's
`case/pass @model/mode` row when that is the only row it has for the case/pass, so a
plain baseline compares with a one-variant result, and a note says which rows were
paired. Against an eval with several variants per case/pass nothing is guessed: every
plain row reads as missing, so run the baseline as the same `--model`/`--mode` eval.
A model or mode listed twice runs once. Two variants whose labels fold to the same log
name (`claude-opus` and `claude_opus`, or `Opus` and `opus` on a case-insensitive disk)
are refused before the first run.

Any `--mode` but `auto` is refused together with `--cmd`: the bench can't tell
where in a custom command line the system-prompt flag belongs, so put
`--append-system-prompt` in the template yourself. A `--cmd` for Claude Code needs
`--output-format json` too, or its runs record no usage and no `observed_mode`; and with
several models in `--model` the template must contain `{model}`, or `run` refuses it
rather than paying for identical runs under different labels.

A run fails when it wrote no report, timed out (even with a half-written report), raised
an error, or produced a report with none of the severity headings the scorer reads.
Runs scored from printed output are counted in `from_output`; a rise is printed as a
note, not a regression, because whether a host may save under `.claude/` depends on its
permission setup as much as on the prompt.

## Hosts

| Host | How the pass is invoked | Flags verified |
| --- | --- | --- |
| Claude Code | `claude [--append-system-prompt <mode hint>] -p --plugin-dir plugins/grumpy … --output-format json [--model M] /grumpy:review`, so the working tree's prompts run, not the installed plugin. Its `<label>.log` holds the one JSON result line, with the report in `result` | yes |
| Codex | `codex exec --sandbox workspace-write --skip-git-repo-check -`, with the pass's `SKILL.md` sent on stdin | match OpenAI's docs, no live run yet |
| pi | `pi --print --no-session`, with the pass's `SKILL.md` sent on stdin | match pi's docs, no live run yet |
| oneshot | `python3 plugins/grumpy/scripts/oneshot.py <pass> --provider <p> --model <m>`, this checkout's script against a chat API, no agent | yes (this repo's own script, run end to end against a stub server in the tests); no real-model run recorded yet |

Codex and pi get the skill body inline because skill discovery differs per host and
version, and inlining makes the run independent of what is installed. `--invoke inline`
does the same on Claude Code. `--cmd` replaces the whole command line (`{model}`,
`{grumpy}`, `{claude_tools}` and `{prompt}` are substituted; `{model}` must be its own
token after its flag, so both drop when no model is given), so a wrong flag can be fixed
without a code change. Claude Code runs get `acceptEdits` plus an allowlist of what a
review or imagine pass needs (git, python3, read-only file tools, file writes scoped to
the report folder with `Edit(.sharpen/grumpy/**)`, Agent), not a permission bypass; Codex runs in its
`workspace-write` sandbox inside the temp repo.

`oneshot` is not an agent: the bench runs grumpy's `scripts/oneshot.py` from this checkout in
the temp repo (the host profiles live in `scripts/bench_hosts.py`), which calls the chat API itself (see grumpy's README, "No agent installed").
`--provider` picks the API (default `SHARPEN_LLM_PROVIDER`, else `openai`) and is refused on
the other hosts. `run` stops before the first case when the provider's variable is missing
(`OPENAI_API_KEY` for `openai`, `SHARPEN_LLM_URL` for `compatible`) or `compatible` has no
model. `--model` is the model for every pass; with `--provider openai` and no `--model` it is
`models.yaml`'s `analysis.openai`. `--analysis` pins Claude Code's alias and is refused here.
The recorded `cli_version` is the grumpy version whose `oneshot.py` ran, and the prompt hash
covers `oneshot.py` and `oneshot_prompts.py` too, since the first adds its own preamble and
aggregation wording and the second cuts the per-pass prompts out of the command. A pass that
earns no receipt (an aspect that failed or answered in prose) exits 1 and is a failed
`exit-nonzero` run, not a scored one. Raise `--timeout` for a slow local server with
`--provider compatible`: it runs the calls one at a time, and each can take up to 300 seconds
twice (one retry), so in the worst case an imagine run's six calls need an hour and a
review run's eight need 80 minutes, against the default 1200 seconds; and a run cut off there is recorded as `timeout`, not as a failure of
the path. With `--provider openai` the fixture code goes to
OpenAI; it is planted toy code, but say so if that matters where you run it.

## Cost

Nine cases: five for review, two for imagine, and two real-size replays that run both.
`run --pass all` with the default three repeats is 33 headless sessions per host, model
and mode, so a two-model, two-mode eval is 132. The replays are where the money goes: on
Claude Code a toy case costs about $0.15 per run, a replay $0.30 to $0.50 inline and $1.30
to $5.30 fanned out (single runs, 2026-11-23). `--cases` and `--repeat 1` give a
quick check. On `oneshot` a run is API calls rather than a session: eight per review run
(seven aspects and an aggregation) and six per imagine run (five simulations and an
aggregation), each carrying the diff and the changed files, so `--pass all` with three
repeats is 240 calls.

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
- **On Claude Code, `--model` is the model review's passes run on, in every mode.**
  Inline, the session runs the passes itself, so they run on `--model`. Fanned out,
  review dispatches them on `models.yaml`'s `analysis` role, so when `--model` is a
  Claude Code alias (`haiku`, `sonnet`, `opus`) the bench pins `analysis` to it too, and
  `@sonnet/fanout` means sonnet passes. With a full model id nothing is pinned and
  fanned-out passes follow `models.yaml`. `--analysis` overrides the pin for every run
  (to compare a session model against a fixed pass model, say). Each run records the
  alias it pinned as `analysis`. Imagine runs its passes on the session's model.
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
| `replay-declared-commands` | both | A real sharpen layer (PR 57's first commit, a 16 KB diff): the gate starts running the commands AGENTS.md declares. Planted: the six issues that layer's own review and imagine found and its next two commits fixed (gate.md's root and monorepo handling, silent fallbacks in the parser, a sibling import). |
| `replay-oneshot-imagine` | both | A real sharpen layer (PR 71 as its review saw it, a 120 KB diff): the no-agent path runs imagine. Planted: the ten issues its review and imagine found and its next two commits fixed. |

Both replays' acceptable lists hold real issues later runs found that the layer didn't
fix. The replays are scored against the union of what review and imagine found, so a pass's
recall there is lower than on a toy case by design: compare runs against each other, not
against 100%. They carry only the files the layer changed (and, for the first, the module
its new script imports), minus version bumps, changelogs and generated SKILL.md files,
whose basenames would collide. To add one, copy a merged layer's changed files at its
reviewed commit and its parent into `after/` and `before/`, and plant what the layer's own
later fix commits changed.

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
