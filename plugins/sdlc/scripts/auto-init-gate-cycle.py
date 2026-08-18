#!/usr/bin/env python3
"""
PostToolUse hook: auto-init an SDLC gate cycle on any git commit to a
non-default branch (idempotent — no-ops if a cycle already exists).

When a git commit runs on a feature branch that has no active gate cycle, this
hook initializes a gate cycle so the enforce-sdlc-gates.py hook fires on a
subsequent `gh pr create` — without requiring a manual /sdlc:gate --init.
Defaults to 'small-medium'; auto-downgrades to 'tiny' when every file changed
since the branch diverged from main/master is confidently docs/text/assets
(see _pick_tier) — mirrors /sdlc:gate's own docs-only classification, but
applied automatically instead of waiting for a human to notice and re-init.

Projects can opt out by shipping their own SDLC overlay skill at
`.claude/skills/sdlc/SKILL.md`. The overlay signals "this repo has its own
tier logic — don't stamp a default cycle on my behalf." Explicit /sdlc:init
still works.

Output (see hook_out): exit 2 is non-blocking here and simply means "the model
should see this". Two things earn it. Every path where a commit produced NO
gate cycle — meaning the later `gh pr create` sails through unchecked — goes
out via `ungated`. And arming a cycle does too, because that message asks the
reader to choose a tier; it fires at most once per branch.

Everything else stays at 0 and stays quiet, because nothing was lost and
nothing is being asked: not a commit at all, `main`/`master` (gating them is
meaningless), detached HEAD (routine mid-rebase, and the work lands on a branch
that has its own cycle), a cycle already present, and the overlay opt-out.

No third-party dependencies. Requires gate_store.py, hook_out.py and
shell_parse.py in the same directory.
@fires-on Bash tool (PostToolUse)
"""

import json
import os
import subprocess
import sys

import gate_store as gs
import hook_out as ho
import shell_parse as sp

GIT_COMMIT = "git commit"
DEFAULT_TIER = "small-medium"
OVERLAY_SKILL_PATH = os.path.join(".claude", "skills", "sdlc", "SKILL.md")

# Allowlist, not a denylist: an unrecognized extension (or none at all) is
# NOT docs. See _pick_tier — under-classifying silently skips gates 2-6,
# over-classifying is one `--init tiny` away from being right, so ties go to
# the bigger tier.
DOCS_EXTENSIONS = {
    ".md",
    ".mdx",
    ".txt",
    ".rst",
    ".adoc",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    # NOT .svg: unlike the raster formats above, SVG is XML and can carry
    # <script>/event-handler payloads — a "docs-only" diff whose only file is
    # a crafted .svg would otherwise skip the review gate for exactly the
    # kind of content that needs it.
    ".ico",
    ".webp",
    ".pdf",
}
DOCS_BASENAMES = {"LICENSE", "NOTICE", "CHANGELOG", "COPYING"}


# Deliberately does NOT claim the commit succeeded. This hook cannot know that:
# the Bash PostToolUse payload's shape is undocumented, and `git commit && git
# push` reports one status for two commands. What IS always true is that this
# message can't have affected the tool call, which is the misreading that
# matters — an error-shaped line attached to a commit invites a retry.
COMMIT_OK = "Your commit already ran — this does not change that"


def ungated(what, fix="Run `/sdlc:gate --init <tier>` to gate it anyway."):
    """Report that this commit left the branch with no gate cycle, and return
    the exit code that makes the report visible.

    Opens with COMMIT_OK because this arrives attached to the commit's own tool
    call, shaped like an error: without that clause it reads as "the commit
    failed", and the obvious response to that is to run it again. `fix` is a
    parameter because the default advice is wrong for a store that can't be
    written — `--init` writes to the same file.

    Single-sourced for the five no-cycle paths that call it, which had drifted
    into near-copies. Two other messages in main() — the arming line and the
    errored-commit line — say different things and are written there, but share
    COMMIT_OK where it applies."""
    return ho.notify(
        "auto-init",
        f"{COMMIT_OK}. But {what}, so no gate cycle was stamped — "
        f"`gh pr create` will NOT be gated for this work. {fix}",
    )


def is_commit_ish(command):
    """Cheap "could this have been a commit?" test, for error paths only.

    Deliberately a substring test and not the tokenizer: the callers are places
    where we either can't trust the parse or don't want to pay for it, and the
    cost of a false positive is one extra line, while a false negative is a
    branch silently left ungated."""
    return "commit" in str(command)


