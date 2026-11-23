#!/usr/bin/env python3
"""Summaries and comparisons for scripts/bench-grumpy.py.

`summarize` folds a run's results into one row per case/pass (and per
model/mode variant in a matrix run); `compare` reads two of those and names
the regressions. Running and scoring stay in bench-grumpy.py. Pure stdlib.
"""

import collections
import statistics


def _mean(values):
    """statistics.mean, or None when there is nothing to average."""
    values = list(values)
    return statistics.mean(values) if values else None


def summarize(runs):
    """Aggregate per row over repeats: one row per (case, pass), and per
    model/mode variant in a matrix run. Runs without a score (no report, a
    timeout, an unparseable report, an error) count as failures.

    `.get` defaults read results.json files from before a key existed, so an
    older baseline still compares."""
    groups = {}
    for r in runs:
        groups.setdefault(group_name(r), []).append(r)
    out = {}
    for name, rs in sorted(groups.items()):
        scored = [r["score"] for r in rs if r.get("score")]
        # How the passes ran, from runs that finished: a fanout-pinned run that
        # died on its first call spawned nothing and would read as inline.
        finished = [r for r in rs if r.get("score")]
        out[name] = {
            "runs": len(rs),
            "failed_runs": len(rs) - len(scored),
            **_score_stats(scored),
            "from_output": sum(
                1 for r in rs if r.get("status") == "scored-from-output"
            ),
            "statuses": _counts(r.get("status", "?") for r in rs),
            "duration_s": _mean(r["duration_s"] for r in rs if r.get("duration_s")),
            # ponytail: timed-out runs record no usage and drop out of these means, biasing slow fanout low; estimate their spend when an eval row shows unpriced > 0
            "cost_usd": _usage_mean(rs, "cost_usd"),
            "tokens": _usage_mean(rs, "tokens"),
            "subagents": _usage_mean(rs, "subagents"),
            "unpriced": sum(1 for r in rs if not _has_usage(r)),
            "partial_usage": sum(
                1 for r in rs if (r.get("usage") or {}).get("partial")
            ),
            "modes": _counts(
                r["observed_mode"] for r in finished if r.get("observed_mode")
            ),
            "mode_unknown": sum(
                1 for r in finished if r.get("usage") and not r.get("observed_mode")
            ),
            "mode_missed": sum(
                1
                for r in finished
                if r.get("pinned_mode")
                and r.get("observed_mode")
                and r["observed_mode"] != r["pinned_mode"]
            ),
            "clean_rate": _mean(1.0 if s["clean"] else 0.0 for s in scored),
            "held_back_failures": sum(1 for s in scored if s.get("present")),
        }
    return out


def _score_stats(scored):
    """The quality half of a summary row, from the runs that scored."""
    recalls = [s["recall"] for s in scored if s["recall"] is not None]
    precisions = [s["precision"] for s in scored if s["precision"] is not None]
    return {
        "recall": _mean(recalls),
        "recall_spread": (max(recalls) - min(recalls)) if len(recalls) > 1 else 0.0,
        "precision": _mean(precisions),
        "noise_blocking": _mean(s["noise_blocking"] for s in scored),
        "overrated": _mean(len(s["overrated"]) for s in scored),
        "underrated": _mean(len(s.get("underrated", [])) for s in scored),
        "unlocated": _mean(s.get("unlocated", 0) for s in scored),
        "duplicates": _mean(s.get("duplicates", 0) for s in scored),
        "findings": _mean(s["findings"] for s in scored),
    }


# ponytail: failed runs' spend is averaged in, so a run cut short by a usage cap reads cheap; average scored runs only when an eval row with failed_runs > 0 shows a lower cost than its clean repeats
def _usage_mean(rs, field):
    """Mean of one usage field over the runs that recorded it."""
    values = [(r.get("usage") or {}).get(field) for r in rs]
    return _mean(v for v in values if v is not None)


def _has_usage(run):
    """Whether a run recorded a cost or a token count. A Claude run that
    times out records neither: its JSON prints only when the run ends."""
    usage = run.get("usage") or {}
    return usage.get("cost_usd") is not None or usage.get("tokens") is not None


