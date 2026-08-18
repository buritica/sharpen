#!/usr/bin/env python3
"""
Shared store + logic for SDLC gate tracking.

Gate state is a plain JSON file, keyed by branch and SHARED across all worktrees
of the same repo, so any worktree (or cwd inside the repo) sees every branch's
cycle. No database, no bun — pure stdlib so the enforce/record hooks run on
every box that has python3.

The shared location is resolved from `git rev-parse --git-common-dir`, which
points at the MAIN checkout's .git from every linked worktree. Keying by branch
keeps two branches checked out in two worktrees isolated from each other.

Store path (precedence):
  1. $SDLC_GATES_PATH env var
  2. <main-checkout>/.claude/data/gates.json  (dirname of the common .git dir)
  3. <cwd>/.claude/data/gates.json   (git resolution failed)

Schema:
  {
    "<branch>": {
      "tier": "small-medium",
      "created_at": "2026-06-16T04:53:38+00:00",
      "gates": { "tests": "<iso8601>", "lint": "<iso8601>", ... },
      "routed_from": ["/abs/path/to/a/driving/worktree"]   # optional
    }
  }

`routed_from` is the cross-worktree routing channel. `/sdlc:gate --worktree A`
runs from session B, so the PostToolUse auto-record hook — which only ever sees
B's cwd — would otherwise stamp skill gates on B's branch. At `--init` the
command writes B's worktree root here, and the hook resolves its own root and
prefers the branch routed from it over `detect_branch()`. A channel, not a
heuristic: it works with any number of live worktrees.
"""

import fcntl
import glob
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone


class StoreCorruptError(Exception):
    """The store file exists but could not be parsed. Distinct from 'no file
    yet' so callers can fail closed instead of mistaking corruption for an
    empty (opt-in) store."""


TIERS = ("tiny", "small-medium", "significant")

# Key under a branch's cycle holding the worktree root that drives it. See the
# module docstring — this is the cross-worktree routing channel.
ROUTE_KEY = "routed_from"

# Canonical gate names — the JSON keys, the --record arguments, and the
# auto-record targets all use these exact strings. Keep them in sync with
# gate.md's gate-chain table.
ALL_GATE_NAMES = [
    "tests",
    "simplify",
    "grumpy-review",
    "grumpy-fix-post-review",
    "grumpy-imagine",
    "grumpy-fix-post-imagine",
    "lint",
    "typecheck",
]

GATES_BY_TIER = {
    "tiny": ["tests", "lint", "typecheck"],
    "small-medium": ALL_GATE_NAMES,
    "significant": ALL_GATE_NAMES,
}

# Skill name (as seen on the Skill tool) -> gate it records.
SKILL_TO_GATE = {
    "simplify": "simplify",
    "grumpy:review": "grumpy-review",
    "grumpy:imagine": "grumpy-imagine",
    "grumpy:fix": "grumpy-fix",
}

# Gate -> the skill that legitimately records it. These may ONLY be recorded by
# the auto-record hook after that skill runs, never by a manual --record; the
# bash-verifiable gates stay manually recordable. One map, so the gate list and
# the "run this instead" text can't drift apart.
SKILL_FOR_GATE = {
    "simplify": "/simplify",
    "grumpy-review": "/grumpy:review",
    "grumpy-fix-post-review": "/grumpy:fix",
    "grumpy-imagine": "/grumpy:imagine",
    "grumpy-fix-post-imagine": "/grumpy:fix",
}
BASH_GATES = [g for g in ALL_GATE_NAMES if g not in SKILL_FOR_GATE]


def gate_lists_hint():
    """The two-line "which gates are which" tail, shared by every refusal."""
    return (
        f"Skill-gated gates: {', '.join(SKILL_FOR_GATE)}\n"
        f"Manually recordable gates: {', '.join(BASH_GATES)}"
    )


