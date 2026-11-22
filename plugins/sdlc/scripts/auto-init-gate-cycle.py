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

Arming a cycle also installs (or refreshes) sdlc's git pre-push floor via
install-pre-push.py, so the default path gets it without /sdlc:gate. It is
named in the arming message only on a first install, and never fails the hook.

No third-party dependencies. Requires capabilities.py, gate_store.py,
gate_receipts.py, gate_convergence.py, gate_base.py, hook_out.py and
shell_parse.py in the same directory.
@fires-on Bash tool (PostToolUse)
"""

import json
import importlib.util
import os
import subprocess
import sys

import capabilities
import gate_base as gb
import gate_convergence as gc
import gate_receipts as rc
import gate_store as gs
import hook_out as ho
import hosts
import shell_parse as sp

GIT_COMMIT = "git commit"
DEFAULT_TIER = "small-medium"
OVERLAY_SKILL_PATH = os.path.join(".claude", "skills", "sdlc", "SKILL.md")
# The grumpy passes a small-medium/significant cycle needs (gates 2-6), as
# manifest capability names, in chain order.
SKILL_CAPABILITIES = ("simplify", "review", "fix", "imagine")

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


def _capability_manifest_path(host, cwd=None):
    """Where the session-start adapter wrote this host's manifest.

    Before sdlc 4.15 every host wrote `capabilities.claude.json`. Claude Code
    still reads that file when its own is missing (a session that started
    before the upgrade); other hosts don't, because what it says about them
    was written under the wrong name."""
    path = gs.state_file_path(hosts.manifest_name(host), hosts.MANIFEST_ENV, cwd)
    if path and os.path.isfile(path):
        return path
    if host == "claude-code" and not os.environ.get(hosts.MANIFEST_ENV):
        legacy = gs.state_file_path(hosts.LEGACY_MANIFEST_NAME, hosts.MANIFEST_ENV, cwd)
        if legacy and os.path.isfile(legacy):
            return legacy
    return path


def _load_host_manifest(host, cwd=None):
    """This host's validated manifest, or None.

    Missing, unreadable or invalid manifests deliberately preserve legacy
    auto-init behavior: local enforcement still works without them, and
    explicit `/sdlc:gate --init --capabilities-file ...` remains the path that
    fails closed on a bad declaration."""
    path = _capability_manifest_path(host, cwd)
    if not path or not os.path.isfile(path):
        return None
    try:
        return capabilities.load_manifest(path)
    except ValueError as e:
        ho.notify(
            "auto-init",
            f"ignoring capability manifest at {path}: {e}",
            surface=False,
        )
        return None


def _resolve_portable_profile(manifest):
    """Return `(profile, snapshot)` from a loaded manifest, or `(None, None)`.

    The profile is recorded on the cycle for diagnostics only. It never
    changes the tier: an incomplete profile still arms the tier _pick_tier
    chose, and the arming message names what is missing instead."""
    if manifest is None:
        return None, None
    decision = capabilities.resolve_profile(manifest["capabilities"])
    if decision["decision"] != "selected":
        ho.notify(
            "auto-init",
            f"capability manifest did not satisfy a profile: {decision['reason']}",
            surface=False,
        )
        return None, None
    return decision["resolved_profile"], manifest["capabilities"]


def _pick_evidence(manifest, host):
    """`(evidence, note)` for a new cycle.

    Receipt-backed by default (or event-only when SDLC_GATE_EVIDENCE=events).
    The one automatic exception: the manifest says grumpy is installed but
    predates receipts (`x-receipts: false`), and the host is Claude Code. Its
    passes write no receipt, so a receipt-backed cycle could never complete;
    an event-only cycle can there, because Claude Code's `PostToolUse` hook on
    the `Skill` tool stamps it. No other host has that event (pi has no Skill
    tool; Codex and `unknown` have no confirmed equivalent), so an event-only
    cycle could not complete there either: those hosts keep the receipt cycle
    and are told to upgrade grumpy. A missing grumpy is NOT this case —
    nothing records without it either way, and the arming message names it."""
    evidence = rc.default_evidence()
    if (
        evidence == rc.EVIDENCE_RECEIPTS
        and manifest is not None
        and manifest.get("x-receipts") is False
        and "review" in manifest["capabilities"]
    ):
        if host == "claude-code":
            return None, (
                " The installed grumpy predates receipts, so this cycle records "
                "gates 2-6 the old event-only way; upgrade grumpy to 2.9.0 or "
                "later for receipt-backed gates."
            )
        return evidence, (
            " The installed grumpy predates receipts, so gates 2-6 can't record "
            "on this host until grumpy is upgraded to 2.9.0 or later "
            "(`record-gate.py --attest` per gate is the last resort)."
        )
    return evidence, ""


# Absolute, because the agent's shell on Claude Code has no plugin root to
# build the path from: the script sits next to this hook.
RECORD_GATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "record-gate.py")


def _missing_skills(manifest):
    """Grumpy passes the manifest says are absent. A manifest written before
    sdlc 4.15 (no current `x-source`) predates the `simplify` capability, so
    its silence on `simplify` is unknown, not missing."""
    known = SKILL_CAPABILITIES
    if manifest.get("x-source") != hosts.MANIFEST_SOURCE:
        known = tuple(c for c in known if c != "simplify")
    return [c for c in known if c not in manifest["capabilities"]]


def _recording_note(host, manifest):
    """One sentence on how gates 2-6 record on this host, plus any grumpy
    passes the manifest says are missing. Without a manifest, the host's
    default recording mode still applies; missing skills can't be known."""
    # The manifest's `x-recording` is a declaration for other consumers; the
    # host table is the one source here, so the two can't disagree.
    verify = f"`python3 {RECORD_GATE} --verify <pass>`"
    if hosts.recording_mode(host) == hosts.RECORDING_AUTOMATIC:
        note = (
            f"Each grumpy pass leaves a receipt, and this host ({host}) records "
            f"it at the end of the turn ({verify} records it now)."
        )
    else:
        note = (
            f"Each grumpy pass leaves a receipt; on this host ({host}), run "
            f"{verify} after each pass to record it."
        )
    if manifest is not None:
        missing = _missing_skills(manifest)
        if missing:
            note += (
                f" This host's manifest lists no grumpy {', '.join(missing)}: "
                "install grumpy, or re-init as tiny (/sdlc:gate --init tiny) "
                "if the change qualifies."
            )
    return note


