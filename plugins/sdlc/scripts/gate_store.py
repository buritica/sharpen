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
  2. <main-checkout>/.sharpen/data/gates.json  (dirname of the common .git dir)
  3. <main-checkout>/.claude/data/gates.json   (legacy, read fallback)
  4. <cwd>/.sharpen/data/gates.json            (git resolution failed)
  5. <cwd>/.claude/data/gates.json             (legacy git-resolution fallback)

Schema:
  {
    "<branch>": {
      "tier": "small-medium",
      "created_at": "2026-06-16T04:53:38+00:00",
      "gates": { "tests": "<iso8601>", "lint": "<iso8601>", ... },
      "routed_from": ["/abs/path/to/a/driving/worktree"],   # optional
      "tier_reason": "docs-only diff auto-detected (2 file(s))"   # optional
    }
  }

`routed_from` is the cross-worktree routing channel. `/sdlc:gate --worktree A`
runs from session B, so the PostToolUse auto-record hook — which only ever sees
B's cwd — would otherwise stamp skill gates on B's branch. At `--init` the
command writes B's worktree root here, and the hook resolves its own root and
prefers the branch routed from it over `detect_branch()`. A channel, not a
heuristic: it works with any number of live worktrees.

`tier_reason` records WHY a tier was picked (e.g. auto-init-gate-cycle.py's
docs-only detection). Its absence is the signal that a tier was chosen
manually (via `--init`) rather than auto-detected — it is never backfilled
for a manual choice.
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

# Portable profile names accepted on stored cycle metadata. This is storage
# validation only; profile-to-gate mapping is a separate runtime decision and
# deliberately does not change required_gates() here.
PROFILE_NAMES = ("baseline", "review", "adversarial")

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
        # Deliberately NOT mentioning --attest here. This message fires on
        # every manual --record of a skill-gated gate, including the very
        # first attempt before the skill has ever run — surfacing the
        # attestation escape hatch in that hot path would read as "here's
        # your actual next step" rather than the rare, reasoned last resort
        # it's meant to be (see gate.md's own section on it). It stays
        # documented there and in attest_gate's docstring, not advertised at
        # the exact moment an agent is looking for the fastest way through.
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


def branch_exists(branch, cwd=None):
    """Does `branch` name a real ref (local, or a remote-tracking branch) in
    the repo at `cwd`?

    `--branch <name>` lets a caller record gates for a branch it isn't
    currently on (the documented cross-worktree pattern), but that trust is
    unconditional: nothing has ever verified the name is real in the repo
    `cwd` resolves to. Pair this with a repo-mismatch check at `--init` —
    `--branch` alone can't tell "this worktree, a different branch" from
    "this string, a repo that's never heard of it" — see gate.md's "cannot
    target a different repository" section (sharpen#16) — and only the
    second one is a mistake worth stopping for.

    Checks `refs/remotes/*/<branch>` too, not just `refs/heads/<branch>`: a
    branch fetched but never locally checked out (a fresh clone, a PR branch
    a CI job pulled without `git checkout -b`) is still a genuine branch in
    this repo, not the "never heard of it" case this guard exists to catch —
    treating it as nonexistent would be a false refusal of a real topology.
    """
    try:
        # No --quiet: `_git` already sends stderr to DEVNULL, and success/
        # failure here is read from the exit code (CalledProcessError), never
        # git's own output, so --quiet would only suppress a message nobody
        # was going to see anyway.
        _git("rev-parse", "--verify", f"refs/heads/{branch}", cwd=cwd)
        return True
    except (OSError, subprocess.CalledProcessError):
        pass
    try:
        out = _git(
            "for-each-ref", "--format=%(refname)", f"refs/remotes/*/{branch}", cwd=cwd
        )
        return bool(out.strip())
    except (OSError, subprocess.CalledProcessError):
        return False


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


