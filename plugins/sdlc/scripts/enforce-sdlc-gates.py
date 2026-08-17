#!/usr/bin/env python3
"""
PreToolUse hook: block `gh pr create` when SDLC gates are incomplete.

Reads the shared (per-repo, branch-keyed) JSON gate store, takes the branch from
the command's --head flag (or, failing that, its working directory), and blocks
if any required gate for the tier is missing. Because the store is shared across
worktrees, a cycle recorded in one checkout is seen here even when the PR is
created from another. Opt-in: no cycle for the branch -> allow.

KNOWN BLIND SPOT, not a bug to be fixed here: `--head owner:branch` (a PR from a
fork) strips to the bare branch name for the store lookup (extract_head_flag),
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

Pure stdlib — runs without bun. @fires-on Bash tool, @blocking
"""

import json
import os
import sys

import gate_store as gs
import hook_out as ho
import shell_parse as sp


PR_CREATE = "gh pr create"


def pr_creates(command):
    """EVERY `gh pr create` invocation in `command`, each with its own workdir.

    Detection is argv-based (see shell_parse): it sees through the wrapped
    forms agents routinely emit — `bash -c "gh pr create"`, `eval`, subshells,
    env prefixes — which a regex over the raw string read as unrelated
    commands, so the gate simply stopped applying. `echo "gh pr create"` is
    still not a PR.

    All of them, not just the first: one command can open two PRs, and
    `gh pr create --head done/branch && gh pr create --head ungated/branch`
    would otherwise be judged entirely on the first — including its workdir,
    so the second would be checked against the first one's repo."""
    return sp.invocations(command, PR_CREATE)


def is_crew_actor():
    """Non-interactive crew sessions should get machine-parseable deny
    output. Any truthy SDLC_ACTOR=crew value enables it."""
    return os.environ.get("SDLC_ACTOR", "").strip().lower() == "crew"


# gh pr create flags that consume the following token. Needed so the scan below
# can tell a flag from a value: `--title "-Hotfix: ..."` must not be read as a
# head branch. Over-listing is harmless here (a missed flag only costs us the
# `-H` clustered form); under-listing is what creates a bypass.
GH_VALUE_FLAGS = {
    "--title",
    "-t",
    "--body",
    "-b",
    "--body-file",
    "-F",
    "--base",
    "-B",
    "--head",
    "-H",
    "--label",
    "-l",
    "--assignee",
    "-a",
    "--reviewer",
    "-r",
    "--milestone",
    "-m",
    "--project",
    "-p",
    "--template",
    "-T",
    "--repo",
    "-R",
}


def extract_head_flag(argv):
    """The head branch from `--head`/`-H`, in every spelling gh accepts.

    Parsed positionally rather than via sp.flag_value because gh (pflag) also
    accepts the clustered `-Hbranch`, and recognizing that requires knowing
    which tokens are flags. A naive scan reads `--title "-Hotfix: crash"` as a
    head branch, whose unknown name then looks like "no cycle -> allow" — a
    gate bypass triggered by a plausible PR title."""
    head = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("--head", "-H") and i + 1 < len(argv):
            head = argv[i + 1]
            break
        if tok.startswith("--head=") or tok.startswith("-H="):
            head = tok.split("=", 1)[1]
            break
        # Clustered `-Hbranch`, but only in a flag position — never a value.
        if tok.startswith("-H") and len(tok) > 2 and not tok.startswith("--"):
            head = tok[2:]
            break
        i += 2 if tok in GH_VALUE_FLAGS else 1
    if not head:
        return head
    # Normalize the forms gh accepts that aren't how the store is keyed:
    # `--head owner:branch` for a fork, and a full `refs/heads/branch`. Left
    # raw, both miss the branch's cycle and read as "no cycle -> allow" — a
    # one-flag bypass of the whole gate, which is not something to leave open
    # while removing escape hatches elsewhere. Owner first, then the ref
    # prefix: `owner:refs/heads/branch` is a legal combination of the two, and
    # stripping in the other order leaves the prefix behind.
    if ":" in head:
        head = head.split(":", 1)[1]
    if head.startswith("refs/heads/"):
        head = head[len("refs/heads/") :]
    # A degenerate `--head owner:` normalizes to "", which the caller's `or`
    # treats as "no --head given" and resolves from the cwd instead. That is a
    # silent substitution, but gh rejects the input anyway, so there is no real
    # command it lets through — noted rather than special-cased.
    return head


def check_gates(tool, tool_input):
    """Returns (allowed, error_string, state_dict, notes) for the whole command.

    Denies if ANY `gh pr create` in it is ungated — first refusal wins, so a
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
    if tool != "Bash":
        return True, None, None, []
    command = tool_input.get("command", "")
    notes = []
    for inv in pr_creates(command):
        allowed, error, state, inv_notes = _check_one(inv)
        notes.extend(n for n in inv_notes if n not in notes)
        if not allowed:
            return allowed, error, state, notes
    return True, None, None, notes


def _check_one(inv):
    """One `gh pr create` invocation → (allowed, error, state, notes), the same
    4-tuple check_gates returns and documents."""
    # The workdir comes from this invocation (its own -C, or the last `cd`
    # before it in an enclosing scope) — not from a substring scan of the whole
    # string, which would follow a `cd` quoted inside the PR body, and not from
    # the first invocation, which would judge a second PR against the wrong repo.
    argv, workdir = inv.argv, inv.workdir
    notes = []
    if workdir is None and inv.names_workdir:
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
    branch = extract_head_flag(argv) or gs.detect_branch(cwd=workdir)
    if not branch:
        return (
            False,
            (
                "Could not detect branch for gate validation.\n\n"
                "Run from a git worktree, or pass --head <branch> to gh pr create."
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
        return True, None, None, notes  # opt-in: no cycle -> allow

    missing = gs.missing_gates(bd)
    if not missing:
        return True, None, None, notes

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
    routed = gs.routed_branch(data, source_root)
    if routed and routed[0] != branch:
        reason += (
            f'\n\nThis worktree\'s skill gates are routed to "{routed[0]}", not '
            f'"{branch}" — that may be why gates are missing here. '
            "/sdlc:gate --unroute stops routing them elsewhere."
        )
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
