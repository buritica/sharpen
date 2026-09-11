#!/usr/bin/env python3
"""Host-neutral skill-completion recorder for SDLC gate adapters.

Adapters normalize their host event and call :func:`record_skill_completion`
only after a trusted successful skill lifecycle callback. Hosts without such a
callback must retain the visible, reason-required --attest fallback.
"""

import os

import subprocess

import gate_store as gs


def active_worktree_branches(cwd=None):
    try:
        out = subprocess.check_output(
            ["git", "worktree", "list", "--porcelain"],
            cwd=cwd,
            stderr=subprocess.DEVNULL,
        ).decode()
    except (OSError, subprocess.CalledProcessError):
        return None
    branches = set()
    for line in out.splitlines():
        if line.startswith("branch refs/heads/"):
            branches.add(line[len("branch refs/heads/") :])
    return branches


def _pending_cycles(data, skill, active_branches):
    """Checked-out branches whose cycle still wants the gate this skill records."""
    if active_branches is None:
        return []
    pending = []
    for branch, bd in data.items():
        if branch in ("main", "master") or branch not in active_branches:
            continue
        gate = gs.determine_gate(skill, bd)
        if gate and not bd.get("gates", {}).get(gate):
            pending.append((branch, bd))
    return pending


def find_active_cycle(data, skill, active_branches):
    # active_branches is None only when `git worktree list` failed. In that case
    # we can't confirm which branches are really checked out, so we do NOT scan
    # across branches — guessing would risk stamping a gate on the wrong branch.
    pending = _pending_cycles(data, skill, active_branches)
    if len(pending) != 1:
        # Exactly one candidate, or nothing. With two live sessions on two
        # worktrees, "newest pending" is a coin flip — and a gate stamped on
        # the wrong branch cannot be undone, since skill-gated gates have no
        # manual --record to correct them with. The caller surfaces the
        # ambiguous case (it sets "surprising"); the no-candidates case is the
        # ordinary opt-out and stays quiet.
        return None
    return pending[0]


def handle_skill_completion(
    skill, data, branch, active_branches=None, source_root=None
):
    # `branch` and `source_root` are both resolved by the caller, never here:
    # this runs inside update_store's mutator, and a git subprocess in there
    # holds the repo-wide flock (see gate_store.update_store).
    if skill not in gs.SKILL_TO_GATE:
        return {"recorded": False, "reason": f'"{skill}" is not gate-tracked'}

    # An explicit route beats everything below it. `/sdlc:gate --worktree A`
    # declared, at --init, that this worktree drives branch A's cycle; the skill
    # just ran here on A's behalf. Guessing from cwd instead is exactly the bug
    # this channel exists to close, so we do NOT fall through to the branch
    # heuristics when a route is present — a route that yields no applicable
    # gate reports that, rather than quietly stamping the local branch. Every
    # routed skip is `surprising`: someone explicitly asked for this stamp.
    routed = gs.routed_branch(data, source_root)
    if routed:
        target_branch, bd = routed
        # Read-only questions first, liveness check last: the liveness check
        # only matters when a write is actually about to happen. Checking it
        # first meant a transient `git worktree list` failure interrupted a
        # routed cycle that had *already finished* — the exact renag-forever
        # this diff exists to close, just re-triggered by git flakiness
        # instead of routing. Neither of these two early returns touches
        # active_branches, so a git hiccup can't turn a steady-state no-op
        # into a surprise.
        gate = gs.determine_gate(skill, bd)
        if not gate:
            return {
                "recorded": False,
                "reason": (
                    f'No applicable gate on routed branch "{target_branch}" '
                    "(--unroute to stop routing here)"
                ),
                "surprising": True,
            }
        if bd.get("gates", {}).get(gate):
            # NOT surprising: this is the steady state of a routed cycle that
            # already finished — every later skill invocation on this worktree
            # would otherwise renag forever until someone runs --unroute. The
            # first time a routed stamp lands (or lands on the wrong branch)
            # is loud below; a no-op repeat of that same stamp is not.
            return {
                "recorded": False,
                "reason": f'"{gate}" already recorded on routed "{target_branch}"',
            }
        if active_branches is None:
            # Couldn't enumerate worktrees, so we can't confirm the route's
            # target is still checked out anywhere. Same posture as
            # find_active_cycle below: a safety check that couldn't run is not
            # proof the thing it checks for is fine, so this fails closed too
            # rather than silently stamping on the strength of a stale route.
            return {
                "recorded": False,
                "reason": (
                    f'Routed target "{target_branch}" could not be verified as '
                    "still checked out — `git worktree list` failed, so the "
                    "stale-route check was skipped"
                ),
                "surprising": True,
            }
        if target_branch not in active_branches:
            # The route outlived its branch (removed worktree, deleted branch).
            # Stamping here would land a gate nobody can see or correct — so
            # this is the one routed outcome that must interrupt regardless of
            # what it costs, same reasoning as the stale-target write itself.
            return {
                "recorded": False,
                "reason": (
                    f'Routed target "{target_branch}" is no longer checked out '
                    "anywhere — stale route (--unroute to clear it)"
                ),
                "surprising": True,
            }
        # Routed or not, this hook just watched the skill run.
        invalidated = gs.record_gate_and_diff(
            data, target_branch, gate, authorized=True
        )
        return {
            "recorded": True,
            "gate": gate,
            "target": target_branch,
            "invalidated": invalidated,
        }

    if not branch:
        # Distinct from detached HEAD so the message says which happened, but
        # NOT surfaced: the overwhelmingly common cause is running a skill
        # outside a git repo at all, and surfacing turns every /grumpy:simplify
        # in a scratch directory into an interrupt the caller can't act on. Nothing
        # was lost there — there was no cycle to record against either way.
        return {
            "recorded": False,
            "reason": "could not resolve the current branch (not a git repo?)",
        }

    is_detached = branch == "HEAD"
    is_protected = branch in ("main", "master")
    # Detached HEAD never has its own dict entry to look up (the literal
    # string "HEAD" is not a real branch) — treat it as "no local cycle" so
    # it falls into the SAME cross-worktree fallback as any other branch
    # with none, rather than giving up before ever trying. `git worktree add`
    # routinely leaves the anchor checkout detached, so bailing here used to
    # make the fallback that exists specifically for "can't detect branch
    # from cwd" unreachable in exactly that case. Issue #5.
    bd = None if (is_protected or is_detached) else data.get(branch)
    target_branch = branch

    if not bd:
        resolved = find_active_cycle(data, skill, active_branches)
        if not resolved:
            # Surprising only when there WAS a cycle we declined to pick: an
            # ambiguous cross-worktree match, or a git failure that stopped us
            # looking. A branch with no cycle anywhere is the opt-out, not a
            # problem, and must stay quiet — detached HEAD included, so
            # `git commit --amend` mid-rebase doesn't turn into an interrupt.
            candidates = _pending_cycles(data, skill, active_branches)
            where = "detached HEAD" if is_detached else f'branch "{branch}"'
            if active_branches is None:
                # Couldn't enumerate worktrees, so we declined to guess. Say so —
                # don't claim "no cycle" when the real cause was a git failure.
                reason = (
                    f"No cycle for {where} in this worktree, and "
                    "`git worktree list` failed so cross-worktree lookup was skipped"
                )
            elif candidates:
                reason = (
                    f"No gate cycle for {where}, and "
                    f"{len(candidates)} other checked-out branches have this gate "
                    f"pending ({', '.join(b for b, _ in candidates)}) — refusing "
                    "to guess. Run the skill from the worktree you mean to gate."
                )
            else:
                reason = f"No gate cycle for {where}"
            return {
                "recorded": False,
                "reason": reason,
                "surprising": active_branches is None or bool(candidates),
            }
        target_branch, bd = resolved

    gate = gs.determine_gate(skill, bd)
    if not gate:
        return {
            "recorded": False,
            "reason": "No applicable gate (already recorded or preconditions unmet)",
        }
    if bd.get("gates", {}).get(gate):
        return {"recorded": False, "reason": f'"{gate}" already recorded'}

    # This hook just watched the skill run — the one authorized recorder.
    invalidated = gs.record_gate_and_diff(data, target_branch, gate, authorized=True)
    return {
        "recorded": True,
        "gate": gate,
        "target": target_branch,
        "invalidated": invalidated,
    }


