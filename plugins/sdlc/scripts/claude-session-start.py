#!/usr/bin/env python3
"""Claude SessionStart adapter: declare this host's portable SDLC capabilities.

This is intentionally Claude-specific: it runs on Claude's SessionStart hook,
detects the sibling skill plugins Claude has installed, and writes a v1
capability manifest the portable resolver can consume. It does not initialize a
cycle and does not weaken enforcement — it only makes the capability declaration
available before the first auto-init.

Manifest path precedence mirrors gate_store.default_store_path():
  1. $SDLC_CAPABILITIES_PATH
  2. <main-checkout>/.claude/data/capabilities.claude.json
  3. <cwd>/.claude/data/capabilities.claude.json (git resolution failed)

Output is quiet on success. Problems are written to stderr but exit 0 so a
SessionStart capability-detection failure never blocks the session.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

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


def _git(args, cwd=None):
    return (
        subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.DEVNULL)
        .decode()
        .strip()
    )


def _manifest_path(cwd=None):
    override = os.environ.get(MANIFEST_ENV)
    if override:
        return override
    try:
        common = _git(["rev-parse", "--git-common-dir"], cwd=cwd)
        root = os.path.dirname(
            os.path.realpath(os.path.join(cwd or os.getcwd(), common))
        )
    except (OSError, subprocess.CalledProcessError):
        root = os.path.realpath(cwd or os.getcwd())
    return os.path.join(root, ".claude", "data", MANIFEST_NAME)


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