def state_data_root(cwd=None):
    """Return the shared neutral `.sharpen/data` root.

    `state_file_path` retains an existing legacy file on a per-file basis. The
    root helper deliberately has no migration policy so new portable state files
    do not inherit a `.claude` dependency merely because that directory exists.
    """
    common = git_common_dir(cwd)
    if common:
        # dirname(common .git dir) == the main checkout root, identical for every
        # linked worktree → one shared, branch-keyed state root.
        root = os.path.dirname(common)
    else:
        root = os.path.realpath(cwd or os.getcwd())
    return os.path.join(root, ".sharpen", "data")


def state_file_path(filename, env_var, cwd=None):
    """Resolve state with an explicit override and per-file legacy fallback.

    Prefer an already-existing neutral file. Otherwise retain an existing legacy
    file so creating unrelated neutral state cannot hide an active gate cycle.
    A missing file resolves to the neutral location for new writes.
    """
    env = os.environ.get(env_var)
    if env:
        return env
    neutral = os.path.join(state_data_root(cwd), filename)
    if os.path.exists(neutral):
        return neutral
    root = os.path.dirname(os.path.dirname(state_data_root(cwd)))
    legacy = os.path.join(root, ".claude", "data", filename)
    if os.path.exists(legacy):
        return legacy
    return neutral


def default_store_path(cwd=None):
    env = os.environ.get("SDLC_GATES_PATH")
    if env:
        return env
    common = git_common_dir(cwd)
    if not common:
        # git resolution failed (not a repo, broken .git, git missing). Fall back
        # to a cwd-relative store and leave a breadcrumb, since a silent fallback
        # here is the likeliest cause of a later "my gate didn't record / wasn't
        # enforced" report. `cwd`, not os.getcwd(): callers pass the workdir they
        # resolved out of the command (`cd X && …`, `git -C X`). Using the
        # process's own cwd here sends the recorder and the enforcer to different
        # files without saying so.
        fallback = state_file_path("gates.json", "SDLC_GATES_PATH", cwd)
        sys.stderr.write(
            "[gate] warning: could not resolve the repo's shared git dir; "
            f"falling back to a cwd-local gate store at {fallback}. Gates recorded "
            "here won't be visible from other worktrees/cwds.\n"
        )
        return fallback
    return state_file_path("gates.json", "SDLC_GATES_PATH", cwd)


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


def init_gates(data, branch, tier, tier_reason=None, profile=None, capabilities=None):
    """Initialize a branch cycle while preserving legacy state shape by default.

    `tier_reason` records WHY this tier was picked, e.g. by
    auto-init-gate-cycle.py's docs-only detection — absence of the key is how
    a reader (format_status, a human editing the store) tells "manually
    chosen" from "auto-detected"; a manual --init passes no reason and the
    key is simply not written, so a plain init produces a byte-identical
    entry to before this field existed.

    `profile` and `capabilities` are optional portable-core metadata. They are
    written only when a caller explicitly resolves a manifest, so legacy init
    behavior remains unchanged and pre-profile cycles continue to load.
    """
    if branch in ("main", "master"):
        raise ValueError(
            f'Refusing to initialize gate cycle for "{branch}". '
            "Gates track feature branches, not the main branch."
        )
    if tier not in TIERS:
        raise ValueError(f'Invalid tier "{tier}". Valid: {", ".join(TIERS)}')
    if profile is not None and profile not in PROFILE_NAMES:
        raise ValueError(
            f'Invalid profile "{profile}". Valid: {", ".join(PROFILE_NAMES)}'
        )
    if capabilities is not None and not isinstance(capabilities, list):
        raise ValueError("capabilities must be a list of capability names")
    entry = {"tier": tier, "created_at": _now(), "gates": {}}
    if profile is not None:
        entry["profile"] = profile
    if capabilities is not None:
        entry["capabilities"] = sorted(capabilities)
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


def has_any_route(data):
    """Cheap in-memory check: does ANY branch in the store have a route?
    `canonical_worktree_root()` (what a caller needs to then call
    route_mismatch/route_mismatch_note) is a `git rev-parse` subprocess —
    callers that would only spawn it to find nothing routed (e.g. every
    plain `--status` call on a repo that's never used routing) should check
    this first and skip the spawn entirely."""
    return any(route_sources(bd) for bd in data.values())


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