def record_skill_completion(
    *, skill, cwd, succeeded, host, host_version=None, invocation_id=None
):
    """Record a successful normalized host skill-completion event.

    Host adapters must call this only from a trusted post-skill lifecycle event.
    `skill` is the canonical skill identifier, `cwd` identifies the invoking
    worktree, and `succeeded` must describe the invocation result. Optional host
    metadata is returned for adapter diagnostics but is not persisted as gate
    authority. The result is a structured dict; expected no-op outcomes are not
    exceptions.
    """
    if not isinstance(skill, str) or not skill:
        raise ValueError("skill must be a non-empty normalized skill ID")
    if not isinstance(cwd, str) or not cwd or not os.path.isabs(cwd):
        raise ValueError("cwd must be a non-empty absolute canonical worktree path")
    if not isinstance(succeeded, bool):
        raise ValueError("succeeded must be a boolean")
    if not isinstance(host, str) or not host:
        raise ValueError("host must be a non-empty host name")
    if host_version is not None and not isinstance(host_version, str):
        raise ValueError("host_version must be a string when provided")
    if invocation_id is not None and not isinstance(invocation_id, str):
        raise ValueError("invocation_id must be a string when provided")

    result_metadata = {"host": host}
    if host_version is not None:
        result_metadata["host_version"] = host_version
    if invocation_id is not None:
        result_metadata["invocation_id"] = invocation_id
    if not succeeded:
        return {
            "recorded": False,
            "reason": "skill invocation failed",
            **result_metadata,
        }
    if skill not in gs.SKILL_TO_GATE:
        return {
            "recorded": False,
            "reason": f'"{skill}" is not gate-tracked',
            **result_metadata,
        }

    # Normalize paths at the adapter boundary so every subsequent git question
    # and cross-repo registry lookup identifies the same worktree spelling.
    cwd = os.path.realpath(cwd)
    source_root = gs.canonical_worktree_root(cwd)
    cross = gs.resolve_cross_repo_route(source_root, cwd=cwd)
    if cross:
        path = cross["store_path"]
        git_cwd = cross["target_root"]
    else:
        path = gs.default_store_path(cwd)
        git_cwd = cwd
    active = active_worktree_branches(git_cwd)
    branch = gs.detect_branch(cwd)
    holder = {}

    def mutate(store):
        res = handle_skill_completion(
            skill,
            store,
            branch=branch,
            active_branches=active,
            source_root=source_root,
        )
        holder.update(res)
        return res

    try:
        gs.update_store(path, mutate)
    except gs.StoreCorruptError as e:
        return {
            "recorded": False,
            "error": f"gate store unreadable ({e})",
            **result_metadata,
        }
    except ValueError as e:
        return {"recorded": False, "error": str(e), **result_metadata}
    except OSError as e:
        return {
            "recorded": False,
            "error": f"could not lock or write gate store ({e})",
            **result_metadata,
        }
    except Exception as e:
        return {
            "recorded": False,
            "error": f"unexpected error ({e})",
            **result_metadata,
        }
    return {**holder, **result_metadata}
