# Confirm-outcome pattern

The T+window loop confirms a merged change actually solved what it claimed to solve. It is the complement to T+0 deploy verification: deploy success proves the change landed; the confirmation window proves it worked.

## Why this exists

A fix can deploy cleanly, pass smoketests, and still under-deliver vs. baseline a week later. Without an explicit confirmation window, "merged" quietly becomes "done" — before anyone checks whether the metric moved, the error class disappeared, or the latency improved. The confirmation window forces that check to happen.

## Tier → window

| Tier | Window |
|------|--------|
| Docs-only | skip |
| Tiny | skip (opt in by adding the `## Confirmation` block) |
| Small-medium | 24h |
| Significant | 4–7d |

The window starts at merge, not at deploy. If deploy is delayed, the agent notes it and restarts the clock from first traffic.

## PR body contract

Every Small-medium+ PR body includes a `## Confirmation` block:

```markdown
## Confirmation
Window: 24h
Checks:
- <metric or query that shows the change worked in prod>
- <log query proving no regression class introduced>
- <ratio / delta vs pre-merge baseline>
```

**Rules:**
- Missing block on Small-medium+ → warn at ship time (same discipline as `## Verification`).
- Empty block ≠ passed check. Name at least one observable.
- `Window:` accepts an explicit override (e.g. `Window: 4d`) when the default doesn't fit.

## The T+window loop

At T+window, a scheduled agent or job:

1. Reads the `## Confirmation` block from the merged PR body.
2. Runs each check against the live system, comparing to the pre-merge baseline.
3. Posts a structured comment back to the PR thread:

```
## Outcome (T+24h)
Status: pass | fail | inconclusive
---
- ✅ <check> — <result vs baseline>
- ❌ <check> — <result vs baseline>
- ⚠️  <check> — insufficient data
```

`inconclusive` = not enough data yet (low traffic, short window). It is not a pass. Schedule a follow-up check or extend the window.

## Wiring it in your repo

The pattern is scheduler- and metrics-reader-agnostic. You need three pieces:

### 1. A scheduler

Triggers the outcome check at T+window after merge. Options:
- GitHub Actions `on: schedule` with a workflow that reads recently merged PRs.
- A cron job or an internal scheduler that watches merge events.
- A standing agent that polls `gh pr list --state merged --limit N`.

Whichever you pick, **it has to outlive the session that merged the PR.** An
in-process timer dies with the agent, and the confirmation then reports as
silence rather than as a failure — the one outcome indistinguishable from
"nothing went wrong."

### 2. A metrics reader

Reads the named checks from the `## Confirmation` block and queries your telemetry:
- SQL / analytics DB query returning a scalar or time-series delta.
- Log query (Datadog, Loki, CloudWatch) returning error count or rate.
- Endpoint probe returning latency or status.

The metrics reader receives the raw check strings from the PR body and produces `pass | fail | inconclusive` per check.

### 3. A reporter

Posts the structured outcome comment back to the PR via `gh pr comment <n> --body "..."` and optionally notifies a Slack channel.

Exit codes: `0` on pass or inconclusive (needs follow-up, not a hard failure), `1` on fail.

## What this pattern does NOT do

- It does not define what "baseline" means — that's repo-specific (p95 latency before this PR, error rate over the prior week, etc.).
- It does not pick the scheduler — any mechanism that fires at T+window works.
- It does not enforce the check results — a `fail` outcome is surfaced to the PR thread; what to do about it (rollback, hotfix, accept) is a human decision.

The discipline is in naming the checks before merge, not in automating the remediation.

## Relationship to deploy verification (T+0)

| | Deploy verification | Outcome confirmation |
|---|---|---|
| When | Immediately after deploy | T+window after merge |
| Question | Did the change land and start correctly? | Did the change work? |
| Checks | Health endpoints, error rate spike, daemon start | Metric delta, regression absence, ratio vs baseline |
| Skill | `/sdlc:ship` post-merge loop | This pattern + consumer repo scheduler |

Both loops run. T+0 catches deploy failures; T+window catches outcome failures. A change that passes T+0 and fails T+window deployed correctly but didn't solve the problem — which is a different kind of failure and needs a different response.
