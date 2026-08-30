#!/usr/bin/env python3
"""Claude SessionStart adapter: declare this host's portable SDLC capabilities.

This is intentionally Claude-specific: it runs on Claude's SessionStart hook,
detects the sibling skill plugins Claude has installed, and writes a v1
capability manifest the portable resolver can consume. It does not initialize a
cycle and does not weaken enforcement — it only makes the capability declaration
available before the first auto-init.

Manifest path precedence mirrors gate_store.state_file_path():
  1. $SDLC_CAPABILITIES_PATH
  2. <main-checkout>/.sharpen/data/capabilities.claude.json
  3. <main-checkout>/.claude/data/capabilities.claude.json (legacy, when present)
  4. <cwd>/.sharpen/data/capabilities.claude.json (git resolution failed)
  5. <cwd>/.claude/data/capabilities.claude.json (legacy fallback, when present)

Output is quiet on success. Problems are written to stderr but exit 0 so a
SessionStart capability-detection failure never blocks the session.
"""

import json
import os
import sys
from datetime import datetime, timezone

import gate_store as gs

CAPABILITY_BY_SKILL = {
    "review": ("grumpy", "review"),
    "imagine": ("grumpy", "imagine"),
    "fix": ("grumpy", "fix"),
}

# The portable core capabilities this plugin's own commands provide. Detection
# can only add the skill-backed entries above; absence of one of these is not
# inferred from the filesystem, because the plugin itself is what is running.
BASE_CAPABILITIES = ("test", "lint", "typecheck", "ship", "plan")

MANIFEST_ENV = "SDLC_CAPABILITIES_PATH"
MANIFEST_NAME = "capabilities.claude.json"


def _manifest_path(cwd=None):
    return gs.state_file_path(MANIFEST_NAME, MANIFEST_ENV, cwd)


def _plugin_root():
    # scripts/<this file> -> plugin root is one level up.
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _sibling_skill_paths():
    root = os.path.abspath(os.path.join(_plugin_root(), ".."))
    return {
        capability: os.path.join(root, plugin, "skills", skill, "SKILL.md")
        for capability, (plugin, skill) in CAPABILITY_BY_SKILL.items()
    }


def detect_capabilities(path_exists=os.path.isfile):
    capabilities = set(BASE_CAPABILITIES)
    for capability, path in _sibling_skill_paths().items():
        if path_exists(path):
            capabilities.add(capability)
    return sorted(capabilities)


def build_manifest(capabilities):
    return {
        "protocol_version": "1",
        "provider": {"name": "claude-code"},
        "capabilities": sorted(capabilities),
        "x-source": "claude-session-start",
        "x-generated_at": datetime.now(timezone.utc).isoformat(),
    }


def write_manifest(path, manifest):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def main():
    try:
        path = _manifest_path()
        write_manifest(path, build_manifest(detect_capabilities()))
    except (OSError, ValueError) as e:
        sys.stderr.write(
            f"[gate] session-start: could not write capability manifest: {e}\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
