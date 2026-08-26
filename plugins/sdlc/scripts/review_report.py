#!/usr/bin/env python3
"""Portable review-report validation and gate-store attachment.

This module is host-neutral: adapters pass the report path and structured
report data; it does not know how Claude, another agent, CI, or a human
produced the review. Unknown report fields survive so hosts can attach `x-`
metadata without weakening the portable v1 core.
"""

import json

STATUSES = frozenset({"pass", "fail", "inconclusive"})
PROVENANCE_KINDS = frozenset({"git-range", "legacy"})
SEVERITIES = frozenset({"critical", "serious", "suggestion"})
FINDING_TEXT_FIELDS = ("summary", "location", "consequence")
EXECUTOR_TEXT_FIELDS = ("agent", "model", "adapter")


def _non_empty_string(value):
    return isinstance(value, str) and bool(value.strip())


def validate_report(data):
    """Validate the documented v1 structural subset and return a normalized dict.

    The return value preserves all input fields, including unknown and `x-`
    extension keys. Normalization is intentionally shallow: no timestamps,
    identities, paths, or host-specific fields are invented here.
    """
    if not isinstance(data, dict):
        raise ValueError("review report must be a JSON object")
    if data.get("protocol_version") != "1":
        raise ValueError('review report protocol_version must be "1"')

    status = data.get("status")
    if status not in STATUSES:
        raise ValueError(
            f"review report status must be one of: {', '.join(sorted(STATUSES))}"
        )

    provenance = data.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("review report provenance must be an object")
    kind = provenance.get("kind")
    if kind not in PROVENANCE_KINDS:
        raise ValueError(
            "review report provenance.kind must be one of: "
            + ", ".join(sorted(PROVENANCE_KINDS))
        )
    if kind == "git-range":
        for key in ("base", "head"):
            if not _non_empty_string(provenance.get(key)):
                raise ValueError(
                    f"review report provenance.{key} must be a non-empty string"
                )

    executor = data.get("executor")
    if executor is not None:
        if not isinstance(executor, dict):
            raise ValueError("review report executor must be an object")
        for key in EXECUTOR_TEXT_FIELDS:
            if key in executor and not _non_empty_string(executor[key]):
                raise ValueError(f"review report executor.{key} must be non-empty")

    findings = data.get("findings")
    if not isinstance(findings, list):
        raise ValueError("review report findings must be an array")
    for index, finding in enumerate(findings):
        prefix = f"review report findings[{index}]"
        if not isinstance(finding, dict):
            raise ValueError(f"{prefix} must be an object")
        if finding.get("severity") not in SEVERITIES:
            raise ValueError(
                f"{prefix}.severity must be one of: {', '.join(sorted(SEVERITIES))}"
            )
        for key in FINDING_TEXT_FIELDS:
            if not _non_empty_string(finding.get(key)):
                raise ValueError(f"{prefix}.{key} must be a non-empty string")

    return dict(data)


def load_report(path):
    """Load and validate a report from an explicit path."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError(f"{path}: {e}") from e
    try:
        return validate_report(data)
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from e


def attach_report(data, branch, report):
    """Attach a validated report to a branch cycle and return the cycle.

    This is evidence metadata, not gate completion: attaching even a passing
    report does not stamp `grumpy-review`, and replacing the report never
    mutates gate timestamps. The artifact stays under the existing cycle so
    legacy stores and pre-profile readers can ignore it.
    """
    bd = data.get(branch)
    if bd is None:
        raise ValueError(
            f'No gate cycle for branch "{branch}". Run: record-gate.py --init <tier>'
        )
    bd["review_report"] = validate_report(report)
    return bd
