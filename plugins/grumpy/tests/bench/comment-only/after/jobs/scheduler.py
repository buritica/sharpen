"""Run background jobs on a fixed interval."""

import time


def next_run(last_run_ms, interval_ms):
    """Return when the job should run next.

    Both arguments, and the result, are epoch milliseconds.
    """
    return last_run_ms + interval_ms


def due(job, now_ms=None):
    # A job is due once the wall clock reaches its next scheduled run.
    # `now_ms` is injectable so tests don't depend on the real clock.
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return now_ms >= next_run(job["last_run_ms"], job["interval_ms"])
