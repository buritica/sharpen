#!/usr/bin/env python3
"""Score a grumpy findings run against the golden fixture.

Usage:
    python3 scripts/eval-grumpy-findings.py <candidate> [--golden path] [--label name] [--format json|pipe] [--expect-absent ID ...]

<candidate> is either:
- a JSON list of findings (default, `.json` files auto-detected), each shaped
  like {"severity": "CRIT", "file": "src/billing/charge.py", "text": "..."}
- raw pipe-delimited agent output (`--format pipe`, or any non-`.json`
  file), one finding per line, in the exact contract review.md/imagine.md's
  agent prompts specify: SEVERITY|file:line|text|FACT|DOMAIN. Lines that
  aren't FINDING lines (CONTEXT/HANDLED, blank, malformed) are skipped, same
  as the real aggregator is instructed to do -- the "raw lines seen" count
  in the output lets you tell a genuinely quiet run apart from every line
  failing to parse (e.g. a prompt regression dropping a field).

Used to compare recall (did the run still catch the planted issues?) and
output size (a token-cost proxy) between two prompt variants on the same
fixture diff -- see plugins/grumpy/tests/fixtures/README.md. Passing raw
pipe-format output directly (instead of hand-normalizing it to JSON) also
exercises the actual line-parsing contract the format change introduced.

`--expect-absent ID` (repeatable) names a planted finding the run must NOT
report, such as one a `## Settled` entry should hold back: recall is then
scored over the other findings, and the exit code is 1 if any expected-absent
finding still shows up.
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_GOLDEN = os.path.join(
    ROOT, "plugins", "grumpy", "tests", "fixtures", "golden.json"
)
VALID_SEVERITIES = {"CRIT", "WARN", "NOTE"}


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_pipe_line(line):
    """Parse one SEVERITY|file:line|text|FACT|DOMAIN finding line.

    Splits the first two fields from the left and the last two from the
    right, so a `|` inside the free-text middle field doesn't shift FACT/
    DOMAIN out of place. Returns None for non-FINDING lines (CONTEXT/
    HANDLED/blank) or lines that don't have all five fields.
    """
    line = line.strip()
    if not line or "|" not in line:
        return None
    try:
        severity, rest = line.split("|", 1)
        file_line, rest = rest.split("|", 1)
        text, fact, domain = rest.rsplit("|", 2)
    except ValueError:
        return None
    severity = severity.strip()
    if severity not in VALID_SEVERITIES:
        return None
    return {
        "severity": severity,
        "file": file_line.split(":", 1)[0].strip(),
        "text": text.strip(),
        "fact": fact.strip(),
        "domain": domain.strip(),
    }


def parse_pipe_findings(raw_text):
    """Returns (parsed FINDING lines, count of non-blank raw lines seen).

    The raw count matters as much as the parsed findings: CONTEXT/HANDLED
    lines are expected to not parse as findings, so a lower parsed count
    isn't itself a failure signal -- but if a prompt regression drops a
    field from every FINDING line, recall would otherwise silently read as
    "0 findings, 0% recall", indistinguishable from a genuinely quiet run
    unless the caller can also see that N raw lines went in.
    """
    raw_lines = [line for line in raw_text.splitlines() if line.strip()]
    parsed = [f for f in (parse_pipe_line(line) for line in raw_lines) if f]
    return parsed, len(raw_lines)


def matches(golden_finding, candidate):
    if os.path.basename(golden_finding["file"]) != os.path.basename(
        candidate.get("file", "")
    ):
        return False
    text = candidate.get("text", "").lower()
    return any(kw.lower() in text for kw in golden_finding["keywords"])


def score(golden, candidates, expect_absent=()):
    hits = []
    misses = []
    present = []
    matched_candidate_ids = set()
    unknown = set(expect_absent) - {gf["id"] for gf in golden["findings"]}
    if unknown:
        raise ValueError(f"--expect-absent names unknown ids: {sorted(unknown)}")
    for gf in golden["findings"]:
        if gf["id"] in expect_absent:
            if any(matches(gf, c) for c in candidates):
                present.append(gf["id"])
            continue
        hit_indices = [i for i, c in enumerate(candidates) if matches(gf, c)]
        (hits if hit_indices else misses).append(gf["id"])
        matched_candidate_ids.update(hit_indices)

    scored = len(golden["findings"]) - len(set(expect_absent))
    return {
        "recall": len(hits) / scored if scored else 0.0,
        "scored": scored,
        "present": present,
        "hits": hits,
        "misses": misses,
        "candidate_count": len(candidates),
        "matched_candidate_count": len(matched_candidate_ids),
        "signal_ratio": (
            len(matched_candidate_ids) / len(candidates) if candidates else 0.0
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "candidate",
        help="path to a JSON findings list, or raw pipe-format agent output",
    )
    parser.add_argument("--golden", default=DEFAULT_GOLDEN)
    parser.add_argument("--label", default=None)
    parser.add_argument(
        "--expect-absent",
        action="append",
        default=[],
        metavar="ID",
        help="a golden finding id the run must not report (repeatable)",
    )
    parser.add_argument(
        "--format",
        choices=["json", "pipe"],
        default=None,
        help="defaults to json for .json files, pipe otherwise",
    )
    args = parser.parse_args()

    fmt = args.format or ("json" if args.candidate.endswith(".json") else "pipe")
    try:
        golden = load_json(args.golden)
        if fmt == "json":
            candidates = load_json(args.candidate)
            raw_line_count = None
        else:
            with open(args.candidate, encoding="utf-8") as f:
                candidates, raw_line_count = parse_pipe_findings(f.read())
    except FileNotFoundError as e:
        print(f"error: {e.filename} not found", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(
            f"error: {args.candidate if fmt == 'json' else args.golden} is not valid JSON: {e}",
            file=sys.stderr,
        )
        return 1

    try:
        result = score(golden, candidates, args.expect_absent)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    label = args.label or os.path.basename(args.candidate)
    output_chars = os.path.getsize(args.candidate)

    print(f"=== {label} ===")
    print(
        f"recall:            {result['recall']:.0%} ({len(result['hits'])}/{result['scored']})"
    )
    if result["misses"]:
        print(f"missed:            {', '.join(result['misses'])}")
    print(f"candidate findings: {result['candidate_count']}")
    if raw_line_count is not None:
        print(
            f"raw lines seen:    {raw_line_count} (non-blank lines in the pipe-format input)"
        )
    print(
        f"signal ratio:      {result['signal_ratio']:.0%} (matched a planted issue / total findings)"
    )
    print(f"output size:       {output_chars} bytes ({fmt} format)")
    for fid in args.expect_absent:
        state = (
            "PRESENT, should have been held back"
            if fid in result["present"]
            else "absent, as expected"
        )
        print(f"expect absent:     {fid}: {state}")

    return 1 if result["present"] else 0


if __name__ == "__main__":
    sys.exit(main())
