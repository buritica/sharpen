#!/usr/bin/env python3
"""
PreToolUse hook: block opening a PR when SDLC gates are incomplete.

Every path pr_paths.pr_opens() recognizes is gated: `gh pr create`/`gh pr new`,
a `gh api` POST to .../pulls or a createPullRequest mutation, `glab mr create`,
`hub pull-request`, `git push -o merge_request.create`, and MCP
create_pull_request/create_merge_request tools, from Bash, Monitor or pi's
bash. For each one it reads the shared (per-repo, branch-keyed) JSON gate
store of the checkout pr_paths resolved (the workdir, or the checkout whose
remote matches a `-R`/owner/project hint), takes the branch from the call's
head (`--head`, `--source-branch`, `head=`, the MCP `head`/`source_branch`;
`HEAD`, `@` or a shell-expanded value mean the workdir's checked-out branch),
and blocks if any required gate for the tier is missing or stale. Because the
store is shared across worktrees, a cycle recorded in one checkout is seen here
even when the PR is created from another. Opt-in: no cycle for the branch, or a
named repo with no checkout in reach from a non-checkout cwd -> allow, with a
note.

KNOWN BLIND SPOT, not a bug to be fixed here: `--head owner:branch` (a PR from a
fork) strips to the bare branch name for the store lookup (pr_paths.normalize_head),
but that name almost never has a cycle in THIS checkout's store — the fork
contributor's own commits ran against their own clone, if any hooks ran at all.
That reads as "no cycle -> allow" and the PR goes out ungated. This is
architectural, not a parsing gap: the whole system is local-hook-based state
with no shared backend across separate clones, so there is no cycle to find.
Closing it needs a different enforcement point entirely (a CI-side check that
re-derives gate state from the PR's own commits, not this local store) — not a
fix to how this hook reads --head.

Denials go out as the documented PreToolUse payload *and* exit 2; caveats that
accompany an allow can only reach the user. See hook_out for why.

Pure stdlib — runs without bun. @fires-on Bash, Monitor and MCP PR tools, @blocking
"""

import json
import os
import sys

import gate_receipts as rc
import gate_store as gs
import hook_out as ho
import pr_paths as pp


def is_crew_actor():
    """Non-interactive crew sessions should get machine-parseable deny
    output. Any truthy SDLC_ACTOR=crew value enables it."""
    return os.environ.get("SDLC_ACTOR", "").strip().lower() == "crew"


def check_gates(tool, tool_input):
    """Returns (allowed, error_string, state_dict, notes) for the whole command.

    `tool` is any host tool name: a shell tool (Bash, Monitor, pi's bash) or an
    MCP PR tool. pr_paths decides whether the call opens a PR at all; this
    module only judges the gates. Denies if ANY PR it opens is ungated — first refusal wins, so a
    gated PR chained behind a clean one can't ride through.

    state_dict is populated on incomplete-gates denials and is what crew
    mode reformats into machine-parseable output. It's None for allows and
    for the pre-gate failure modes (undetectable branch, corrupt store)
    where there's nothing structural to report.

    notes is a list of caveats about how the verdict was reached. They ride
    the denial's reason when there is one (the agent reads it) and fall back
    to a user-facing `systemMessage` when the verdict is an allow. Deduped:
    two `gh pr create`s behind the same unresolvable `cd` are one caveat,
    not the same sentence printed twice."""
    notes = []
    for pr in pp.pr_opens(tool, tool_input or {}):
        allowed, error, state, inv_notes = _check_one(pr)
        notes.extend(n for n in inv_notes if n not in notes)
        if not allowed:
            return allowed, error, state, notes
    return True, None, None, notes


