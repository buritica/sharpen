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
  record-gate.py --profile <name>       # with --init: record an explicit
                                        # portable profile
  record-gate.py --capabilities-file <path>
                                      # with --init: resolve/store declared
                                      # portable capabilities
  record-gate.py --attach-review <path>
                                      # attach a validated portable review
                                      # report without recording a gate
  record-gate.py --attest <gate> --reason "<text>"
                                      # last-resort: stamp a skill-gated gate
                                      # on human attestation instead of the
                                      # auto-record hook (see gate_store.attest_gate)

Store path: $SDLC_GATES_PATH, or <main-checkout>/.sharpen/data/gates.json
            (resolved via `git rev-parse --git-common-dir`, shared per-repo;
             existing .claude/data/gates.json remains active until .sharpen exists)
"""

import sys

import capabilities
import gate_store as gs
import review_report


def _log(msg):
    sys.stderr.write(msg + "\n")


def _init_with_route(
    data, branch, tier, route_to_set, route_to_clear, profile=None, capabilities=None
):
    """Init the cycle, apply this invocation's routing decision, and report the
    branches it stopped driving as `(branch_data, dropped)`.

    Both roots arrive pre-resolved: this runs inside update_store's mutator, and
    a git subprocess in there holds the repo-wide flock. `_resolve_routing` does
    the git work before the lock is taken.

    `dropped` exists so the caller can say so out loud. Revoking a route moves
    where this worktree's skill gates land, and an unannounced move is the exact
    failure this whole channel was built to stop.
    """
    bd = gs.init_gates(data, branch, tier, profile=profile, capabilities=capabilities)
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
    requested_profile = None
    capabilities_file = None
    attach_review_file = None
    reason = None
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
        if argv[i] == "--profile" and i + 1 < len(argv):
            requested_profile = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--capabilities-file" and i + 1 < len(argv):
            capabilities_file = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--attach-review" and i + 1 < len(argv):
            attach_review_file = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--reason" and i + 1 < len(argv):
            reason = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--attest" and i + 1 < len(argv):
            rest.append("--attest")
            rest.append(argv[i + 1])
            i += 2
            continue
        rest.append(argv[i])
        i += 1

    command = rest[0] if rest else None
    if attach_review_file and command is not None:
        _log(
            "[gate] error: --attach-review does not modify or combine with gate commands"
        )
        return 1
    if attach_review_file and requested_profile:
        _log("[gate] error: --attach-review does not accept --profile")
        return 1
    if command != "--attest" and reason is not None:
        _log("[gate] error: --reason is only valid with --attest")
        return 1
    if "--attest" in rest and command != "--attest":
        # Without this, `--init <tier> --attest <gate>` parses to command
        # "--init" with the "--attest <gate>" tail silently absorbed as
        # unused positionals: no error, no attestation, exit 0 — the kind of
        # silent no-op this whole escape hatch exists to NOT be.
        _log(
            "[gate] error: --attest does not combine with other commands "
            f"(got: {' '.join(rest)})"
        )
        return 1
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
        if attach_review_file:
            report = review_report.load_report(attach_review_file)
            bd = gs.update_store(
                path, lambda d: review_report.attach_report(d, branch, report)
            )
            attached = bd["review_report"]
            provenance = attached["provenance"]
            provenance_note = provenance["kind"]
            if provenance["kind"] == "git-range":
                provenance_note += f" {provenance['base']}...{provenance['head']}"
            _log(
                f"[gate] attached {attached['status']} review report for {branch} "
                f"({provenance_note}; {len(attached['findings'])} finding(s))"
            )
            if attached["status"] == "fail":
                _log(
                    "[gate] note: a failing review report is evidence only; "
                    "it does not change recorded gates"
                )
        elif command == "--init":
            tier = rest[1] if len(rest) > 1 else None
            profile = None
            capability_snapshot = None
            if requested_profile and not capabilities_file:
                raise ValueError(
                    "--profile requires --capabilities-file so the resolver can "
                    "verify declared capabilities"
                )
            if capabilities_file:
                manifest = capabilities.load_manifest(capabilities_file)
                decision = capabilities.resolve_profile(
                    manifest["capabilities"], requested_profile
                )
                if decision["decision"] != "selected":
                    raise ValueError(decision["reason"])
                profile = decision["resolved_profile"]
                capability_snapshot = manifest["capabilities"]
            # Resolved before update_store takes the repo-wide flock.
            route_to_set, route_to_clear = _resolve_routing(branch, route_from)
            existing = gs.load_store(path).get(branch)
            had = gs.completed_gates(existing) if existing else []
            bd, dropped = gs.update_store(
                path,
                lambda d: _init_with_route(
                    d,
                    branch,
                    tier,
                    route_to_set,
                    route_to_clear,
                    profile=profile,
                    capabilities=capability_snapshot,
                ),
            )
            profile_note = f" with profile {profile}" if profile else ""
            _log(
                f"[gate] initialized {tier} cycle for {branch}{profile_note} "
                f"at {bd['created_at']}"
            )
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
        elif command == "--attest":
            if len(rest) < 2:
                _log('Usage: --attest <gate> --reason "<text>"')
                return 1
            gate = rest[1]
            # No reason pre-check here: gate_store.attest_gate raises ValueError
            # for a missing/blank reason, and the except clause below already
            # turns that into the same "[gate] error: ..." + exit 1 every other
            # command's store-level refusal produces — a second, separately
            # worded copy of that check would only be able to drift from it.
            bd = gs.update_store(
                path, lambda d: gs.attest_gate(d, branch, gate, reason)
            )
            remaining = gs.missing_gates(bd)
            done = (
                "All gates complete."
                if not remaining
                else f"Remaining: {', '.join(remaining)}"
            )
            _log(
                f"[gate] ⚠ {gate} attested (not hook-verified) for {branch}: "
                f"{reason.strip()}. {done}"
            )
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
                "--attest <gate> --reason <text> | --status | --oneline | "
                "--unroute] [--branch <name>] [--route-from <path>] "
                "[--profile <name> --capabilities-file <path>] "
                "[--attach-review <path>]"
            )
            return 1
    except (ValueError, gs.StoreCorruptError) as e:
        _log(f"[gate] error: {e}")
        return 1
    except OSError as e:
        _log(
            "[gate] error: could not lock or write gate store "
            f"(is the gate state dir on a filesystem without flock support?): {e}"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
