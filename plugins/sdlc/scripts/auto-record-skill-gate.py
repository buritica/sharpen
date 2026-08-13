#!/usr/bin/env python3
"""
PostToolUse hook: auto-record SDLC gates after a gate-tracked skill completes.

When simplify / grumpy:review / grumpy:imagine / grumpy:fix finishes, record the
corresponding gate in the JSON store. This is the ONLY path to record skill-gated
gates: gate_store.record_gate() refuses them unless the caller passes
`authorized=True`, and this hook is the only caller that does.
(block-direct-gate-record.py blocks the obvious manual spellings earlier, with a
better message, but the store is what holds.)

**What this actually attests, and what it does not.** PostToolUse for a Skill
call fires when the *tool* returns — and a Skill tool call returns the skill's
instructions, not its results. So this hook can attest that the skill was
invoked and did not error. It cannot attest that the agent then followed the
instructions, or that a review found anything. An agent determined to fake a
gate can invoke the skill and ignore it; the guard is against talking yourself
past your own process, not against an adversary. Below, we at least refuse to
stamp when the tool call itself reported an error — a skill that errored plainly
did not run.

Branch resolution, in order:
  1. An explicit route (gate_store.ROUTE_KEY) from this worktree — written by
     `/sdlc:gate --worktree <path> --init`. Wins outright; see the note in
     handle_skill_completion.
  2. This worktree's own branch, if it has a cycle.
  3. The single other checked-out branch with a pending cycle, if unambiguous.

Pure stdlib. @fires-on Skill tool (PostToolUse)
"""

import json
import subprocess
import sys

import gate_store as gs
import hook_out as ho


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
        if active_branches is not None and target_branch not in active_branches:
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
        # Routed or not, this hook just watched the skill run.
        gs.record_gate(data, target_branch, gate, authorized=True)
        return {"recorded": True, "gate": gate, "target": target_branch}

    if branch == "HEAD":
        # Detached HEAD: routine mid-rebase, and there is no branch to key a
        # gate to. Nothing was lost, so it stays quiet — same call auto-init
        # makes for the same state.
        return {
            "recorded": False,
            "reason": "detached HEAD — no branch to record against",
        }
    if not branch:
        # Distinct from detached HEAD so the message says which happened, but
        # NOT surfaced: the overwhelmingly common cause is running a skill
        # outside a git repo at all, and surfacing turns every /simplify in a
        # scratch directory into an interrupt the caller can't act on. Nothing
        # was lost there — there was no cycle to record against either way.
        return {
            "recorded": False,
            "reason": "could not resolve the current branch (not a git repo?)",
        }

    is_protected = branch in ("main", "master")
    bd = None if is_protected else data.get(branch)
    target_branch = branch

    if not bd:
        resolved = find_active_cycle(data, skill, active_branches)
        if not resolved:
            # Surprising only when there WAS a cycle we declined to pick: an
            # ambiguous cross-worktree match, or a git failure that stopped us
            # looking. A branch with no cycle anywhere is the opt-out, not a
            # problem, and must stay quiet.
            candidates = _pending_cycles(data, skill, active_branches)
            if active_branches is None:
                # Couldn't enumerate worktrees, so we declined to guess. Say so —
                # don't claim "no cycle" when the real cause was a git failure.
                reason = (
                    f'No cycle for "{branch}" in this worktree, and '
                    "`git worktree list` failed so cross-worktree lookup was skipped"
                )
            elif candidates:
                reason = (
                    f'No gate cycle for branch "{branch}", and '
                    f"{len(candidates)} other checked-out branches have this gate "
                    f"pending ({', '.join(b for b, _ in candidates)}) — refusing "
                    "to guess. Run the skill from the worktree you mean to gate."
                )
            else:
                reason = f'No gate cycle for branch "{branch}"'
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
    gs.record_gate(data, target_branch, gate, authorized=True)
    return {"recorded": True, "gate": gate, "target": target_branch}


def main():
    try:
        data_in = json.load(sys.stdin)
    except Exception as e:
        # Same reasoning as the sibling hooks: if the payload shape ever
        # changes, this hook stops recording gates for every skill run in every
        # repo, and a bare return 0 leaves no trace of that anywhere.
        ho.emit(ho.warn("auto-record", f"could not parse hook stdin ({e}), skipping"))
        return 0
    if data_in.get("tool_name") != "Skill":
        return 0
    skill = data_in.get("tool_input", {}).get("skill")
    if not skill or skill not in gs.SKILL_TO_GATE:
        return 0  # untracked skill — don't even touch the store file

    # A Skill call that errored did not run. Stamping it would record a gate for
    # work that demonstrably did not happen — the one case we can actually
    # detect from the payload, so detect it.
    resp = data_in.get("tool_response")
    if isinstance(resp, dict) and resp.get("is_error"):
        return 0

    # The harness reports the session's cwd on the payload; prefer it over this
    # process's cwd so every git question below is asked of the right worktree.
    cwd = data_in.get("cwd") or None
    path = gs.default_store_path(cwd)
    # All three git calls happen BEFORE the lock: update_store holds an exclusive
    # flock shared by every worktree of the repo, and a stalled git inside it
    # stalls all of them.
    active = active_worktree_branches(cwd)
    branch = gs.detect_branch(cwd)
    source_root = gs.canonical_worktree_root(cwd)
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
        # Locked read-modify-write so a concurrent record can't clobber us.
        gs.update_store(path, mutate)
    except gs.StoreCorruptError as e:
        return ho.notify("auto-record", f"skipped {skill}: gate store unreadable ({e})")
    except ValueError as e:
        # record_gate's own refusals. Unreachable today — determine_gate only
        # returns real gate names and this hook passes authorized=True — but a
        # PostToolUse hook should skip with a line, never traceback at the user.
        return ho.notify("auto-record", f"skipped {skill}: {e}")
    except OSError as e:
        return ho.notify(
            "auto-record",
            f"skipped {skill}: could not lock or write gate store "
            f"(filesystem without flock support?) ({e})",
        )
    except Exception as e:
        # Same posture as the sibling hooks: never wedge or traceback at the
        # user over a bug in here.
        return ho.notify("auto-record", f"skipped {skill}: unexpected error ({e})")

    if holder.get("recorded"):
        # A stamp on the local branch is the expected outcome and asks nothing
        # of the reader — quiet. A stamp that landed anywhere else (an explicit
        # route, or the single-candidate cross-worktree adoption) is a write
        # nobody watching this session would otherwise see, and it cannot be
        # corrected after the fact (skill-gated gates have no manual --record),
        # so it surfaces. Keyed off target != branch rather than `routed` alone
        # so it also catches the adoption path a `routed`-only check would miss.
        cross_worktree = holder.get("target") != branch
        where = f" on {holder['target']}" if cross_worktree else ""
        return ho.notify(
            "auto-record",
            f'recorded "{holder["gate"]}"{where} after {skill}',
            surface=cross_worktree,
        )
    if holder.get("reason"):
        # Surfaced only when the skip is one the caller had no way to predict,
        # i.e. it set "surprising": any skip on an explicitly routed branch
        # (someone asked for that stamp by name), and an ambiguous
        # cross-worktree match. Everything else is an ordinary outcome of
        # asking — no cycle here, a gate already recorded, no applicable gate,
        # detached HEAD, not a git repo — and stays quiet so an ungated repo
        # doesn't nag on every skill run. Note auto-init makes the opposite
        # call for a non-repo cwd, because there a commit really did go
        # ungated; here there was never a cycle to record against.
        return ho.notify(
            "auto-record",
            f"skipped {skill}: {holder['reason']}",
            surface=holder.get("surprising", False),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
