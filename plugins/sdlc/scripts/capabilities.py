#!/usr/bin/env python3
"""Portable capability manifest validation and profile resolution.

This module is deliberately host-neutral: callers provide declared capability
data; it does not inspect Claude commands, sibling plugin files, or process
state. Unknown manifest fields survive so adapters can attach `x-` metadata,
but unknown capability names are rejected to avoid falsely promising support.
"""

import json

KNOWN_CAPABILITIES = frozenset(
    {"plan", "review", "imagine", "fix", "test", "lint", "typecheck", "ship"}
)

# Order is both precedence for implicit resolution and the stable reporting
# order for missing requirements. `claude-enforced` is intentionally absent:
# it describes a host adapter/enforcement mode, not portable core evidence.
PROFILE_REQUIREMENTS = {
    "baseline": ("test", "lint", "typecheck"),
    "review": ("test", "lint", "typecheck", "review"),
    "adversarial": ("test", "lint", "typecheck", "review", "imagine", "fix"),
}


def validate_manifest(data):
    """Validate the documented v1 structural subset and return a normalized dict.

    The return value preserves all input fields, including unknown and `x-`
    extension keys, while normalizing `capabilities` into sorted deterministic
    form for storage and comparisons.
    """
    if not isinstance(data, dict):
        raise ValueError("manifest must be a JSON object")
    if data.get("protocol_version") != "1":
        raise ValueError('manifest protocol_version must be "1"')

    provider = data.get("provider")
    if not isinstance(provider, dict):
        raise ValueError("manifest provider must be an object")
    name = provider.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("manifest provider.name must be a non-empty string")

    capabilities = data.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        raise ValueError("manifest capabilities must be a non-empty array")
    if len(capabilities) != len(set(capabilities)):
        raise ValueError("manifest capabilities must not contain duplicates")
    unknown = [c for c in capabilities if c not in KNOWN_CAPABILITIES]
    if unknown:
        raise ValueError(
            "manifest contains unknown capability "
            f"{unknown[0]!r}; valid capabilities: {', '.join(sorted(KNOWN_CAPABILITIES))}"
        )

    normalized = dict(data)
    normalized["capabilities"] = sorted(capabilities)
    return normalized


def load_manifest(path):
    """Load and validate a manifest from an explicit path.

    Raises ValueError with the path included so CLI callers can surface one
    actionable message without adding their own context.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError(f"{path}: {e}") from e
    try:
        return validate_manifest(data)
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from e


def missing_capabilities(profile, available):
    """Requirements for `profile` missing from `available`, in profile order."""
    required = PROFILE_REQUIREMENTS[profile]
    present = set(available)
    return [capability for capability in required if capability not in present]


def resolve_profile(available, requested=None):
    """Resolve a requested or highest-complete profile.

    Returns only JSON-serializable values. A requested unavailable profile is a
    clear failure, never a silent downgrade; with no request, the highest
    complete portable profile is normal behavior rather than a downgrade state.
    """
    available_set = set(available)
    if requested is not None:
        if requested not in PROFILE_REQUIREMENTS:
            return {
                "decision": "unavailable",
                "resolved_profile": None,
                "requested_profile": requested,
                "missing": [],
                "reason": (
                    f'unknown profile "{requested}"; valid profiles: '
                    f"{', '.join(PROFILE_REQUIREMENTS)}"
                ),
            }
        missing = missing_capabilities(requested, available_set)
        if missing:
            return {
                "decision": "unavailable",
                "resolved_profile": None,
                "requested_profile": requested,
                "missing": missing,
                "reason": (
                    f'requested profile "{requested}" is unavailable; missing: '
                    f"{', '.join(missing)}"
                ),
            }
        return {
            "decision": "selected",
            "resolved_profile": requested,
            "requested_profile": requested,
            "missing": [],
            "reason": f'requested profile "{requested}" is available',
        }

    for profile in reversed(tuple(PROFILE_REQUIREMENTS)):
        missing = missing_capabilities(profile, available_set)
        if not missing:
            return {
                "decision": "selected",
                "resolved_profile": profile,
                "requested_profile": None,
                "missing": [],
                "reason": f'highest complete profile is "{profile}"',
            }
    return {
        "decision": "unavailable",
        "resolved_profile": None,
        "requested_profile": None,
        "missing": missing_capabilities("baseline", available_set),
        "reason": (
            "no complete baseline profile is available; missing: "
            + ", ".join(missing_capabilities("baseline", available_set))
        ),
    }