def _check_one(pr):
    """One attempted PR open (a pr_paths.PrOpen) → (allowed, error, state,
    notes), the same 4-tuple check_gates returns and documents."""
    # The workdir comes from this invocation (its own -C, or the last `cd`
    # before it in an enclosing scope) — not from a substring scan of the whole
    # string, which would follow a `cd` quoted inside the PR body, and not from
    # the first invocation, which would judge a second PR against the wrong repo.
    workdir = pr.workdir
    notes = list(pr.notes)
    if pr.no_checkout:
        # An MCP call (or `-R owner/repo`) for a repo with no local checkout
        # in reach: there is no store to read, so no cycle. Opt-in allows,
        # but says so rather than looking like a check that passed.
        return True, None, None, notes
    if workdir is None and pr.names_workdir:
        # The command asked for a directory we couldn't resolve (`cd $VAR`,
        # `cd ~/x` that doesn't exist). We fall back to our own cwd, which may
        # be a different repo — so the caveat has to travel with the verdict
        # rather than sit on exit-0 stderr, where nothing reads it.
        notes.append(
            "The command changes directory, but the target could not be "
            "resolved — gates were checked against this process's cwd "
            "instead, so this verdict may be about a different repo."
        )
    path = gs.default_store_path(cwd=workdir)
    branch = pr.head or gs.detect_branch(cwd=workdir)
    if not branch:
        return (
            False,
            (
                "Could not detect branch for gate validation.\n\n"
                f"Run from a git worktree, or name the head branch in the "
                f"{pr.label} call."
            ),
            None,
            notes,
        )

    try:
        data = gs.load_store(path)
    except gs.StoreCorruptError as e:
        # Corruption must not look like "no cycle -> allow". Fail closed.
        return (
            False,
            (
                f"SDLC gate store at {path} is unreadable ({e}).\n"
                "Refusing to allow the PR until it's fixed. Repair or delete "
                "that file, then re-init the cycle — /sdlc:gate reads the same "
                "store and will fail the same way until it's readable."
            ),
            None,
            notes,
        )
    bd = data.get(branch)
    if not bd:
        # Opt-in: no cycle -> allow. A branch pushed only through an MCP tool
        # never had a local commit for auto-init to arm, so say so there.
        if pr.label.startswith("MCP "):
            notes.append(
                f'No gate cycle for "{branch}" in {path}, so this PR was not '
                "gated. A branch pushed only through an MCP tool never arms one; "
                "run /sdlc:gate --init <tier> in its checkout to opt it in."
            )
        return True, None, None, notes

    missing = gs.missing_gates(bd)
    if not missing:
        stale = rc.stale_skill_gates(bd, rc.branch_head(branch, workdir))
        if not stale:
            return True, None, None, notes
        state = {
            "branch": branch,
            "tier": bd.get("tier"),
            "required": gs.required_gates(bd),
            "completed": gs.completed_gates(bd),
            "missing": [],
            "stale": stale,
        }
        return (
            False,
            f'SDLC review gates are stale for branch "{branch}".\n\n'
            + rc.stale_message(bd, branch, stale),
            state,
            notes,
        )

    # Deliberately no escape hatch here: a missing gate blocks whatever the
    # diff contains. See test_gitignored_only_diff_still_blocks for the waiver
    # this removed, and why it couldn't be made visible instead.
    tier = bd.get("tier")
    required = gs.required_gates(bd)
    completed = gs.completed_gates(bd)
    state = {
        "branch": branch,
        "tier": tier,
        "required": required,
        "completed": completed,
        "missing": missing,
    }
    reason = (
        f'SDLC gates incomplete for branch "{branch}" (tier "{tier}").\n\n'
        f"Completed ({len(completed)}/{len(required)}): {', '.join(completed) or '(none)'}\n"
        f"Missing ({len(missing)}): {', '.join(missing)}\n\n"
        "Run /sdlc:gate to finish the chain, then retry.\n"
        # The tier is often the real problem, not the gates: auto-init stamps
        # small-medium on a branch's first commit, so a docs-only change
        # inherits an eight-gate chain nobody chose. Naming the way out here
        # is the difference between a gate and a wall — it used to be a
        # gitignore escape that fired silently, which is worse.
        "If this branch doesn't warrant that tier (docs-only, or no executable "
        "change), /sdlc:gate --init <tier> picks a different one — but note it "
        "RESETS the cycle, so any gate above is discarded and the skill-gated "
        "ones must be earned again by re-running their skills."
    )
    # A missing skill-gate here is often not "nobody ran the skill" — it's "the
    # skill ran, but this worktree's route sent the stamp somewhere else." The
    # store already has the answer; say it instead of leaving the reader to
    # find --status on their own.
    source_root = gs.canonical_worktree_root(workdir)
    note = gs.route_mismatch_note(data, source_root, branch)
    if note:
        reason += f"\n\n{note} That may be why gates are missing here."
    # Same reasoning, different cause: a missing bash gate here can also mean
    # it passed once and was cleared again by a later code-mutating gate —
    # without this, that reads identical to "never ran" and the reader has
    # to already know to check --status for the difference.
    invalidation = gs.invalidation_note(bd)
    if invalidation:
        reason += f"\n\n{invalidation}"
    return False, reason, state, notes


def fail_open(message):
    """Allow, but say so.

    These are the branches where the enforcer itself broke. They can't fail
    closed — one bug would wedge every Bash call in the session — so they fail
    open, which makes them the most dangerous paths in the plugin: a broken
    gate is indistinguishable from a passing one.

    `systemMessage` reaches the user and not the model (see hook_out). In a
    headless crew run there is no user, so crew also gets the line on stderr —
    which an exit-0 hook does NOT surface to the model either. It is there for
    a dispatcher capturing the hook process's own stderr, and for nobody else.
    That is the honest limit of this channel: on an allow, PreToolUse has no
    way to reach the agent, and inventing one would mean deciding "allow" and
    auto-approving the command."""
    ho.emit(ho.warn("enforce", message))
    if is_crew_actor():
        sys.stderr.write(f"[gate] enforce: {message}\n")
    return 0


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return fail_open("could not parse hook stdin — not enforcing this call.")
    try:
        allowed, error, state, notes = check_gates(
            data.get("tool_name"), data.get("tool_input", {})
        )
    except Exception as e:
        return fail_open(f"unexpected error, NOT enforcing this call: {e}")
    if not allowed:
        reason = "\n\n".join([error, *notes])
        # Deny twice over, on purpose. Exit 2 is what blocks and what puts the
        # reason in front of the agent; the stdout payload is the documented
        # form, kept so the denial still lands if that ever becomes the channel
        # that's read. A gate must fail closed, so the exit code leads.
        ho.emit(ho.deny(reason))
        if state is not None and is_crew_actor():
            # Crew mode: attach the structured state so headless dispatchers can
            # parse missing gates and act (record a stamp, re-dispatch, escalate)
            # instead of failing on prose. It rides stderr rather than the stdout
            # payload because unknown keys there are a schema-validation failure
            # — which means a dispatcher has to read the raw process stderr, not
            # the reason the model sees. Only incomplete-gates has state.
            #
            # On its own line, and last: gate_store may already have written a
            # prose warning to this same stream (the shared-git-dir fallback),
            # in which case json.load() over the whole of stderr fails on the
            # first character. Contract for dispatchers: parse the LAST
            # non-empty line, not the entire stream.
            sys.stderr.write(
                "\n"
                + json.dumps(
                    {"decision": "deny", "reason": reason, "sdlc_state": state}
                )
                + "\n"
            )
        else:
            # Newline-terminated for the same reason the crew branch above
            # wraps its payload: an unterminated stream lets whatever writes
            # next concatenate onto the reason.
            sys.stderr.write(reason + "\n")
        return 2
    if notes:
        ho.emit(ho.warn("enforce", *notes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
