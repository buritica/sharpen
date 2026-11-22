#!/usr/bin/env python3
"""SessionStart adapter: declare this host's portable SDLC capabilities.

Runs on every host with a session-start event: Claude Code's and Codex's
`SessionStart` hook (both read `hooks/hooks.json`) and pi's `session_start`
(through the pi extension). It names the host (`hosts.detect_host()`: explicit
`SHARPEN_HOST`, else Claude Code's `CLAUDECODE=1`, else `unknown` — never a
guess), detects the sibling grumpy plugin, and writes a v1 capability manifest
the portable resolver can consume. It does not initialize a cycle and does not
weaken enforcement — it only makes the declaration available before the first
auto-init.

Beyond the v1 fields, the manifest carries two `x-` keys:
  x-recording  "automatic" when a host event sweeps grumpy receipts at the end
               of a turn (Claude Code, pi), else "manual" (run --verify). A
               declaration only: auto-init reads the host table, not this.
  x-receipts   whether the installed grumpy writes receipts (ships
               scripts/receipt.py). An older grumpy can't complete a
               receipt-backed cycle. On Claude Code auto-init falls back to
               event-only recording (its Skill event stamps it); other hosts
               have no such event, so they keep the receipt cycle and are told
               to upgrade grumpy to 2.9.0+.

The file is `capabilities.<host>.json`, one per host, so two hosts in one repo
don't overwrite each other. Path precedence mirrors gate_store.state_file_path():
  1. $SDLC_CAPABILITIES_PATH
  2. <main-checkout>/.sharpen/data/capabilities.<host>.json
  3. <main-checkout>/.claude/data/capabilities.<host>.json (legacy, when present)
  4. <cwd>/.sharpen/data/capabilities.<host>.json (git resolution failed)
  5. <cwd>/.claude/data/capabilities.<host>.json (legacy fallback, when present)

Output is quiet on success. Problems are written to stderr but exit 0 so a
SessionStart capability-detection failure never blocks the session.
"""

import glob
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

import gate_store as gs
import hosts

CAPABILITY_BY_SKILL = {
    "simplify": ("grumpy", "simplify"),
    "review": ("grumpy", "review"),
    "imagine": ("grumpy", "imagine"),
    "fix": ("grumpy", "fix"),
}

# The portable core capabilities this plugin's own commands provide. Detection
# can only add the skill-backed entries above; absence of one of these is not
# inferred from the filesystem, because the plugin itself is what is running.
BASE_CAPABILITIES = ("test", "lint", "typecheck", "ship", "plan")

# The file whose presence says the installed grumpy writes receipts (grumpy
# 2.9.0+). Checked the same way as the skill files, so it shares their
# candidate locations and their limits.
RECEIPT_WRITER = ("grumpy", os.path.join("scripts", "receipt.py"))


def _manifest_path(host, cwd=None):
    return gs.state_file_path(hosts.manifest_name(host), hosts.MANIFEST_ENV, cwd)


def _plugin_root():
    # scripts/<this file> -> plugin root is one level up.
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _sibling_skill_candidates(plugin, skill, plugin_root=None):
    """Every plausible on-disk location for <plugin>'s skills/<skill>/SKILL.md."""
    return _sibling_candidates(
        plugin, os.path.join("skills", skill, "SKILL.md"), plugin_root
    )


