#!/usr/bin/env python3
"""Generic portable-core adapter.

Runs gate commands for any host that can execute shell commands and produce
JSON. Reads a capability manifest, resolves the highest complete profile,
executes the mapped commands, and emits a review report for the `review`
capability. This is the reference non-Claude adapter: it exercises the
portable core without any host-specific hooks, skills, or lifecycle events.

Pure stdlib. Exits 0 on success, 1 on gate failure, 2 on unrecoverable error.
"""

import os
import subprocess
import sys
import time

import capabilities
import gate_store as gs
import review_report

# Default commands for capabilities that have no host-specific mapping. These
# are the portable baseline: any POSIX environment with Python can run them.
DEFAULT_COMMANDS = {
    "test": "python3 -m unittest discover -s tests -p 'test_*.py'",
    "lint": "python3 -m py_compile",
    "typecheck": "python3 -c 'import ast; ast.parse(open(__file__).read())'",
}


def _log(msg):
    sys.stderr.write(msg + "\n")


def _run(cmd, cwd=None):
    """Run a shell command and return (exit_code, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=300,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after 300s"
    except OSError as e:
        return 1, "", str(e)


def _resolve_commands(manifest):
    """Map capabilities to shell commands.

    Host-specific overrides come from `x-host-command-map`; missing entries
    fall back to DEFAULT_COMMANDS. A capability with neither is skipped.
    """
    host_map = manifest.get("x-host-command-map", {})
    commands = {}
    for cap in manifest["capabilities"]:
        if cap in host_map:
            commands[cap] = host_map[cap]
        elif cap in DEFAULT_COMMANDS:
            commands[cap] = DEFAULT_COMMANDS[cap]
    return commands


def _git(*args, cwd=None):
    return (
        subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.DEVNULL)
        .decode()
        .strip()
    )


def _detect_base(cwd=None):
    """The merge-base with the default branch, or the first commit."""
    for ref in ("origin/main", "origin/master", "main", "master"):
        try:
            return _git("merge-base", ref, "HEAD", cwd=cwd)
        except (OSError, subprocess.CalledProcessError):
            continue
    try:
        return _git("rev-list", "--max-parents=0", "HEAD", cwd=cwd).splitlines()[0]
    except (OSError, subprocess.CalledProcessError):
        return "HEAD~1"


def _detect_head(cwd=None):
    try:
        return _git("rev-parse", "HEAD", cwd=cwd)
    except (OSError, subprocess.CalledProcessError):
        return "HEAD"


def run_gates(manifest, profile, cwd=None):
    """Execute the commands for a resolved profile and return results.

    Returns a list of dicts: capability, command, exit_code, duration_s,
    stdout, stderr. A gate fails if its command exits non-zero.
    """
    commands = _resolve_commands(manifest)
    results = []
    for cap in capabilities.PROFILE_REQUIREMENTS[profile]:
        cmd = commands.get(cap)
        if cmd is None:
            results.append(
                {
                    "capability": cap,
                    "command": None,
                    "exit_code": 1,
                    "duration_s": 0,
                    "stdout": "",
                    "stderr": f"no command mapped for capability '{cap}'",
                }
            )
            continue
        start = time.monotonic()
        exit_code, stdout, stderr = _run(cmd, cwd)
        duration = time.monotonic() - start
        results.append(
            {
                "capability": cap,
                "command": cmd,
                "exit_code": exit_code,
                "duration_s": round(duration, 2),
                "stdout": stdout,
                "stderr": stderr,
            }
        )
    return results


def build_review_report(manifest, gate_results, cwd=None):
    """Produce a review report from gate results.

    The report is a synthetic review: it passes if all gates passed, fails if
    any failed, and includes findings for failures. This is the minimal
    portable review — a real adapter would run a delegated reviewer here.
    """
    provider = manifest.get("provider", {})
    failures = [r for r in gate_results if r["exit_code"] != 0]
    findings = [
        {
            "severity": "serious",
            "summary": f"gate '{r['capability']}' failed",
            "location": r["command"] or "(no command)",
            "consequence": r["stderr"] or f"exit code {r['exit_code']}",
        }
        for r in failures
    ]
    return review_report.validate_report(
        {
            "protocol_version": "1",
            "status": "fail" if failures else "pass",
            "provenance": {
                "kind": "git-range",
                "base": _detect_base(cwd),
                "head": _detect_head(cwd),
            },
            "executor": {
                "agent": provider.get("agent", "generic-adapter"),
                "model": provider.get("model", "unknown"),
                "adapter": provider.get("name", "generic"),
            },
            "findings": findings,
        }
    )


def main(argv):
    manifest_path = argv[0] if argv else os.environ.get("SHARPEN_CAPABILITIES")
    if not manifest_path:
        _log("usage: generic_adapter.py <capabilities.json>")
        _log("       or set SHARPEN_CAPABILITIES")
        return 2

    try:
        manifest = capabilities.load_manifest(manifest_path)
    except ValueError as e:
        _log(f"[generic-adapter] error: {e}")
        return 2

    decision = capabilities.resolve_profile(manifest["capabilities"])
    if decision["decision"] != "selected":
        _log(f"[generic-adapter] error: {decision['reason']}")
        return 2
    profile = decision["resolved_profile"]
    _log(f"[generic-adapter] resolved profile: {profile}")

    branch = gs.detect_branch()
    if not branch:
        _log("[generic-adapter] error: not on a named branch")
        return 2

    results = run_gates(manifest, profile)
    failures = [r for r in results if r["exit_code"] != 0]

    for r in results:
        status = "PASS" if r["exit_code"] == 0 else "FAIL"
        _log(f"[generic-adapter] {status} {r['capability']} ({r['duration_s']}s)")

    report = build_review_report(manifest, results)

    # Attach the review report to the gate store so ci-validate.py can read it.
    path = gs.default_store_path()
    try:
        gs.update_store(path, lambda d: review_report.attach_report(d, branch, report))
        _log(f"[generic-adapter] attached review report for {branch}")
    except ValueError as e:
        _log(f"[generic-adapter] warning: could not attach review report: {e}")

    if failures:
        _log(f"[generic-adapter] {len(failures)} gate(s) failed")
        return 1
    _log("[generic-adapter] all gates passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
