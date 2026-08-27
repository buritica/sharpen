#!/usr/bin/env python3
"""Score a grumpy findings run against the golden fixture.

Usage:
    python3 scripts/eval-grumpy-findings.py <candidate.json> [--golden path] [--label name]

<candidate.json> is a JSON list of findings, each shaped like:
    {"severity": "CRIT", "file": "src/billing/charge.py", "text": "..."}

Used to compare recall (did the run still catch the planted issues?) and
output size (a token-cost proxy) between two prompt variants on the same
fixture diff -- see plugins/grumpy/tests/fixtures/README.md.
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_GOLDEN = os.path.join(
    ROOT, "plugins", "grumpy", "tests", "fixtures", "golden.json"
)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def matches(golden_finding, candidate):
    if os.path.basename(golden_finding["file"]) != os.path.basename(
        candidate.get("file", "")
    ):
        return False
    text = candidate.get("text", "").lower()
    return any(kw.lower() in text for kw in golden_finding["keywords"])


def score(golden, candidates):
    hits = []
    misses = []
    matched_candidate_ids = set()
    for gf in golden["findings"]:
        hit_indices = [i for i, c in enumerate(candidates) if matches(gf, c)]
        (hits if hit_indices else misses).append(gf["id"])
        matched_candidate_ids.update(hit_indices)

    return {
        "recall": len(hits) / len(golden["findings"]) if golden["findings"] else 0.0,
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
    parser.add_argument("candidate", help="path to a JSON list of findings")
    parser.add_argument("--golden", default=DEFAULT_GOLDEN)
    parser.add_argument("--label", default=None)
    args = parser.parse_args()

    golden = load_json(args.golden)
    candidates = load_json(args.candidate)
    result = score(golden, candidates)

    label = args.label or os.path.basename(args.candidate)
    output_chars = os.path.getsize(args.candidate)

    print(f"=== {label} ===")
    print(f"recall:            {result['recall']:.0%} ({len(result['hits'])}/{len(golden['findings'])})")
    if result["misses"]:
        print(f"missed:            {', '.join(result['misses'])}")
    print(f"candidate findings: {result['candidate_count']}")
    print(f"signal ratio:      {result['signal_ratio']:.0%} (matched a planted issue / total findings)")
    print(f"output size:       {output_chars} bytes (candidate JSON)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