def skill_gated_message(gate):
    """Refusal text shared by the store, the CLI, and the PreToolUse hook."""
    return (
        f'BLOCKED: Gate "{gate}" is skill-gated and cannot be recorded manually.\n\n'
        f"Run the skill instead: {SKILL_FOR_GATE[gate]}\n"
        "The gate will be auto-recorded after the skill completes.\n\n"
        + gate_lists_hint()
        + (
            f"\n\nDon't have {SKILL_FOR_GATE[gate]}? Then this cycle cannot "
            "complete: install it, or re-init as `tiny` if the change genuinely "
            "qualifies (≤3 lines, no executable code)."
        )
    )


def _git(*args, cwd=None):
    return (
        subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.DEVNULL)
        .decode()
        .strip()
    )


def git_root(cwd=None):
    try:
        return _git("rev-parse", "--show-toplevel", cwd=cwd)
    except (OSError, subprocess.CalledProcessError):
        return None


def detect_branch(cwd=None):
    try:
        return _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
    except (OSError, subprocess.CalledProcessError):
        return None


# Pulling the working directory out of a command string used to live here as a
# pair of regexes. It is shell parsing, not storage, and scoping it to the
# invocation matters (a `cd` quoted inside a PR body must not redirect which
# repo a hook inspects), so it now lives in shell_parse.resolve_workdir.


def git_common_dir(cwd=None):
    """The shared .git directory for the repo. In a linked worktree this resolves
    to the MAIN checkout's .git, so every worktree of the same repo maps to the
    same place — the basis for one gate store shared across worktrees.

    Git may return this relative (e.g. ".git" from the main checkout) or absolute
    (from a linked worktree). We canonicalize with realpath so every caller — no
    matter its cwd or which symlinked form of the checkout it sits under (macOS
    /var -> /private/var is the classic trap) — agrees on the SAME string. That
    string identity is load-bearing: the shared store and its sidecar lock are
    keyed by this path, so two spellings of one directory would split both. As a
    bonus, realpath is always absolute, so the dirname() below is never empty."""
    try:
        common = _git("rev-parse", "--git-common-dir", cwd=cwd)
    except (OSError, subprocess.CalledProcessError):
        return None
    # os.path.join drops `base` when `common` is already absolute, so this one
    # line handles both the relative and absolute cases.
    return os.path.realpath(os.path.join(cwd or os.getcwd(), common))


def default_store_path(cwd=None):
    env = os.environ.get("SDLC_GATES_PATH")
    if env:
        return env
    common = git_common_dir(cwd)
    if common:
        # dirname(common .git dir) == the main checkout root, identical for every
        # linked worktree → one shared, branch-keyed store.
        return os.path.join(os.path.dirname(common), ".claude", "data", "gates.json")
    # git resolution failed (not a repo, broken .git, git missing). Fall back to a
    # cwd-relative store — realpath'd for the same single-inode guarantee as the
    # happy path — and leave a breadcrumb, since a silent fallback here is the
    # likeliest cause of a later "my gate didn't record / wasn't enforced" report.
    # `cwd`, not os.getcwd(): callers pass the workdir they resolved out of the
    # command (`cd X && …`, `git -C X`). Using the process's own cwd here sends
    # the recorder and the enforcer to different files without saying so.
    fallback = os.path.realpath(
        os.path.join(cwd or os.getcwd(), ".claude", "data", "gates.json")
    )
    # Name the path: "which file did it actually pick" is the whole question
    # when someone reports a gate that recorded but wasn't enforced.
    sys.stderr.write(
        "[gate] warning: could not resolve the repo's shared git dir; "
        f"falling back to a cwd-local gate store at {fallback}. Gates recorded "
        "here won't be visible from other worktrees/cwds.\n"
    )
    return fallback


