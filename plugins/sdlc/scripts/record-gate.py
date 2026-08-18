#!/usr/bin/env python3
"""
CLI to record SDLC gate completions to a JSON file shared across all worktrees
of the repo, keyed by branch.

Usage:
  record-gate.py --init <tier>          # start a gate cycle for this branch
  record-gate.py --record <gate-name>   # record a gate completion
  record-gate.py --status               # print current state (also names
                                        # where THIS worktree's own skill
                                        # gates route to, if elsewhere)
  record-gate.py --oneline              # compact one-line status
  record-gate.py --unroute              # stop driving another worktree's gates
  record-gate.py --branch <name>        # override auto-detected branch
  record-gate.py --route-from <path>    # with --init: route <path>'s skill
                                        # gates to --branch (cross-worktree)

Store path: $SDLC_GATES_PATH, or <main-checkout>/.claude/data/gates.json
            (resolved via `git rev-parse --git-common-dir`, shared per-repo)
"""

import sys

import gate_store as gs


def _log(msg):
    sys.stderr.write(msg + "\n")


def _init_with_route(data, branch, tier, route_to_set, route_to_clear):
    """Init the cycle, apply this invocation's routing decision, and report the
    branches it stopped driving as `(branch_data, dropped)`.

    Both roots arrive pre-resolved: this runs inside update_store's mutator, and
    a git subprocess in there holds the repo-wide flock. `_resolve_routing` does
    the git work before the lock is taken.

    `dropped` exists so the caller can say so out loud. Revoking a route moves
    where this worktree's skill gates land, and an unannounced move is the exact
    failure this whole channel was built to stop.
    """
    bd = gs.init_gates(data, branch, tier)
    dropped = gs.clear_route(data, route_to_clear) if route_to_clear else []
    if route_to_set:
        gs.set_route(data, branch, route_to_set)
    return bd, [b for b in dropped if b != branch]


def _resolve_routing(branch, route_from):
    """(route_to_set, route_to_clear) for this `--init`, or raises ValueError.

    Two lifecycle events, both anchored on `--init` because that is the only
    moment the caller declares what it is driving:
      * `--route-from <path>` — <path>'s skill gates now land on `branch`.
      * plain `--init` for the caller's OWN branch — the caller has stopped
        driving someone else, so any route it left behind is dropped. Without
        this a stale route would keep redirecting its gates to a branch it
        walked away from.
    A plain `--init` for some OTHER branch touches no routing: init_gates
    preserves the existing route, which is what makes the post-gate reset safe.
    """
    if route_from:
        source = gs.canonical_worktree_root(route_from)
        if not source:
            raise ValueError(f'--route-from "{route_from}" is not inside a git repo')
        if gs.detect_branch(route_from) == branch:
            # Routing a worktree to the branch it already has checked out is a
            # no-op that would only go stale on the next branch switch.
            return None, source
        return source, None
    # No --route-from. Only a plain init of this worktree's OWN branch is the
    # "I've stopped driving someone else" signal, so detect_branch is asked for
    # here and nowhere else — main() must keep short-circuiting on --branch,
    # which the gate chain always passes.
    if gs.detect_branch() == branch:
        return None, gs.canonical_worktree_root()
    return None, None


def main(argv):
    branch_override = None
    route_from = None
    rest = []
    i = 0
    while i < len(argv):
        if argv[i] == "--branch" and i + 1 < len(argv):
            branch_override = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--route-from" and i + 1 < len(argv):
            route_from = argv[i + 1]
            i += 2
            continue
        rest.append(argv[i])
        i += 1

    command = rest[0] if rest else None
    path = gs.default_store_path()

    if command == "--unroute":
        # Routing is keyed by the invoking worktree, so this needs no branch —
        # and must work from a detached HEAD, hence its place above the check.
        source = gs.canonical_worktree_root()
        if not source:
            _log("[gate] error: --unroute must run inside a git worktree")
            return 1
        try:
            cleared = gs.update_store(path, lambda d: gs.clear_route(d, source))
        except (gs.StoreCorruptError, OSError) as e:
            _log(f"[gate] error: {e}")
            return 1
        _log(
            f"[gate] no longer driving gates for: {', '.join(cleared)}"
            if cleared
            else "[gate] this worktree was not driving another worktree's gates"
        )
        return 0

    branch = branch_override or gs.detect_branch()
    if not branch or branch == "HEAD":
        sys.stderr.write(
            "[gate] cannot record gate: not on a named branch (detached HEAD?)\n"
        )
        return 1

    try:
        if command == "--init":
            tier = rest[1] if len(rest) > 1 else None
            # Resolved before update_store takes the repo-wide flock.
            route_to_set, route_to_clear = _resolve_routing(branch, route_from)
            existing = gs.load_store(path).get(branch)
            had = gs.completed_gates(existing) if existing else []
            bd, dropped = gs.update_store(
                path,
                lambda d: _init_with_route(
                    d, branch, tier, route_to_set, route_to_clear
                ),
            )
            _log(f"[gate] initialized {tier} cycle for {branch} at {bd['created_at']}")
            if dropped:
                _log(
                    f"[gate] this worktree no longer drives gates for "
                    f"{', '.join(dropped)}; skill gates now record on {branch}"
                )
            if had:
                # --init overwrites. Skill-gated gates can only be re-earned by
                # re-running their skill, so say what this just cost.
                _log(
                    f"[gate] reset {len(had)} recorded gate(s): {', '.join(had)}. "
                    "The skill-gated ones need their skills run again."
                )
            sources = gs.route_sources(bd)
            if sources:
                _log(
                    f"[gate] skill gates run from {', '.join(sources)} will "
                    f"record on {branch} (--unroute to stop)"
                )
        elif command == "--record":
            if len(rest) < 2:
                _log("Usage: --record <gate-name>")
                return 1
            gate = rest[1]
            bd = gs.update_store(path, lambda d: gs.record_gate(d, branch, gate))
            remaining = gs.missing_gates(bd)
            done = (
                "All gates complete."
                if not remaining
                else f"Remaining: {', '.join(remaining)}"
            )
            _log(f"[gate] ✓ {gate} recorded for {branch}. {done}")
        elif command == "--status":
            data = gs.load_store(path)
            out = gs.format_status(data.get(branch), branch)
            # `format_status`'s own "Driven from:" line only reaches whoever
            # asks about the branch being routed TO. The source side — the
            # worktree actually doing the driving — had no way to learn this
            # except by inference (a branch with no cycle here) or by reading
            # --unroute's output. Say it plainly whenever this worktree's
            # skill gates land somewhere other than the branch being asked
            # about, regardless of whether that branch has a cycle at all.
            note = None
            if gs.has_any_route(data):
                # Skip the git subprocess spawn (canonical_worktree_root)
                # entirely on a repo that's never used routing — the
                # overwhelmingly common case for a plain --status call.
                note = gs.route_mismatch_note(
                    data, gs.canonical_worktree_root(), branch
                )
            if note:
                out += f"\n\n{note}"
            sys.stdout.write(out + "\n")
        elif command == "--oneline":
            sys.stdout.write(gs.format_oneline(gs.load_store(path).get(branch)) + "\n")
        else:
            _log(
                "Usage: record-gate.py [--init <tier> | --record <gate> | "
                "--status | --oneline | --unroute] [--branch <name>] "
                "[--route-from <path>]"
            )
            return 1
    except (ValueError, gs.StoreCorruptError) as e:
        _log(f"[gate] error: {e}")
        return 1
    except OSError as e:
        _log(
            "[gate] error: could not lock or write gate store "
            f"(is .claude/data on a filesystem without flock support?): {e}"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