def is_git_commit(command):
    """True when the command really runs `git commit`.

    argv-based (see shell_parse), so git's pre-subcommand flags (`git -C dir
    commit`) and wrappers (`bash -c "git commit"`) are handled, while
    `git log --grep commit` and `echo "git commit"` are not commits."""
    return sp.invokes(command, GIT_COMMIT)


def _git_output(args, cwd=None):
    """Run git, return decoded+stripped stdout, or None on any failure.

    Local to this file rather than gate_store._git (which raises instead of
    returning None): every caller here wants "couldn't determine" folded into
    one falsy value, not an exception to catch again at each call site."""
    try:
        return (
            subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.DEVNULL)
            .decode()
            .strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return None


def has_project_overlay(cwd=None):
    """Return True when the repo ships its own SDLC overlay skill.

    Resolves the repo's top-level worktree dir via `git rev-parse
    --show-toplevel` (this is the checkout we're committing in — where the
    overlay file lives on this branch). Any resolution failure returns False
    so the hook keeps its current default-init behavior for repos without an
    overlay."""
    top = _git_output(["rev-parse", "--show-toplevel"], cwd=cwd)
    if not top:
        return False
    return os.path.isfile(os.path.join(top, OVERLAY_SKILL_PATH))


def _is_docs_path(path):
    base = os.path.basename(path)
    if base in DOCS_BASENAMES:
        return True
    _, ext = os.path.splitext(path)
    return ext.lower() in DOCS_EXTENSIONS


def _default_branch_candidates(workdir):
    """Branches to try as the diff base, most-authoritative first.

    `origin/HEAD`'s symbolic ref names the actual default branch when it's
    known (set by `git remote set-head origin --auto`, or by some clones) —
    checked first so a repo whose default branch isn't `main`/`master` (e.g.
    `trunk`, `develop`) isn't silently unclassifiable forever. It isn't
    always set, so the hardcoded names remain as a fallback, in the same
    origin-before-local order as before (see _merge_base)."""
    candidates = []
    symbolic = _git_output(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=workdir)
    if symbolic and symbolic.startswith("refs/remotes/"):
        resolved = symbolic[len("refs/remotes/") :]
        candidates.append(resolved)
    for base in ("origin/main", "origin/master", "main", "master"):
        if base not in candidates:
            candidates.append(base)
    return candidates


def _merge_base(workdir):
    """Merge-base with the first resolvable candidate from
    _default_branch_candidates, or None. origin/* before local: /sdlc:gate's
    own documented docs-only check runs `git diff --name-only
    origin/main...HEAD` (gate.md), and a stale local `main` (nobody fetches
    before every commit) would otherwise let this hook and that documented
    check disagree about the same branch."""
    for base in _default_branch_candidates(workdir):
        merge_base = _git_output(["merge-base", base, "HEAD"], cwd=workdir)
        if merge_base:
            return merge_base
    return None


def _changed_paths_since(merge_base, workdir):
    """Files changed since `merge_base`, or None on a git failure — the
    caller must NOT assume docs-only on None."""
    out = _git_output(["diff", "--name-only", f"{merge_base}..HEAD"], cwd=workdir)
    if out is None:
        return None
    return [line for line in out.splitlines() if line]


def _pick_tier(workdir):
    """(tier, reason). DEFAULT_TIER unless every file changed since the
    default branch is confidently docs/text/assets. Any uncertainty (no
    default branch to diff against, a git failure, an empty diff) keeps the
    safe default. `reason` records which case applied — stored on the cycle
    (see gate_store.init_gates's tier_reason) so a later reader doesn't have
    to re-derive it from git state that may have moved on since.

    Calls _merge_base directly (rather than through a combined helper) so
    "no default branch resolvable" and "found one, but the diff itself
    failed" get distinct, accurate reasons instead of collapsing into one —
    they're different problems with different fixes."""
    merge_base = _merge_base(workdir)
    if merge_base is None:
        return DEFAULT_TIER, "default (no default branch found to diff against)"
    paths = _changed_paths_since(merge_base, workdir)
    if paths is None:
        return DEFAULT_TIER, "default (a git failure prevented computing the diff)"
    if not paths:
        return DEFAULT_TIER, "default (no changes found since the default branch)"
    if all(_is_docs_path(p) for p in paths):
        return "tiny", f"docs-only diff auto-detected ({len(paths)} file(s))"
    return DEFAULT_TIER, "default (diff includes non-docs files)"


