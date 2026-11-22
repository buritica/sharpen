"""Run background jobs on a fixed interval."""

import time


def next_run(last_run_ms, interval_ms):
    """Return when the job should run next.

    Both arguments are in seconds.
    """
    return last_run_ms + interval_ms


def due(job, now_ms=None):
    # check if due
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return now_ms >= next_run(job["last_run_ms"], job["interval_ms"])