def _counts(values):
    return dict(sorted(collections.Counter(values).items()))


def group_name(run):
    """`case/pass`, plus `@model/mode` for a run from a matrix (several
    models or modes in one `run`), so its variants summarize apart."""
    name = f"{run['case']}/{run['pass']}"
    if run.get("variant"):
        name += f" @{run['variant']}"
    return name


def _rate(s, key):
    return s.get(key, 0) / s["runs"] if s.get("runs") else 0.0


def compare(
    baseline,
    candidate,
    recall_drop=0.0,
    noise_rise=0.0,
    precision_drop=0.1,
    dup_rise=1.0,
    cost_rise=0.5,
):
    """Regressions of candidate vs baseline summaries.

    Returns (problems, notes); an empty problems list means no regression.
    A case/pass the baseline has and the candidate lacks is a problem: a
    narrowed run must not read as a clean one. Counts of failed runs are
    compared as rates, so runs with different --repeat compare fairly. Mean
    cost (or, where a host reports no cost, tokens) rising by more than
    `cost_rise` (a fraction: 0.5 is +50%) is a problem; wall time and a
    change in how the passes ran (inline or fanned out) are notes.
    """
    limits = {
        "recall": recall_drop,
        "noise": noise_rise,
        "precision": precision_drop,
        "duplicates": dup_rise,
    }
    problems, notes = [], []
    b, c = baseline.get("summary", {}), candidate.get("summary", {})
    if not c:
        problems.append("candidate has no runs")
    if candidate.get("interrupted"):
        problems.append("candidate run was interrupted")
    if baseline.get("key_hashes", {}) != candidate.get("key_hashes", {}):
        notes.append("case keys differ between runs; rescore both with the same keys")
    # Hosts count tokens differently (Claude adds cache reads, oneshot takes
    # the API's total), so tokens compare only within one host.
    same_host = baseline.get("host") == candidate.get("host")
    if not same_host:
        notes.append(
            f"hosts differ ({baseline.get('host')} vs {candidate.get('host')}):"
            " they count tokens differently, so only cost is compared"
        )
    b, c, matched = _match_variant_rows(b, c)
    notes += [f"{bn}: compared with candidate's {cn}" for bn, cn in matched]
    for name in sorted(set(b) | set(c)):
        if name not in c:
            problems.append(f"{name}: in baseline, not in candidate")
            continue
        if name not in b:
            notes.append(f"{name}: new in candidate")
            continue
        bs, cs = b[name], c[name]
        if _rate(cs, "failed_runs") > _rate(bs, "failed_runs"):
            problems.append(
                f"{name}: {cs['failed_runs']}/{cs['runs']} run(s) failed"
                f" (baseline {bs['failed_runs']}/{bs['runs']}; statuses {cs.get('statuses', {})})"
            )
        # A note, not a problem: whether a host may save the report depends
        # on its permission setup as much as on the prompt.
        if _rate(cs, "from_output") > _rate(bs, "from_output"):
            notes.append(
                f"{name}: {cs['from_output']}/{cs['runs']} run(s) printed the report"
                " without saving it"
            )
        if _rate(cs, "held_back_failures") > _rate(bs, "held_back_failures"):
            problems.append(
                f"{name}: {cs['held_back_failures']}/{cs['runs']} run(s) re-raised a settled finding"
                f" (baseline {bs.get('held_back_failures', 0)}/{bs['runs']})"
            )
        problems += [f"{name}: {p}" for p in _quality_problems(bs, cs, limits)]
        spend, why_not = _spend_change(bs, cs, cost_rise, same_host)
        if spend:
            problems.append(f"{name}: {spend}")
        if why_not:
            notes.append(f"{name}: {why_not}")
        notes += [f"{name}: {n}" for n in _run_notes(bs, cs)]
    return problems, notes