def route_mismatch_note(data, source_root, branch):
    """Ready-to-append sentence when `source_root`'s skill gates route to a
    branch other than `branch`, else None. One canonical wording shared by
    every caller (enforce-sdlc-gates.py's denial, record-gate.py's --status)
    so a future tweak doesn't have to be remembered in more than one place."""
    elsewhere = route_mismatch(data, source_root, branch)
    if not elsewhere:
        return None
    return (
        f'This worktree\'s skill gates are routed to "{elsewhere}", not '
        f'"{branch}" (/sdlc:gate --unroute to stop).'
    )


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

    Also raises if `gate` isn't required by `branch`'s tier. Defense in
    depth, not a live path today: the skill-gated check above already
    catches every unauthorized attempt at a mismatched gate first (every
    bash gate the CLI can record without authorization is required by every
    tier), and the one `authorized=True` caller (auto-record-skill-gate.py)
    already filters through `determine_gate`'s own `required_gates` check
    before ever getting here. Exists so the NEXT `authorized=True` caller
    that skips that filtering fails loudly instead of silently polluting
    `gates` with a key every reader (missing_gates, completed_gates,
    format_status) assumes is a strict subset of `required_gates`.
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
    required = required_gates(bd)
    if gate not in required:
        # A tier that doesn't require this gate has no business recording
        # it — determine_gate already keeps the auto-record hook from ever
        # asking for one (see its own check), so the only way here is a
        # direct/authorized call. Refusing keeps `gates` a strict subset of
        # required_gates, which every reader (missing_gates, completed_gates,
        # format_status) already assumes rather than re-filters.
        raise ValueError(
            f'"{gate}" is not required by the "{bd.get("tier")}" tier for '
            f'"{branch}". Required: {", ".join(required)}'
        )
    bd.setdefault("gates", {})[gate] = _now()
    return bd


def attest_gate(data, branch, gate, reason):
    """Stamp a skill-gated `gate` on human attestation, not a hook observation.

    Last-resort escape hatch for a reported gap (sharpen#11): in a long
    session, the Skill tool can return "already loaded above, instructions
    unchanged" for a skill invoked again — a Claude Code caching behavior —
    and PostToolUse can fail to fire for that call, so auto-record-skill-gate.py
    doesn't run even though the skill's instructions genuinely executed (see
    that script's docstring; how consistently this reproduces isn't fully
    pinned down). The documented way around it is a fresh subagent dispatch
    (a clean context re-runs the Skill tool for real); this exists for when
    that route was already tried, or genuinely isn't practical.

    Deliberately not the same code path as the hook's `authorized=True`: this
    requires a human-legible `reason` and marks the stamp in `attestations` so
    `--status`/`--oneline` render it with a different marker than a
    hook-verified gate. It attests the operator's claim that the skill ran,
    not this process's own observation of it — the distinction the reader of
    `--status` needs in order to judge it, same reasoning as `authorized=True`
    itself: any bypass has to say so out loud, where it will be seen.

    That distinction is for the reader, not the enforcer: an attestation
    writes the same timestamp into `gates` that a hook-verified stamp would,
    so `missing_gates()` (and therefore `gh pr create` via
    enforce-sdlc-gates.py) treats the two identically. `attestations` is
    metadata for a human reading `--status`/`--oneline`, not a weaker gate.

    Refuses on a bash-verifiable gate (`tests`, `lint`, `typecheck`): those
    already have a legitimate manual `--record`, so an attestation would only
    be a second, weaker way to do the same thing.

    Refuses when `gate` is already stamped, even by an attestation. Without
    this, attesting an already hook-verified gate would silently overwrite a
    genuine timestamp with a fabricated one and downgrade real verification
    history to merely-attested — the opposite of what this function is for.
    A gate that's already done needs no attestation; if it's wrong, `--init`
    resets it the same as any other gate.
    """
    if gate not in SKILL_FOR_GATE:
        raise ValueError(
            f'"{gate}" is not skill-gated, so it has no attestation path — '
            f"record it directly instead: --record {gate}\n\n" + gate_lists_hint()
        )
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(
            "--attest requires --reason \"<why the skill's run could not be "
            'observed>" — the whole point is that the bypass is legible, not silent'
        )
    bd = data.get(branch)
    if bd is None:
        raise ValueError(
            f'No gate cycle for branch "{branch}". Run: record-gate.py --init <tier>'
        )
    if bd.get("gates", {}).get(gate):
        raise ValueError(
            f'"{gate}" is already recorded for "{branch}" — nothing to attest. '
            "If it's wrong, --init resets the cycle."
        )
    bd = record_gate(data, branch, gate, authorized=True)
    stamp = bd["gates"][gate]
    bd.setdefault("attestations", {})[gate] = {"reason": reason.strip(), "at": stamp}
    return bd