def _sibling_candidates(plugin, relpath, plugin_root=None):
    """Every plausible on-disk location for <plugin>'s `relpath`.

    A single `${CLAUDE_PLUGIN_ROOT}/../<plugin>` join (this function's
    previous shape) only works in a flat dev checkout, where `plugins/sdlc`
    and `plugins/grumpy` sit as direct siblings. It breaks under the *real*
    installed layout: this repo's own CLAUDE.md already documents Claude
    Code's plugin cache as version-nested (`cache/<marketplace>/<plugin>/
    <version>/`) in its "Hook authoring" note, and the same is now directly
    confirmed for Codex CLI too — a live installed Codex plugin cache showed
    this join silently resolving to `cache/sharpen/sdlc/grumpy/...` (one
    level too shallow) and never matching. `grumpy`'s own version segment is
    unknown to `sdlc` at that point (the two plugins version independently),
    so the nested case globs for it rather than guessing a fixed depth.

    CLAUDE.md's "Hook authoring" note also says to "detect sibling
    capabilities by command availability instead, the way /sdlc:gate detects
    grumpy" — deliberately not followed here. That pattern relies on an LLM
    agent consulting its own list of available skills/commands; this file is
    a plain Python subprocess invoked by a SessionStart hook, with no such
    list to consult. Filesystem detection is the only signal available to
    it, so the fix widens it (two shapes instead of one, plus an override)
    rather than switching to a mechanism this script has no access to.

    These two shapes are what's confirmed today, not a closed set — a third
    host with a different cache layout would reproduce this exact bug
    silently (this adapter fails open by design; see the module docstring).
    Multiple cached versions of the sibling plugin are also not
    disambiguated: the glob matches any version directory that has the
    skill file, so a stale leftover from a since-upgraded install could
    false-positive a capability that's no longer actually there. Neither
    gap has a fix here — both are accepted limitations, not oversights.
    `SDLC_<PLUGIN>_ROOT` (e.g. `SDLC_GRUMPY_ROOT`) is the escape hatch for
    the former: point it directly at the sibling plugin's root to add one
    more candidate, without waiting on a new sdlc release — same naming
    convention as this file's own `SDLC_CAPABILITIES_PATH` override.

    `<PLUGIN>_PLUGIN_ROOT` (e.g. `GRUMPY_PLUGIN_ROOT`) is the sibling's own
    exported root. pi's grumpy extension sets it in the shared process env
    before this hook runs, which makes it the most precise candidate there;
    it is checked first."""
    root = plugin_root or _plugin_root()
    flat_root = os.path.abspath(os.path.join(root, ".."))
    nested_marketplace_root = os.path.abspath(os.path.join(root, "..", ".."))
    candidates = []
    exported = os.environ.get(f"{plugin.upper()}_PLUGIN_ROOT")
    if exported:
        candidates.append(os.path.join(exported, relpath))
    candidates.append(os.path.join(flat_root, plugin, relpath))
    candidates += sorted(
        glob.glob(os.path.join(nested_marketplace_root, plugin, "*", relpath))
    )
    override = os.environ.get(f"SDLC_{plugin.upper()}_ROOT")
    if override:
        candidates.append(os.path.join(override, relpath))
    return candidates


def detect_capabilities(path_exists=os.path.isfile, plugin_root=None):
    """`SDLC_DEBUG=1` writes one stderr line per skill-backed capability,
    naming which candidate path (if any) matched — this bug (silently
    missing capabilities under a real plugin cache) took a live debugging
    session to find precisely because nothing said which of the candidate
    shapes, if any, was actually checked. Off by default: this hook's
    contract is quiet on success (see module docstring), and this is
    diagnostic noise on every session, not something a normal run should
    print."""
    debug = os.environ.get("SDLC_DEBUG") == "1"
    capabilities = set(BASE_CAPABILITIES)
    for capability, (plugin, skill) in CAPABILITY_BY_SKILL.items():
        candidates = _sibling_skill_candidates(plugin, skill, plugin_root=plugin_root)
        matched = next((path for path in candidates if path_exists(path)), None)
        if matched:
            capabilities.add(capability)
        if debug:
            sys.stderr.write(
                f"[gate] session-start debug: {capability!r} ({plugin}/{skill}) "
                f"{'matched ' + matched if matched else 'no match among ' + repr(candidates)}\n"
            )
    return sorted(capabilities)


def detect_receipts(path_exists=os.path.isfile, plugin_root=None):
    """True when a reachable grumpy ships the receipt writer.

    Same candidate list, and the same stale-cache caveat, as the skill
    detection above: a leftover newer grumpy version directory can make this
    True for an older active install. That errs toward receipt-backed cycles,
    the default anyway."""
    plugin, relpath = RECEIPT_WRITER
    return any(
        path_exists(path)
        for path in _sibling_candidates(plugin, relpath, plugin_root=plugin_root)
    )


def build_manifest(capabilities, host=hosts.UNKNOWN, receipts=True):
    return {
        "protocol_version": "1",
        "provider": {"name": host},
        "capabilities": sorted(capabilities),
        "x-recording": hosts.recording_mode(host),
        "x-receipts": bool(receipts),
        "x-source": "session-start",
        "x-generated_at": datetime.now(timezone.utc).isoformat(),
    }


def write_manifest(path, manifest):
    # Same atomic-write pattern as gate_store.save_store: tempfile.mkstemp
    # gives a kernel-guaranteed unique name (safe across concurrent writers,
    # including same-pid threads), and the finally block removes the temp
    # file if json.dump raises before os.replace runs.
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix="capabilities-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass  # never let cleanup mask the original error


def main():
    try:
        host = hosts.detect_host()
        path = _manifest_path(host)
        write_manifest(
            path,
            build_manifest(
                detect_capabilities(), host=host, receipts=detect_receipts()
            ),
        )
    except (OSError, ValueError) as e:
        sys.stderr.write(
            f"[gate] session-start: could not write capability manifest: {e}\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
