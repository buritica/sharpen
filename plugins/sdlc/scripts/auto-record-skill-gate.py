#!/usr/bin/env python3
"""
PostToolUse hook: auto-record SDLC gates after a gate-tracked skill completes.

When grumpy:simplify / grumpy:review / grumpy:imagine / grumpy:fix finishes, record the
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

**Known gap: this hook can only fire on a genuine tool call.** In a long
session, re-invoking a skill already loaded earlier in the conversation can
make the Skill tool return "already loaded above, instructions unchanged"
instead of dispatching fresh. Reported behavior (sharpen#11) is that when this
happens with no real tool call underneath it, PostToolUse does not fire, so
this hook does not run, even though the skill's instructions genuinely
executed — though it is not yet fully understood how consistently that holds
across harness versions and sessions; treat "the hook didn't fire" as a
symptom to investigate, not a guarantee tied to this exact wording. Either
way, there is nothing this hook itself can do about it: if the underlying tool
call never happened, this script never runs to detect anything. The documented
ways through: re-run the skill from a fresh subagent (a clean context has no
cached instructions to short-circuit — see /sdlc:gate's --worktree/--route-from
routing for wiring its gate to the right branch), or, last resort,
`record-gate.py --attest <gate> --reason <text>` (gate_store.attest_gate) — a
separate, reason-required path that marks its stamp as human-attested rather
than hook-verified.

Repo resolution, BEFORE any of the branch resolution below even starts
(sharpen#10): if the payload's `cwd` is the SOURCE of a registered cross-repo
route (gate_store.resolve_cross_repo_route), every git question in this file
— which store to open, which branch is checked out where — is asked about
the TARGET repo, not `cwd`'s own repo. Skipped entirely for the ordinary case
(no cross-repo route from `cwd`), which is the overwhelming majority of
invocations and costs nothing beyond the one registry lookup.

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
        before = set(bd.get("gates", {}))
        gs.record_gate(data, target_branch, gate, authorized=True)
        invalidated = sorted(before - set(bd.get("gates", {})))
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
    before = set(bd.get("gates", {}))
    gs.record_gate(data, target_branch, gate, authorized=True)
    invalidated = sorted(before - set(bd.get("gates", {})))
    return {
        "recorded": True,
        "gate": gate,
        "target": target_branch,
        "invalidated": invalidated,
    }


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
    if skill in gs.RENAMED_SKILLS:
        # Distinct from the ordinary "untracked skill" quiet return below:
        # this name used to record a gate and a caller may reasonably expect
        # it still does. Surface it so the gap doesn't read as a gate that
        # simply never got attempted.
        new_name = gs.RENAMED_SKILLS[skill]
        return ho.notify(
            "auto-record",
            f'"{skill}" was replaced by "{new_name}" — that gate did not '
            f"record. Run {new_name} instead.",
            surface=True,
        )
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
    source_root = gs.canonical_worktree_root(cwd)
    # A cross-repo route (sharpen#10) means `cwd` is the SOURCE of a route
    # whose actual target is a different repository — `default_store_path(cwd)`
    # would resolve to the SOURCE's own store (via ITS OWN git-common-dir),
    # which never has, and never could have, the target's cycle in it: they're
    # different files. Check the registry BEFORE falling back to the local
    # resolution, so this is the one place that decides which repo's git state
    # everything below asks about — not just which store file to open.
    cross = gs.resolve_cross_repo_route(source_root, cwd=cwd)
    if cross:
        path = cross["store_path"]
        git_cwd = cross["target_root"]
    else:
        path = gs.default_store_path(cwd)
        git_cwd = cwd
    # All git calls happen BEFORE the lock: update_store holds an exclusive
    # flock shared by every worktree of the repo, and a stalled git inside it
    # stalls all of them. Tradeoff: `active`/`branch` are a snapshot from before
    # the lock, while `data` is read fresh once inside it — so a race between two
    # detached-HEAD worktrees both adopting the same pending branch can leave the
    # loser's reason string stale ("no cycle" when really "someone else just took
    # it"). No double-stamp results (the lock still serializes the actual write),
    # only a misleading diagnostic in that one rare window. Detached HEAD is
    # `git worktree add`'s normal state, so this window is now a mainline path
    # rather than only reachable through named-branch ambiguity.
    #
    # `active_worktree_branches` runs against `git_cwd` — the TARGET repo's own
    # root on a cross-repo route — because "is this branch checked out
    # anywhere" is a question about the target's worktrees, and `cwd` (the
    # source) has no idea the target repo even exists.
    active = active_worktree_branches(git_cwd)
    # Deliberately still `cwd` (the source), not `git_cwd`, on a cross-repo
    # route: `branch` is never consulted for correctness on that path — a
    # route, once found, resolves the target branch entirely on its own
    # (see handle_skill_completion) — it's used only for the "is this a
    # cross-worktree stamp worth surfacing" display comparison against
    # `holder["target"]` below, where the meaningful question is "what was
    # the SOURCE session actually on," not what happens to be checked out in
    # some worktree of the target.
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
        # This skill's own pass criterion can edit source files (see
        # gate_store.CODE_MUTATING_GATES), so any already-stamped
        # tests/lint/typecheck just got cleared — surface it, the same as a
        # cross-worktree write: it's a change to the store nobody watching
        # this session would otherwise see, and unlike a cross-worktree
        # stamp it also means the PR is further from ready than the reader
        # last checked.
        invalidated = holder.get("invalidated") or []
        invalidated_note = (
            f" — this fixes code inline, so {', '.join(invalidated)} no "
            "longer verify the current state and must run again"
            if invalidated
            else ""
        )
        return ho.notify(
            "auto-record",
            f'recorded "{holder["gate"]}"{where} after {skill}{invalidated_note}',
            surface=cross_worktree or bool(invalidated),
        )
    if holder.get("reason"):
        # Surfaced only when the skip is one the caller had no way to predict,
        # i.e. it set "surprising": a stale or exhausted route, a routed
        # branch with no applicable gate, and an ambiguous cross-worktree
        # match. NOT every skip on a routed branch — the already-recorded case
        # deliberately omits "surprising" (see above) since it's the steady
        # state of a finished routed cycle, not news. Everything else is an
        # ordinary outcome of asking — no cycle here, a gate already recorded,
        # no applicable gate, detached HEAD, not a git repo — and stays quiet
        # so an ungated repo doesn't nag on every skill run. Note auto-init
        # makes the opposite call for a non-repo cwd, because there a commit
        # really did go ungated; here there was never a cycle to record
        # against.
        return ho.notify(
            "auto-record",
            f"skipped {skill}: {holder['reason']}",
            surface=holder.get("surprising", False),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