# ponytail: pairs whatever single variant finished, even on another model; check the variant's model against the plain side's when a pairing note names a model the baseline didn't run
def _match_variant_rows(b, c):
    """Pair a plain `case/pass` row on one side with the other side's
    `case/pass @model/mode` row when that is the only row it has for the
    case/pass, so a plain baseline compares with a one-variant candidate (and
    the other way round). Returns the summaries keyed so paired rows share
    the plain name, and the (baseline name, candidate name) pairs."""

    def by_case(summary):
        out = {}
        for name in summary:
            out.setdefault(name.split(" @", 1)[0], []).append(name)
        return out

    b, c = dict(b), dict(c)
    bc, cc = by_case(b), by_case(c)
    matched = []
    for plain in sorted(set(bc) & set(cc)):
        bn, cn = bc[plain], cc[plain]
        if len(bn) != 1 or len(cn) != 1 or bn == cn:
            continue
        if bn[0] == plain:
            c[plain] = c.pop(cn[0])
        elif cn[0] == plain:
            b[plain] = b.pop(bn[0])
        else:
            continue  # two different variants: not the same run
        matched.append((bn[0], cn[0]))
    return b, c, matched


# (field, label, format, limit key, worse when lower). The order is the
# order compare reports them in.
_QUALITY = (
    ("overrated", "overrated findings", "{:.1f}", "noise", False),
    ("underrated", "underrated planted issues", "{:.1f}", "noise", False),
    ("duplicates", "duplicate findings", "{:.1f}", "duplicates", False),
    ("recall", "recall", "{:.0%}", "recall", True),
    ("noise_blocking", "blocking noise", "{:.1f}", "noise", False),
    ("precision", "precision", "{:.0%}", "precision", True),
)


def _quality_problems(bs, cs, limits):
    """'recall 80% -> 60%' and the like, for each quality field the
    candidate got worse on by more than its limit."""
    out = []
    for field, label, fmt, limit_key, lower_is_worse in _QUALITY:
        bv, cv = bs.get(field), cs.get(field)
        if bv is None or cv is None:
            continue
        limit = limits[limit_key]
        worse = cv < bv - limit if lower_is_worse else cv > bv + limit
        if worse:
            out.append(f"{label} {fmt.format(bv)} -> {fmt.format(cv)}")
    return out


def _run_notes(bs, cs):
    """Notes on how the runs went, not on what they found: wall time and
    whether the passes ran inline or fanned out."""
    notes = []
    bt, ct = bs.get("duration_s"), cs.get("duration_s")
    if bt and ct and ct > bt * 1.5:
        notes.append(f"wall time {bt:.0f}s -> {ct:.0f}s")
    # Which modes ran, not how often: counts differ whenever --repeat does.
    if bs.get("modes") and cs.get("modes") and set(bs["modes"]) != set(cs["modes"]):
        notes.append(f"passes ran {_modes(bs)} in baseline, {_modes(cs)} in candidate")
    return notes


def _modes(s):
    return ", ".join(f"{m} x{n}" for m, n in s["modes"].items())


def _spend_change(bs, cs, limit, same_host=True):
    """(problem, note). The problem is 'cost $0.40 -> $1.10' when the
    candidate spends more than `limit` above the baseline: cost where both
    have it, else tokens (same host only). The note says why the check
    couldn't run when one side recorded spend and the other didn't, or the
    baseline recorded 0, so a skipped check never reads as a passed one.
    Both are None when neither side recorded any (Codex, pi)."""
    fields = (("cost_usd", "${:.2f}"), ("tokens", "{:,.0f}"))
    why_not = None
    for field, fmt in fields if same_host else fields[:1]:
        bv, cv = bs.get(field), cs.get(field)
        label = "cost" if field == "cost_usd" else "tokens"
        if bv is None and cv is None:
            continue
        if bv is None or cv is None:
            side = "baseline" if bv is None else "candidate"
            why_not = why_not or f"{label} not compared: the {side} recorded none"
            continue
        if not bv:
            why_not = why_not or f"{label} not compared: the baseline recorded 0"
            continue
        if cv > bv * (1 + limit):
            return f"{label} {fmt.format(bv)} -> {fmt.format(cv)}", None
        return None, None
    return None, why_not