def _install_pre_push(workdir):
    """Install or refresh sdlc's git pre-push floor whenever a cycle is armed,
    so the default path (auto-armed, /sdlc:gate never run) gets it too. The
    installer is idempotent. Returns a sentence for the arming message on a
    first install only, else "". Never fails the hook: the cycle is the thing
    that matters here, and the floor is an extra."""
    try:
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "install-pre-push.py"
        )
        spec = importlib.util.spec_from_file_location("install_pre_push", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        top = _git_output(["rev-parse", "--show-toplevel"], cwd=workdir) or "."
        status, _ = mod.install(top)
    except (Exception, SystemExit) as e:  # SystemExit: its git() on failure
        ho.notify("auto-init", f"pre-push hook not installed: {e}", surface=False)
        return ""
    if status != "installed":
        return ""
    return (
        " It also installed sdlc's git pre-push hook, which warns when a pushed "
        "branch's gates aren't done (`git config sharpen.prePush block` makes it "
        "refuse; delete .git/hooks/pre-push to remove it)."
    )


def _is_docs_path(path):
    base = os.path.basename(path)
    if base in DOCS_BASENAMES:
        return True
    _, ext = os.path.splitext(path)
    return ext.lower() in DOCS_EXTENSIONS


def _base(workdir):
    """The review round's base for the branch checked out in `workdir`
    (gate_base.snapshot, without the network `gh` lookup a hook must not
    make), or None."""
    branch = _git_output(["rev-parse", "--abbrev-ref", "HEAD"], cwd=workdir)
    if not branch or branch == "HEAD":
        return None
    return gb.snapshot(branch, "HEAD", cwd=workdir)


def _merge_base(workdir):
    """The base commit the branch's diff is measured from (gate_base: an
    explicit --set-base, then the branch's local upstream parent, then the default
    branch, origin/* before local), or None. /sdlc:gate's docs-only check
    diffs against the same `Base:` ref, so the two agree on a stack too."""
    return (_base(workdir) or {}).get("commit")


def _with_rounds(entry, rounds, base=None):
    entry[gc.ROUNDS_KEY] = rounds
    if base:
        entry[gc.BASE_KEY] = base
    return entry


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
    host = hosts.detect_host()
    manifest = None
    if cycle_exists:
        tier, tier_reason = DEFAULT_TIER, None  # unused: mutator no-ops below
        profile, capability_snapshot = None, None
        evidence, evidence_note = None, ""
    else:
        tier, tier_reason = _pick_tier(workdir)
        manifest = _load_host_manifest(host, workdir)
        profile, capability_snapshot = _resolve_portable_profile(manifest)
        evidence, evidence_note = _pick_evidence(manifest, host)
    try:
        # Idempotency check is inside the mutator so it runs under the exclusive
        # lock — one read, atomic guard, no write when cycle already exists.
        # Unlike `record-gate.py --init`, this does NOT clear a route pointing at
        # this worktree, and that asymmetry is deliberate: a route is an explicit
        # declaration that this session drives another worktree's gates, and a
        # background hook firing on an incidental commit has no business
        # revoking it. Only an explicit --init/--unroute tears a route down.
        # Round 1 of the advisory counter starts here on the default path.
        rounds = base = None
        if not cycle_exists:
            head = _git_output(["rev-parse", "HEAD"], cwd=workdir)
            base = _base(workdir)
            added = gc.added_lines(head, cwd=workdir, base=(base or {}).get("commit"))
            rounds = gc.next_rounds(None, head, added)
        result = gs.update_store(
            path,
            lambda d: (
                None
                if d.get(branch)
                else _with_rounds(
                    rc.init_gates(
                        d,
                        branch,
                        tier,
                        tier_reason=tier_reason,
                        profile=profile,
                        capabilities=capability_snapshot,
                        evidence=evidence,
                    ),
                    rounds,
                    base,
                )
            ),
        )
        if result is not None:
            problem = gb.write_base_file(branch, base, cwd=workdir)
            if problem:
                sys.stderr.write(f"[sdlc] warning: {problem}\n")
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
        profile_note = f" using portable profile {profile}" if profile else ""
        push_note = _install_pre_push(workdir)
        if tier == "tiny":
            # _pick_tier already confirmed every changed file is docs/text/
            # assets — gates 2-6 don't apply, so there's nothing to ask the
            # reader to widen unless that changes.
            detail = (
                f'It armed a tiny gate cycle for "{branch}"{profile_note} — every file '
                "changed so far looks like docs/text/assets, so the grumpy "
                "review gates don't apply. Run `/sdlc:gate --init "
                "small-medium` (or `significant`) if that changes." + push_note
            )
        else:
            detail = (
                f'It armed a {tier} gate cycle for "{branch}"{profile_note}. '
                + (
                    _recording_note(host, manifest)
                    if evidence == rc.EVIDENCE_RECEIPTS
                    else "Gates 2-6 need the grumpy skills to record, so "
                    "without them, re-init as tiny (/sdlc:gate --init tiny) if "
                    "the change qualifies."
                )
                + evidence_note
                + push_note
            )
        return ho.notify("auto-init", f"{COMMIT_OK}. {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