def load_store(path):
    """Return the parsed store. Missing file -> {} (legitimately no cycles yet).
    Present-but-unparseable -> StoreCorruptError so callers can fail closed."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except ValueError as e:
        raise StoreCorruptError(f"{path}: {e}") from e


def _store_dir(path):
    # A bare relative path (e.g. "gates.json") has an empty dirname; mkstemp and
    # makedirs both choke on "". Treat it as the current directory.
    return os.path.dirname(path) or "."


_TMP_PREFIX = "gates-"


_TMP_MAX_AGE_S = 3600


def _sweep_orphan_temps(directory):
    # A hook killed at its timeout between mkstemp and os.replace leaves a temp
    # behind, and nothing else ever cleans them up. Temps stranded by <=4.2.0
    # used mkstemp's default `tmp` prefix and are NOT swept — `.claude/data/`
    # is shared, so a bare `tmp*` glob could eat another tool's file. Only our own prefix
    # (`.claude/data/` is a shared namespace), and only files too old for any
    # live writer to own — which is also why this needs no lock.
    cutoff = time.time() - _TMP_MAX_AGE_S
    for name in glob.glob(os.path.join(directory, _TMP_PREFIX + "*.tmp")):
        try:
            if os.path.getmtime(name) < cutoff:
                os.unlink(name)
        except OSError:
            pass


def save_store(path, data):
    directory = _store_dir(path)
    os.makedirs(directory, exist_ok=True)
    # Atomic replace: temp file in the same dir (same filesystem), then rename.
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=_TMP_PREFIX, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass  # never let cleanup mask the original error


@contextmanager
def _exclusive_lock(path):
    """Hold an exclusive lock across a read-modify-write so concurrent hooks
    (auto-record racing a manual --record, two PostToolUse fires) can't lose
    updates. The lock lives on a sidecar file next to the store."""
    directory = _store_dir(path)
    os.makedirs(directory, exist_ok=True)
    # The sidecar lock sits next to the (shared, per-repo) store and is
    # intentionally NEVER deleted: unlinking a flock'd file lets another process
    # create a new inode and lock *that* instead, silently breaking mutual
    # exclusion. Because the store path is canonicalized (realpath) every writer
    # across every worktree locks the same inode here. A 0-byte stale lock is
    # harmless to re-lock, and flock auto-releases on process death.
    lock_path = path + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def update_store(path, mutator):
    """Locked read-modify-write. `mutator(data)` mutates the dict in place; its
    return value is passed back to the caller. The file is only rewritten when
    the data actually changed, so a no-op mutator (untracked skill, idempotent
    re-record) neither creates a spurious empty store nor rewrites timestamps.
    Corruption surfaces as StoreCorruptError (the file is not overwritten).

    The lock is NOT re-entrant — flock attaches to the open file description,
    so a nested update_store() inside a mutator deadlocks against its own
    parent. Keep mutators pure dict work: no nested calls, and no subprocesses
    (a git call in here stalls every other worktree until it returns)."""
    _sweep_orphan_temps(_store_dir(path))  # outside the lock: it needs none
    with _exclusive_lock(path):
        data = load_store(path)
        before = json.dumps(data, sort_keys=True)
        result = mutator(data)
        if json.dumps(data, sort_keys=True) != before:
            save_store(path, data)
        return result


def _now():
    return datetime.now(timezone.utc).isoformat()


def init_gates(data, branch, tier, tier_reason=None):
    """`tier_reason` records WHY this tier was picked, e.g. by
    auto-init-gate-cycle.py's docs-only detection — absence of the key is how
    a reader (format_status, a human editing the store) tells "manually
    chosen" from "auto-detected"; a manual --init passes no reason and the
    key is simply not written, so a plain init produces a byte-identical
    entry to before this field existed."""
    if branch in ("main", "master"):
        raise ValueError(
            f'Refusing to initialize gate cycle for "{branch}". '
            "Gates track feature branches, not the main branch."
        )
    if tier not in TIERS:
        raise ValueError(f'Invalid tier "{tier}". Valid: {", ".join(TIERS)}')
    entry = {"tier": tier, "created_at": _now(), "gates": {}}
    if tier_reason:
        entry["tier_reason"] = tier_reason
    # `--init` doubles as the post-gate reset, so it clears every timestamp. The
    # route is not gate state — it says WHO is driving this branch's cycle — so
    # it survives a reset. Otherwise the mandatory "reset and re-run from gate 1"
    # would silently unhook the driving worktree mid-chain.
    # route_sources, not a raw read: it normalizes a hand-edited scalar and
    # refuses to blow up on a non-dict previous entry (which .get() would, with
    # an AttributeError none of the CLI's handlers catch — a corrupt store
    # should produce a message, not a stack trace).
    previous = route_sources(data.get(branch))
    if previous:
        entry[ROUTE_KEY] = previous
    data[branch] = entry
    return data[branch]


def canonical_worktree_root(cwd=None):
    """The invoking worktree's top-level, canonicalized. This is the routing
    identity: it must be the SAME string whether produced by the command that
    writes the route or by the hook that reads it, so it gets the same realpath
    treatment as the store path (macOS /var -> /private/var)."""
    root = git_root(cwd)
    return os.path.realpath(root) if root else None


def route_sources(branch_data):
    """The worktree roots driving this cycle. Tolerates a hand-edited scalar."""
    if not isinstance(branch_data, dict):
        return []
    value = branch_data.get(ROUTE_KEY)
    if isinstance(value, str):
        return [value]
    return [s for s in value if isinstance(s, str)] if isinstance(value, list) else []


def clear_route(data, source_root):
    """Drop `source_root` from every cycle it drives. Returns the branches it was
    removed from. The key disappears once its last source does, so an unrouted
    cycle is byte-identical to one that was never routed."""
    if not source_root:
        return []
    cleared = []
    for branch in sorted(data):
        bd = data[branch]
        sources = route_sources(bd)
        if source_root not in sources:
            continue
        remaining = [s for s in sources if s != source_root]
        if remaining:
            bd[ROUTE_KEY] = remaining
        else:
            bd.pop(ROUTE_KEY)
        cleared.append(branch)
    return cleared


def set_route(data, branch, source_root):
    """Point `source_root`'s skill gates at `branch`.

    Sources are a LIST, not a scalar. Two sessions may legitimately drive the
    same branch, and with a scalar the second would silently evict the first —
    whose skill gates then fall back to its own branch, which is exactly the
    misrouting this channel exists to prevent. Many sources -> one branch is
    fine; one source -> two branches is not, and clearing first enforces it."""
    if not source_root:
        raise ValueError("Cannot route: could not resolve the invoking worktree root")
    bd = data.get(branch)
    if bd is None:
        raise ValueError(
            f'Cannot route to "{branch}": no gate cycle. Run --init first.'
        )
    clear_route(data, source_root)
    bd[ROUTE_KEY] = route_sources(bd) + [source_root]
    return bd


def routed_branch(data, source_root):
    """(branch, branch_data) driven from `source_root`, or None. Sorted so a
    hand-edited store with duplicate routes still resolves deterministically.

    main/master are skipped for the same reason find_active_cycle skips them:
    init_gates already refuses to create a cycle there, so this is unreachable
    through any supported path — but a hand-edited store shouldn't be able to
    aim an auto-recorder at the trunk."""
    if not source_root:
        return None
    for branch in sorted(data):
        if branch in ("main", "master"):
            continue
        bd = data[branch]
        if source_root in route_sources(bd):
            return branch, bd
    return None


def route_mismatch(data, source_root, branch):
    """The branch `source_root` is routed to, if that's not `branch` — else
    None. Shared by every caller that just needs to say "your skill gates
    land elsewhere", without auto-record-skill-gate.py's additional
    liveness/gate-applicability logic (which needs active_branches and the
    skill being recorded, neither available to a plain status/denial check)."""
    routed = routed_branch(data, source_root)
    if routed and routed[0] != branch:
        return routed[0]
    return None


def record_gate(data, branch, gate, authorized=False):
    """
    Stamp `gate` as complete on `branch`.

    `authorized=True` is the auto-record hook asserting it just watched the
    skill run — the only legitimate way a skill-gated gate gets recorded. The
    check lives here, in the mutator, so every caller is covered: the CLI, the
    hooks, an inline `python3 -c`. The PreToolUse hook reads the Bash command
    and so can only ever see the forms it parses; this sees the real call.

    It guards against an agent talking itself past its own process gate, not
    against an adversary — so a named keyword is enough rigor: any bypass has
    to say `authorized=True` out loud, where a reader will see it.
    """
    if gate not in ALL_GATE_NAMES:
        raise ValueError(
            f'Unknown gate "{gate}". Valid gates: {", ".join(ALL_GATE_NAMES)}'
        )
    if gate in SKILL_FOR_GATE and not authorized:
        raise ValueError(skill_gated_message(gate))
    bd = data.get(branch)
    if bd is None:
        raise ValueError(
            f'No gate cycle for branch "{branch}". Run: record-gate.py --init <tier>'
        )
    if gate not in required_gates(bd):
        # A tier that doesn't require this gate has no business recording
        # it — determine_gate already keeps the auto-record hook from ever
        # asking for one (see its own check), so the only way here is a
        # direct/authorized call. Refusing keeps `gates` a strict subset of
        # required_gates, which every reader (missing_gates, completed_gates,
        # format_status) already assumes rather than re-filters.
        raise ValueError(
            f'"{gate}" is not required by the "{bd.get("tier")}" tier for '
            f'"{branch}". Required: {", ".join(required_gates(bd))}'
        )
    bd.setdefault("gates", {})[gate] = _now()
    return bd


def required_gates(branch_data):
    return GATES_BY_TIER.get(branch_data.get("tier", "small-medium"), ALL_GATE_NAMES)


def missing_gates(branch_data):
    gates = branch_data.get("gates", {})
    return [g for g in required_gates(branch_data) if not gates.get(g)]


def completed_gates(branch_data):
    gates = branch_data.get("gates", {})
    return [g for g in required_gates(branch_data) if gates.get(g)]


def determine_gate(skill, branch_data):
    """Map a completed skill to the gate it records. grumpy:fix is contextual.
    The chain runs review -> fix -> imagine -> fix, so in the canonical order
    post-review is still empty at the first fix and gets filled first; by the
    second fix, post-imagine is the only one open. We check post-imagine first
    only as a tiebreak for the (non-canonical) case where both are pending —
    both gates still end up filled either way."""
    mapped = SKILL_TO_GATE.get(skill)
    if not mapped:
        return None
    if mapped != "grumpy-fix":
        # A tier that doesn't require this gate has no business recording it.
        # This is what keeps a `tiny` cycle from looking like a candidate for
        # `grumpy-review` when the auto-record hook scans other worktrees.
        if branch_data is not None and mapped not in required_gates(branch_data):
            return None
        return mapped
    if branch_data is None:
        return None
    if not set(required_gates(branch_data)) & {
        "grumpy-fix-post-review",
        "grumpy-fix-post-imagine",
    }:
        return None
    gates = branch_data.get("gates", {})
    if gates.get("grumpy-imagine") and not gates.get("grumpy-fix-post-imagine"):
        return "grumpy-fix-post-imagine"
    if gates.get("grumpy-review") and not gates.get("grumpy-fix-post-review"):
        return "grumpy-fix-post-review"
    return None


def format_status(branch_data, branch=None):
    if not branch_data:
        suffix = f' for branch "{branch}"' if branch else ""
        return f"No gate cycle active{suffix}."
    lines = []
    if branch:
        lines.append(f"Branch: {branch}")
    lines.append(
        f"Tier: {branch_data.get('tier')} (started {branch_data.get('created_at')})"
    )
    tier_reason = branch_data.get("tier_reason")
    if tier_reason:
        lines.append(f"  reason: {tier_reason}")
    sources = route_sources(branch_data)
    if sources:
        # A wrong route is the failure mode that used to be invisible; print it.
        lines.append(f"Driven from: {', '.join(sources)}")
    lines.append("")
    gates = branch_data.get("gates", {})
    for g in required_gates(branch_data):
        ts = gates.get(g)
        lines.append(f"  ✓ {g} — {ts}" if ts else f"  ✗ {g}")
    missing = missing_gates(branch_data)
    lines += [
        "",
        "All gates complete." if not missing else f"Missing: {', '.join(missing)}",
    ]
    return "\n".join(lines)


def format_oneline(branch_data):
    if not branch_data:
        return "No SDLC gate cycle active"
    if not missing_gates(branch_data):
        return f"SDLC {branch_data.get('tier')}: all complete"
    gates = branch_data.get("gates", {})
    parts = []
    for g in required_gates(branch_data):
        short = g.replace("grumpy-", "g:").replace("fix-post-", "fix:")
        parts.append(("✓" if gates.get(g) else "✗") + short)
    return f"SDLC {branch_data.get('tier')}: {' '.join(parts)}"