def _gate_marker(stamped, attested):
    """The one shared "attested vs. hook-verified vs. missing" classification,
    so format_status and format_oneline can't render it two different ways."""
    if stamped and attested:
        return "⚠"
    return "✓" if stamped else "✗"


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
    profile = branch_data.get("profile")
    if profile:
        capabilities = branch_data.get("capabilities")
        if capabilities:
            lines.append(f"Profile: {profile} ({', '.join(capabilities)})")
        else:
            lines.append(f"Profile: {profile}")
    report = branch_data.get("review_report")
    if isinstance(report, dict):
        provenance = report.get("provenance", {})
        location = provenance.get("kind", "unknown")
        if provenance.get("kind") == "git-range":
            location += f" {provenance.get('base')}...{provenance.get('head')}"
        findings = report.get("findings")
        finding_count = len(findings) if isinstance(findings, list) else 0
        lines.append(
            f"Review report: {report.get('status', 'unknown')} "
            f"({location}; {finding_count} finding(s))"
        )
    sources = route_sources(branch_data)
    if sources:
        # A wrong route is the failure mode that used to be invisible; print it.
        lines.append(f"Driven from: {', '.join(sources)}")
    lines.append("")
    gates = branch_data.get("gates", {})
    attestations = branch_data.get("attestations", {})
    for g in required_gates(branch_data):
        ts = gates.get(g)
        att = attestations.get(g)
        mark = _gate_marker(ts, att)
        if ts and att:
            # Tolerates a hand-edited store where `attestations[g]` isn't the
            # shape attest_gate writes (missing/non-dict) — same posture as
            # route_sources for a hand-edited scalar: degrade the message,
            # don't throw.
            reason = att.get("reason") if isinstance(att, dict) else None
            detail = f": {reason}" if reason else ""
            lines.append(f"  {mark} {g} — {ts} (attested, not hook-verified{detail})")
        elif ts:
            lines.append(f"  {mark} {g} — {ts}")
        else:
            lines.append(f"  {mark} {g}")
    missing = missing_gates(branch_data)
    lines += [
        "",
        "All gates complete." if not missing else f"Missing: {', '.join(missing)}",
    ]
    return "\n".join(lines)


def format_oneline(branch_data):
    if not branch_data:
        return "No SDLC gate cycle active"
    profile = branch_data.get("profile")
    label = branch_data.get("tier")
    if profile:
        label = f"{label}/{profile}"
    report = branch_data.get("review_report")
    if isinstance(report, dict) and report.get("status"):
        label = f"{label}/review:{report['status']}"
    if not missing_gates(branch_data):
        return f"SDLC {label}: all complete"
    gates = branch_data.get("gates", {})
    attestations = branch_data.get("attestations", {})
    parts = []
    for g in required_gates(branch_data):
        short = g.replace("grumpy-", "g:").replace("fix-post-", "fix:")
        parts.append(_gate_marker(gates.get(g), attestations.get(g)) + short)
    return f"SDLC {label}: {' '.join(parts)}"