def main():
    try:
        data_in = json.load(sys.stdin)
    except Exception as e:
        # Not `ungated` — we can't claim a commit went ungated when we never
        # learned what the tool call was. But it can't be an exit-0 stderr
        # write either: if the payload shape ever changes, this hook stops
        # stamping cycles on every commit in every repo, and that is not
        # allowed to be a debug-log line.
        ho.emit(ho.warn("auto-init", f"could not parse hook stdin ({e}), skipping"))
        return 0
    if data_in.get("tool_name") != "Bash":
        return 0
    # A tool call that errored may not have committed anything, so don't stamp
    # a cycle on its say-so. But don't go silent either: `git commit && git
    # push` reports ONE error status for two commands, so the commit may well
    # have landed — and a silent skip there is the plugin's worst outcome, a
    # branch that looks gated and isn't. Say what is actually known.
    #
    # (`is_error` follows auto-record-skill-gate.py, where it is load-bearing
    # for Skill payloads. For Bash it is unverified, so nothing below depends
    # on it firing — this is a refinement, not a guard.)
    resp = data_in.get("tool_response")
    if isinstance(resp, dict) and resp.get("is_error"):
        if not is_commit_ish(data_in.get("tool_input", {}).get("command", "")):
            return 0
        return ho.notify(
            "auto-init",
            "the commit command reported an error, so no gate cycle was "
            "stamped. If a commit did land (a chained `&& push` failing, say), "
            "`gh pr create` will NOT be gated — run `/sdlc:gate --init <tier>`.",
        )
    command = data_in.get("tool_input", {}).get("command", "")
    try:
        commit = is_git_commit(command)
        workdir = sp.resolve_workdir(command, GIT_COMMIT) if commit else None
    except Exception as e:
        # Detection is a recursive tokenizer now, not a re.search. A bug in it
        # must not surface as a traceback on every git command — but it must
        # not vanish either: no cycle stamped means nothing gets gated later.
        # Only escalate when the command could plausibly have been a commit,
        # so a tokenizer bug doesn't warn about ungated work on `ls`, where
        # there was never a cycle to stamp. Deliberately a crude substring
        # test and not the tokenizer: the tokenizer is what just failed. It
        # errs both ways — `git log --grep commit` would warn spuriously, an
        # aliased `git ci` would stay quiet — which is why it only gates the
        # error path, never detection itself.
        if not is_commit_ish(command):
            return 0
        # No COMMIT_OK opener on this one path: the tokenizer failed, so
        # whether this was a commit at all is exactly what we don't know, and
        # the substring test above admits false positives. Claiming a commit
        # succeeded here would be asserting the thing we just failed to read.
        return ho.notify(
            "auto-init",
            f"a command mentioning 'commit' could not be parsed ({e}), so no "
            "gate cycle was stamped. If that was a commit, `gh pr create` will "
            "NOT be gated for this work — run `/sdlc:gate --init <tier>`.",
        )
    if not commit:
        return 0

    branch = gs.detect_branch(cwd=workdir)
    if not branch:
        # Always say this, not just when a workdir was parsed. The case with no
        # detectable workdir at all is the common one — staying quiet exactly
        # where we know least is backwards.
        where = repr(workdir) if workdir else "this process's cwd"
        return ungated(f"no branch could be detected in {where}")
    if branch in ("HEAD", "main", "master"):
        # `HEAD` is detached, and unlike the cases above that is not a lost
        # gate worth shouting about: `git commit --amend` during an interactive
        # rebase lands here routinely, and the commits end up on a branch whose
        # own first commit already stamped a cycle. Escalating would interrupt
        # every rebase step, and the advice would be actively wrong — the
        # cycle would be keyed to the literal string "HEAD" and match every
        # future detached state in the repo. main/master are quiet because
        # gating them is meaningless.
        return 0

    if has_project_overlay(cwd=workdir):
        # The one no-cycle case that stays quiet: the repo shipped the overlay
        # to opt out, so nothing here is unexpected and there is nothing for
        # the agent to do about it. Routed through notify(surface=False) rather
        # than a bare stderr write so the channel is chosen, not defaulted.
        return ho.notify(
            "auto-init",
            f"project overlay skill detected ({OVERLAY_SKILL_PATH}), "
            "deferring to it — no cycle stamped.",
            surface=False,
        )

    path = gs.default_store_path(cwd=workdir)
    # Cheap, lock-free pre-check before paying for _pick_tier's 2-3 git
    # subprocess spawns: this hook fires on EVERY commit, not just the first
    # per branch, but the tier it computes is only used when a cycle doesn't
    # exist yet — discarded by the mutator's idempotency check on every
    # commit after the first. Broad except on purpose: a corrupt store, a
    # permissions blip, a delete racing this read, or a syntactically-valid-
    # but-wrong-shape store (`.get` on a non-dict) all just mean "can't tell
    # here" — fall through to computing the tier normally rather than
    # crashing before the real error handling below (which re-reads the same
    # store under the lock) ever gets a chance to report it properly.
    try:
        cycle_exists = bool(gs.load_store(path).get(branch))
    except Exception as e:
        # Debug-log only (surface=False): this hook's real error handling —
        # StoreCorruptError/ValueError/OSError/Exception around update_store,
        # a few lines down — re-reads the same store under the lock and
        # reports properly if something's actually wrong. This is just the
        # pre-check's own attempt failing, not a new failure mode; going
        # fully silent here would be the one place in this file that lets a
        # bug vanish without a trace, which is exactly what every other
        # except block here is written to avoid.
        ho.notify(
            "auto-init",
            f"pre-check couldn't read the gate store at {path} ({e}), "
            "computing tier normally",
            surface=False,
        )
        cycle_exists = False
    if cycle_exists:
        tier, tier_reason = DEFAULT_TIER, None  # unused: mutator no-ops below
    else:
        tier, tier_reason = _pick_tier(workdir)
    try:
        # Idempotency check is inside the mutator so it runs under the exclusive
        # lock — one read, atomic guard, no write when cycle already exists.
        # Unlike `record-gate.py --init`, this does NOT clear a route pointing at
        # this worktree, and that asymmetry is deliberate: a route is an explicit
        # declaration that this session drives another worktree's gates, and a
        # background hook firing on an incidental commit has no business
        # revoking it. Only an explicit --init/--unroute tears a route down.
        result = gs.update_store(
            path,
            lambda d: (
                None
                if d.get(branch)
                else gs.init_gates(d, branch, tier, tier_reason=tier_reason)
            ),
        )
    # Every failure here leaves the branch with no cycle, same as above.
    except gs.StoreCorruptError as e:
        # Same reason the write-failure branch below drops the advice:
        # `--init` reads this store first and exits 1 on the same corruption.
        return ungated(
            f"the gate store at {path} is unreadable ({e})",
            fix="Repair or delete that file, then re-init the cycle.",
        )
    except (ValueError, OSError) as e:
        # No `--init` advice here: it writes to the same store and fails the
        # same way. OSError also covers filesystems without flock support,
        # where this repeats on every commit — so name the path, which is the
        # only thing that lets someone act on it.
        return ungated(
            f"the gate store at {path} could not be written ({e})",
            fix="Check that path is writable and supports file locking.",
        )
    except Exception as e:
        return ungated(f"an unexpected error occurred ({e})")

    if result is not None:
        # Name the requirement here, at arming time. This is the only notice a
        # user gets who never invokes /sdlc:gate — the command's own "check for
        # the skills before you init" advice can't fire on this path, and the
        # skill-gated gates cannot be recorded any other way.
        #
        # Which is exactly why it exits 2 rather than 0: it asks the reader to
        # do something, and at exit 0 no reader ever gets it. Fires at most
        # once per branch, since `result` is None when a cycle already exists.
        # Non-blocking — the commit already ran.
        if tier == "tiny":
            # _pick_tier already confirmed every changed file is docs/text/
            # assets — gates 2-6 don't apply, so there's nothing to ask the
            # reader to widen unless that changes.
            detail = (
                f'It armed a tiny gate cycle for "{branch}" — every file '
                "changed so far looks like docs/text/assets, so the grumpy "
                "review gates don't apply. Run `/sdlc:gate --init "
                "small-medium` (or `significant`) if that changes."
            )
        else:
            detail = (
                f'It armed a {tier} gate cycle for "{branch}": gates 2-6 '
                "need /simplify and the grumpy skills to record, so without "
                "them, re-init as tiny (/sdlc:gate --init tiny) if the "
                "change qualifies."
            )
        return ho.notify("auto-init", f"{COMMIT_OK}. {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
