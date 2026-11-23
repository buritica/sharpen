#!/usr/bin/env python3
"""Generic portable-core adapter.

Runs gate commands for any host that can execute shell commands and produce
JSON. Reads a capability manifest, resolves the highest complete profile,
executes the mapped commands and reports PASS/FAIL. It does not review: a
gate summary is not a review, so `review` and `imagine` are left to grumpy (an
agent host, or `run_with_oneshot` below, which openai_adapter.py and
local_llm_adapter.py use). This is the reference non-Claude adapter: it exercises the portable
core without any host-specific hooks, skills, or lifecycle events.

Pure stdlib. Exits 0 on success, 1 on gate failure, 2 on unrecoverable error.
"""

import os
import subprocess
import sys
import time

import capabilities

# The grumpy passes the one-shot path runs, in gate order (gates 3 and 5).
ONESHOT_PASSES = ("review", "imagine")

# Default commands for capabilities that have no host-specific mapping. These
# are the portable baseline: any POSIX environment with Python can run them.
DEFAULT_COMMANDS = {
    "test": "test ! -d tests || python3 -m unittest discover -s tests -p 'test_*.py'",
    # `py_compile` requires explicit file names and `__file__` is not defined
    # under `python -c`; compileall is a safe, useful portable syntax check.
    "lint": "python3 -m compileall -q .",
    "typecheck": "python3 -m compileall -q .",
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


def run_gates(manifest, profile, cwd=None, skip_capabilities=()):
    """Execute mapped commands for a resolved profile and return results.

    `skip_capabilities` is for an adapter that implements a profile capability
    directly rather than through a shell command (for example, local review).
    Returns dicts with capability, command, exit_code, duration_s, stdout, and
    stderr. A gate fails if its command exits non-zero.
    """
    commands = _resolve_commands(manifest)
    skipped = set(skip_capabilities)
    results = []
    for cap in capabilities.PROFILE_REQUIREMENTS[profile]:
        if cap in skipped:
            continue
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


def summarize(results, name="generic-adapter", tail=20):
    """Log one PASS/FAIL line per gate command, tagged with the adapter's
    `name`; a FAIL is followed by the command's last `tail` lines of output
    (stderr, else stdout), indented, so the reason is on the screen and not
    only in the result dict. Returns the failures."""
    for r in results:
        status = "PASS" if r["exit_code"] == 0 else "FAIL"
        _log(f"[{name}] {status} {r['capability']} ({r['duration_s']}s)")
        if r["exit_code"] != 0:
            output = (r["stderr"] or r["stdout"] or "").rstrip()
            lines = output.split("\n")[-tail:] if output else ["(no output)"]
            if r["command"] is not None:
                lines.insert(0, f"$ {r['command']} -> exit {r['exit_code']}")
            for line in lines:
                _log(f"[{name}]     {line}")
    return [r for r in results if r["exit_code"] != 0]


def resolve_manifest(argv, name):
    """(manifest, profile) or an exit code, shared by every adapter here."""
    manifest_path = argv[0] if argv else os.environ.get("SHARPEN_CAPABILITIES")
    if not manifest_path:
        _log(f"usage: {name} <capabilities.json> [--grumpy <grumpy plugin root>]")
        _log("       or set SHARPEN_CAPABILITIES")
        return 2
    try:
        manifest = capabilities.load_manifest(manifest_path)
    except ValueError as e:
        _log(f"[{name}] error: {e}")
        return 2
    decision = capabilities.resolve_profile(manifest["capabilities"])
    if decision["decision"] != "selected":
        _log(f"[{name}] error: {decision['reason']}")
        return 2
    profile = decision["resolved_profile"]
    _log(f"[{name}] resolved profile: {profile}")
    return manifest, profile


def grumpy_oneshot(argv, env=None):
    """Path to grumpy's scripts/oneshot.py: `--grumpy <root>` in argv, else
    GRUMPY_PLUGIN_ROOT. sdlc can't import grumpy (separate plugins, and a
    `../grumpy` path never exists in the version-nested plugin cache), so the
    root is always named explicitly. Returns (path or None, the root that was
    tried or None, remaining argv): the root is kept so the error can name
    the folder that has no oneshot.py (an installed grumpy older than 2.16
    looks exactly like no grumpy at all otherwise)."""
    env = os.environ if env is None else env
    args = list(argv)
    root = env.get("GRUMPY_PLUGIN_ROOT")
    if "--grumpy" in args:
        i = args.index("--grumpy")
        if i + 1 >= len(args):
            raise ValueError("--grumpy needs a path (the grumpy plugin folder)")
        root = args[i + 1]
        del args[i : i + 2]
    path = os.path.join(root, "scripts", "oneshot.py") if root else None
    return (path if path and os.path.isfile(path) else None), (root or None), args


def missing_grumpy_message(name, root):
    if root:
        return (
            f"[{name}] error: {os.path.join(root, 'scripts', 'oneshot.py')} not found. "
            "The review runs grumpy's oneshot.py, which needs grumpy 2.16 or newer "
            "(2.17 for imagine): upgrade grumpy, or point GRUMPY_PLUGIN_ROOT / "
            "--grumpy <path> at one"
        )
    return (
        f"[{name}] error: grumpy not found. Set GRUMPY_PLUGIN_ROOT to the grumpy "
        "plugin folder or pass --grumpy <path>; the review runs grumpy's "
        "oneshot.py (grumpy 2.16 or newer, 2.17 for imagine)"
    )


def declared_passes(manifest):
    """The one-shot passes the manifest declares, in gate order."""
    return [p for p in ONESHOT_PASSES if p in manifest["capabilities"]]


def _map_legacy_env(env, legacy_env, name):
    """Copy each deprecated variable to its new name, with a warning, unless
    the new name is already set."""
    for old, new in (legacy_env or {}).items():
        if env.get(old) and not env.get(new):
            _log(f"[{name}] {old} is deprecated; set {new} instead")
            env[new] = env[old]


def _model_flags(manifest, env, name):
    """`--model <provider.model>` unless SHARPEN_LLM_MODEL beats it (said)."""
    model = (manifest.get("provider") or {}).get("model")
    if model and not env.get("SHARPEN_LLM_MODEL"):
        return ["--model", model]
    if model:
        _log(
            f"[{name}] SHARPEN_LLM_MODEL={env['SHARPEN_LLM_MODEL']} overrides the "
            f"manifest's provider.model ({model})"
        )
    return []


def _run_passes(oneshot, passes, flags, env, name):
    """Run `oneshot.py <pass> <flags>` for each pass. A pass that earns
    nothing doesn't skip the next (separate gates); exit 2 (setup) or a
    signal stops the run. Returns the worst exit code, 0/1/2. Every pass
    reviews the same commit: imagine runs straight after review, with no
    fix between them (see run_with_oneshot)."""
    # ponytail: no deadline on the pass subprocesses and no --timeout/--jobs/
    # --aspects pass-through; oneshot's own defaults bound each at two attempts
    # times --timeout per aspect (about thirty minutes when a provider hangs).
    # Add a manifest `provider.timeout` the first time an adapter run is
    # reported hanging past that, or a caller has to kill it.
    worst = 0
    for pass_name in passes:
        code = subprocess.call([sys.executable, oneshot, pass_name, *flags], env=env)
        if code < 0:  # killed by a signal: outside the 0/1/2 contract
            _log(f"[{name}] oneshot.py was killed by signal {-code}; no {pass_name}")
            return 1
        if code == 2:  # the key, the model or the URL: the next pass fails too
            return 2
        if code:
            _log(f"[{name}] {pass_name} earned nothing (exit {code})")
        worst = max(worst, code)
    return worst


def run_with_oneshot(argv, name, provider, legacy_env=None, env=None):
    """Gate commands from the manifest, then grumpy's one-shot passes.

    The shared body of openai_adapter.py and local_llm_adapter.py. Each pass
    the manifest declares (`review`, `imagine`, in that order) is grumpy's
    own (`oneshot.py review|imagine`): the same prompts as an agent host, a
    report under .sharpen/grumpy/<branch>/ and a receipt that `record-gate.py
    --verify review|imagine` records gate 3 or 5 from. One pass earning
    nothing doesn't skip the other (they are separate gates); a setup error
    (exit 2) stops both. Imagine runs on the same commit as review, before
    any fix: on this path nobody runs `/grumpy:fix` between them, so its
    report covers the pre-fix code. That is deliberate, not the agent
    chain's review -> fix -> imagine order; once fixes are committed the
    imagine gate sits on an older commit and blocks the PR as stale until
    the adapter runs again on the new HEAD. `legacy_env` maps an old variable name to the one
    oneshot.py reads, honored with a warning. Exit codes: 2 setup error, 1 a
    gate command failed or a pass earned nothing, 0 every gate command passed
    and every declared pass wrote its report and receipt. A manifest with
    neither runs no pass, needs no grumpy: it says so on stderr and exits 0."""
    env = dict(os.environ if env is None else env)
    try:
        oneshot, grumpy_root, argv = grumpy_oneshot(argv, env)
    except ValueError as e:
        _log(f"[{name}] error: {e}")
        return 2
    resolved = resolve_manifest(argv, name)
    if isinstance(resolved, int):
        return resolved
    manifest, profile = resolved
    passes = declared_passes(manifest)
    # Only a declared pass needs grumpy, and then it should fail here, before
    # the gate commands run, not after.
    if passes and oneshot is None:
        _log(missing_grumpy_message(name, grumpy_root))
        return 2
    _map_legacy_env(env, legacy_env, name)
    results = run_gates(manifest, profile, skip_capabilities=ONESHOT_PASSES)
    if summarize(results, name):
        _log(f"[{name}] gate command(s) failed; no grumpy pass run")
        return 1
    if not passes:
        _log(
            f"[{name}] gate commands passed; the manifest declares no `review` "
            "or `imagine` capability, so nothing was reviewed and no receipt "
            "was written"
        )
        return 0
    flags = ["--provider", provider, *_model_flags(manifest, env, name)]
    return _run_passes(oneshot, passes, flags, env, name)


def main(argv):
    """Run the manifest's gate commands and report PASS/FAIL. Nothing is
    reviewed and nothing is recorded: test, lint and typecheck gates record
    through `record-gate.py`, and review and imagine only through a grumpy
    pass (an agent host, or `openai_adapter.py` / `local_llm_adapter.py`)."""
    resolved = resolve_manifest(argv, "generic-adapter")
    if isinstance(resolved, int):
        return resolved
    manifest, profile = resolved
    failures = summarize(run_gates(manifest, profile, skip_capabilities=ONESHOT_PASSES))
    if failures:
        _log(f"[generic-adapter] {len(failures)} gate command(s) failed")
        return 1
    _log("[generic-adapter] all gate commands passed")
    declared = declared_passes(manifest)
    if declared:
        _log(
            f"[generic-adapter] {' and '.join(f'`{p}`' for p in declared)} "
            f"{'is' if len(declared) == 1 else 'are'} declared but not run here: "
            "a grumpy pass runs them (an agent host, openai_adapter.py or "
            "local_llm_adapter.py)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
