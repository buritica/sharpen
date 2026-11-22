#!/usr/bin/env python3
"""Receipt-backed skill-gate evidence (sharpen#52, #53).

A cycle initialized with `evidence: receipts` stamps a skill gate only when
the grumpy pass that earns it left a receipt naming a commit on the branch
(HEAD, or an ancestor once a later pass has committed).
The receipt is written by the pass itself, at its end, so "the skill was
loaded" (all a PostToolUse Skill event can prove) no longer counts as "the
work ran". This is a guard against skipping steps, not against forgery: we're
coworkers.

Kept apart from gate_store so the store stays about storage. Store shape it
adds to a branch entry:

  "evidence": "receipts",                        # absent on a legacy cycle
  "evidence_heads": {"grumpy-review": "<sha>"}   # HEAD each gate was earned on
  "evidence_executors": {"grumpy-review": {"host": "oneshot", ...}}
                                                 # set only when the receipt names a
                                                 # non-agent executor (grumpy oneshot.py)

`--init` replaces the whole entry, so a reset clears `evidence_heads` too.
"""

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone

import gate_convergence as gc
import gate_store as gs
from artifact_dirs import GRUMPY_ROOT, LEGACY_GRUMPY_ROOT

EVIDENCE_KEY = "evidence"
EVIDENCE_RECEIPTS = "receipts"
EVIDENCE_HEADS_KEY = "evidence_heads"
# gate -> the receipt's `executor` when a path other than an agent host ran the
# pass (grumpy's oneshot.py). Shown by --status so a one-shot review is never
# mistaken for an agent review. Mirrored in gate_store.format_status.
EVIDENCE_EXECUTORS_KEY = "evidence_executors"
RECEIPT_PROTOCOL = "1"
# Skill (as seen on the Skill tool) -> receipt file name. grumpy:fix writes
# one `fix` receipt per run; which fix gate it earns is decided by
# determine_gate, and freshness against the preceding gate keeps the second
# fix from reusing the first one's receipt.
RECEIPT_NAME_FOR_SKILL = {
    "grumpy:simplify": "simplify",
    "grumpy:review": "review",
    "grumpy:imagine": "imagine",
    "grumpy:fix": "fix",
}
# Fix gate -> the stage-specific receipt grumpy 2.11+ writes for it
# (`receipt.py fix --stage review|imagine`). An older grumpy writes one
# `fix.json` for both; that file is still read when no stage file exists.
FIX_STAGE_RECEIPT = {
    "grumpy-fix-post-review": "fix-review",
    "grumpy-fix-post-imagine": "fix-imagine",
}
# gate -> written_at of the receipt that earned it, so one receipt can never
# earn both fix gates (a skipped --verify after the first fix used to let the
# second fix's receipt record both).
EVIDENCE_RECEIPT_KEY = "evidence_receipts"
# Fix gate -> (the gate it follows, that pass's receipt name). A fix receipt
# must be newer than the receipt of the pass it follows.
_FIX_PREDECESSOR = {
    "grumpy-fix-post-review": ("grumpy-review", "review"),
    "grumpy-fix-post-imagine": ("grumpy-imagine", "imagine"),
}
# `SDLC_GATE_EVIDENCE=events` makes new cycles legacy (event-only) cycles.
# A migration and test escape hatch, not a recommended mode.
EVIDENCE_ENV = "SDLC_GATE_EVIDENCE"
# Chain order of the skill-gated gates, for staleness checks.
SKILL_GATE_ORDER = [g for g in gs.ALL_GATE_NAMES if g in gs.SKILL_FOR_GATE]


def default_evidence():
    """Evidence mode for a NEW cycle: receipts, unless opted out."""
    if os.environ.get(EVIDENCE_ENV, "").strip().lower() == "events":
        return None
    return EVIDENCE_RECEIPTS


def init_gates(data, branch, tier, evidence=None, **kwargs):
    """`gs.init_gates`, then mark the cycle receipt-backed when `evidence` is
    "receipts". A cycle without the key keeps the old event-only recording."""
    if evidence not in (None, EVIDENCE_RECEIPTS):
        raise ValueError(f'Invalid evidence mode "{evidence}"')
    entry = gs.init_gates(data, branch, tier, **kwargs)
    if evidence:
        entry[EVIDENCE_KEY] = evidence
    return entry


def stamp_head(data, branch, gate, head):
    """Remember which commit `gate` was earned on. Call right after the
    record_gate that stamped it, inside the same store mutator."""
    if head:
        data[branch].setdefault(EVIDENCE_HEADS_KEY, {})[gate] = head


def _stamp_receipt_identity(data, branch, gate, receipt):
    written = receipt.get("written_at")
    if gate in FIX_STAGE_RECEIPT and written:
        data[branch].setdefault(EVIDENCE_RECEIPT_KEY, {})[gate] = written


def uses_receipts(branch_data):
    """True when this cycle stamps skill gates only on a fresh receipt."""
    return (
        isinstance(branch_data, dict)
        and branch_data.get(EVIDENCE_KEY) == EVIDENCE_RECEIPTS
    )


def receipt_path(worktree_root, branch, skill, gate=None):
    """Where the grumpy pass for `skill` leaves its receipt for `branch`.

    Same directory the passes already write their reports to
    (`<worktree>/.sharpen/grumpy/<branch>/`). `receipt.py` drops a
    `.gitignore` holding `*` into `.sharpen/grumpy/`, and `/sdlc:init` lists
    it in the repo's `.gitignore` too, so receipts never reach git. For a fix
    gate, the stage-specific receipt wins when it exists.

    A grumpy before 2.15 wrote to `.claude/grumpy/` instead, and two hosts or
    sessions can run different grumpy versions on one branch. So for each
    receipt name both folders are read and the newer receipt wins (by its
    `written_at`, else file mtime), not the one in the newer folder: a stale
    2.15 receipt never shadows a fresh one an older grumpy wrote. Every
    freshness and report check applies to a legacy receipt unchanged (its
    report sits next to it, in the old folder)."""
    name = RECEIPT_NAME_FOR_SKILL.get(skill)
    if not name or not worktree_root:
        return None
    stage = FIX_STAGE_RECEIPT.get(gate)
    for candidate in [stage, name] if stage else [name]:
        found = _newest(_receipt_copies(worktree_root, branch, candidate))
        if found:
            return found
    return os.path.join(worktree_root, *GRUMPY_ROOT, branch, "receipts", f"{name}.json")


def _receipt_copies(worktree_root, branch, name):
    """Existing copies of receipt `name` for `branch`, new folder first."""
    paths = (
        os.path.join(worktree_root, *root, branch, "receipts", f"{name}.json")
        for root in (GRUMPY_ROOT, LEGACY_GRUMPY_ROOT)
    )
    return [p for p in paths if os.path.exists(p)]


def _age_key(path):
    """Sort key for a receipt: (readable, how recent). Recency is its
    written_at, else its mtime. An unreadable copy sorts below every readable
    one, so a corrupt or truncated receipt in one folder never shadows a good
    one in the other; with no readable copy it is still returned, and refused
    later by name."""
    receipt, _ = read_receipt(path)
    ts = _parse_ts((receipt or {}).get("written_at"))
    if ts is None:
        try:
            ts = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
        except OSError:
            ts = datetime.min.replace(tzinfo=timezone.utc)
    return (receipt is not None, ts)


def _newest(paths):
    """The most recent readable one of `paths`, the first one on a tie, or
    None."""
    best, best_key = None, None
    for path in paths:
        key = _age_key(path)
        if best is None or key > best_key:
            best, best_key = path, key
    return best


def _worktree_of(path, branch):
    """The worktree root a receipt at `path` (`<wt>/<root>/<branch>/receipts/
    <name>.json`) sits under, or None when it isn't laid out that way."""
    base = os.path.dirname(os.path.dirname(path))  # <wt>/<root>/<branch>
    for _ in branch.split("/"):
        base = os.path.dirname(base)
    root = (os.path.basename(os.path.dirname(base)), os.path.basename(base))
    if root not in (GRUMPY_ROOT, LEGACY_GRUMPY_ROOT):
        return None
    return os.path.dirname(os.path.dirname(base))


def _parse_ts(value):
    try:
        ts = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def read_receipt(path):
    """The receipt at `path` as a dict, or (None, reason)."""
    if not path or not os.path.isfile(path):
        if not path:
            return None, "no receipt location"
        return None, (
            f"no receipt yet at {path} (grumpy 2.15+ writes one there; "
            "grumpy 2.9 to 2.14 under .claude/grumpy/ instead)"
        )
    try:
        with open(path) as f:
            receipt = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"receipt at {path} is unreadable ({e})"
    if not isinstance(receipt, dict):
        return None, f"receipt at {path} is not a JSON object"
    return receipt, None


def _predecessor_written(path, gate, branch_data, branch=None):
    """When the pass a fix gate follows wrote ITS receipt, or that gate's
    stamp when no such receipt is on disk. The predecessor's receipt is looked
    for in both folders (the newer copy wins, as in `receipt_path`), so a
    grumpy upgrade between review and fix keeps the ordering check. Comparing
    against the predecessor's receipt, not its stamp, keeps a late --verify or
    a sweep that records review AFTER fix finished from refusing the fix
    receipt."""
    if gate not in _FIX_PREDECESSOR:
        return None, None
    before, name = _FIX_PREDECESSOR[gate]
    # A path receipt_path didn't lay out (`<wt>/<root>/<branch>/receipts/`)
    # has no second folder to look in; its own sibling is the only candidate.
    sibling = os.path.join(os.path.dirname(path), f"{name}.json")
    worktree = _worktree_of(path, branch) if branch else None
    if worktree:
        sibling = _newest(_receipt_copies(worktree, branch, name)) or sibling
    prior, _ = read_receipt(sibling)
    ts = _parse_ts((prior or {}).get("written_at"))
    if ts is None:
        ts = _parse_ts((branch_data or {}).get("gates", {}).get(before))
    return before, ts


def _report_mismatch(path, receipt):
    """(refusal, warning) about the report the receipt names. Gone: refuse,
    the gate means "the pass ran and left evidence" and there is none.
    Changed since: warn and still record. Someone tidying a report after the
    pass is not a reason to stop a coworker (no tamper protection)."""
    name, digest = receipt.get("report"), receipt.get("report_sha256")
    if not isinstance(name, str) or not isinstance(digest, str) or "/" in name:
        return f"receipt at {path} names no report (grumpy 2.9.0+ records one)", None
    report = os.path.join(os.path.dirname(os.path.dirname(path)), name)
    try:
        with open(report, "rb") as f:
            actual = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return (
            f"the report this receipt names is missing ({report}), re-run the pass",
            None,
        )
    if actual != digest:
        return None, f"{report} changed after the receipt was written"
    return None, None


def _fix_gate_refusal(path, gate, receipt, branch_data):
    """Why a fix receipt can't earn `gate`, or None: a stage receipt names its
    own fix gate, and one receipt never earns both fix gates."""
    stage = receipt.get("stage")
    if stage and FIX_STAGE_RECEIPT.get(gate) != f"fix-{stage}":
        return f"receipt at {path} is for the fix after {stage}, not {gate}"
    used = (branch_data or {}).get(EVIDENCE_RECEIPT_KEY, {})
    for other, stamp in (used if isinstance(used, dict) else {}).items():
        if other != gate and stamp == receipt.get("written_at"):
            return (
                f"receipt at {path} already earned {other}; one fix run earns "
                f"one fix gate, so run /grumpy:fix again for {gate}"
            )
    return None


def check_receipt(path, *, skill, gate, branch, head, branch_data, reachable=()):
    """Return (receipt, None) when the receipt at `path` earns `gate`, else
    (None, reason).

    A receipt earns a gate when it is for this skill and branch, names the
    branch's current `head` or a commit in `reachable` (ancestors of it the
    caller already checked with git: a later pass such as fix commits before
    the sweep runs), was written after the cycle started, and (for a fix gate)
    after the receipt of the pass it follows. The gate is stamped with the
    receipt's head; enforce then requires the LAST skill gate to sit on HEAD."""
    receipt, reason = read_receipt(path)
    if reason:
        return None, reason
    if receipt.get("protocol_version") != RECEIPT_PROTOCOL:
        return None, f"receipt at {path} has an unknown protocol_version"
    if receipt.get("skill") != RECEIPT_NAME_FOR_SKILL.get(skill):
        return None, f"receipt at {path} is for {receipt.get('skill')!r}, not {skill}"
    if receipt.get("branch") != branch:
        return None, f'receipt at {path} is for branch "{receipt.get("branch")}"'
    rhead = receipt.get("head")
    if not head or not rhead or (rhead != head and rhead not in reachable):
        return None, (
            f"receipt at {path} reviewed {str(rhead)[:12]}, which is not on "
            f"{branch} at {str(head)[:12]}, re-run the pass"
        )
    written = _parse_ts(receipt.get("written_at"))
    if written is None:
        return None, f"receipt at {path} has no valid written_at"
    reason, warning = _report_mismatch(path, receipt)
    if reason:
        return None, reason
    started = _parse_ts((branch_data or {}).get("created_at"))
    if started and written < started:
        return None, f"receipt at {path} predates this gate cycle"
    before, prior = _predecessor_written(path, gate, branch_data, branch)
    if prior and written <= prior:
        return None, f"receipt at {path} predates the {before} pass it must follow"
    reason = _fix_gate_refusal(path, gate, receipt, branch_data)
    if reason:
        return None, reason
    if warning:
        receipt = dict(receipt, warning=warning)
    return receipt, None


def stamp_receipt(data, branch, gate, receipt, invalidated, before):
    """After record_gate_and_diff stamped `gate` from `receipt`: remember the
    commit it was earned on, and put back any tests/lint/typecheck stamp
    taken AFTER the receipt was written. Those ran against the pass's
    committed code, so a late --verify or sweep must not clear them. Returns
    the gates that stay cleared."""
    bd = data[branch]
    stamp_head(data, branch, gate, receipt.get("head"))
    _stamp_receipt_identity(data, branch, gate, receipt)
    executor = receipt.get("executor")
    executors = bd.setdefault(EVIDENCE_EXECUTORS_KEY, {})
    if isinstance(executor, dict) and executor.get("host"):
        executors[gate] = {
            k: str(executor[k]) for k in ("host", "provider", "model") if k in executor
        }
    else:
        executors.pop(gate, None)  # re-earned by an agent host
    if not executors:
        bd.pop(EVIDENCE_EXECUTORS_KEY, None)
    written = _parse_ts(receipt.get("written_at"))
    cleared = []
    for g in invalidated:
        ran = _parse_ts(before.get(g))
        if written and ran and ran > written:
            bd.setdefault("gates", {})[g] = before[g]
            bd.get("invalidated_by", {}).pop(g, None)
        else:
            cleared.append(g)
    return cleared


def receipt_heads(worktree_root, branch, head, cwd=None):
    """Receipt heads on disk for `branch` that are strict ancestors of
    `head`. Runs git, so call it before taking the store lock."""
    found = set()
    paths = [receipt_path(worktree_root, branch, s) for s in RECEIPT_NAME_FOR_SKILL]
    paths += [
        receipt_path(worktree_root, branch, "grumpy:fix", g) for g in FIX_STAGE_RECEIPT
    ]
    for path in paths:
        receipt, _ = read_receipt(path)
        rhead = (receipt or {}).get("head")
        if isinstance(rhead, str) and rhead and rhead != head:
            found.add(rhead)
    reachable = set()
    for sha in found:
        try:
            gs._git("merge-base", "--is-ancestor", sha, head, cwd=cwd or worktree_root)
        except (OSError, subprocess.CalledProcessError):
            continue
        reachable.add(sha)
    return frozenset(reachable)


def worktree_heads(cwd=None):
    """{branch: (worktree_root, head_sha)} for every checked-out branch, or
    None when `git worktree list` fails. One subprocess, run before taking the
    store lock (no git inside a mutator)."""
    try:
        out = gs._git("worktree", "list", "--porcelain", cwd=cwd)
    except (OSError, subprocess.CalledProcessError):
        return None
    heads = {}
    root = sha = None
    for line in out.splitlines() + [""]:
        if line.startswith("worktree "):
            root, sha = os.path.realpath(line[len("worktree ") :]), None
        elif line.startswith("HEAD "):
            sha = line[len("HEAD ") :]
        elif line.startswith("branch refs/heads/") and root:
            heads[line[len("branch refs/heads/") :]] = (root, sha)
        elif not line:
            root = sha = None
    return heads


def stale_skill_gates(branch_data, head):
    """Skill gates whose evidence no longer matches `head`, in chain order.

    Only receipt-backed cycles carry evidence heads; anything else returns
    []. The rule is strict: once the last recorded skill gate's head differs
    from the branch's head, a commit landed after the review chain, and every
    skill gate stamped against an older head is stale. Earlier gates on older
    heads are normal (fix commits), so only the last one decides. A recorded
    carry (gate_convergence) moves a gate's head forward to the commit it was
    carried to, so carried gates count as on that commit."""
    if not uses_receipts(branch_data) or not head:
        return []
    heads = branch_data.get(EVIDENCE_HEADS_KEY, {})
    gates = branch_data.get("gates", {})
    recorded = [g for g in SKILL_GATE_ORDER if gates.get(g)]
    # A last gate with no head was attested (--attest): a person vouched for
    # it, so there is nothing to compare. Judging by the gate before it would
    # block a PR the person just unblocked.
    attested = branch_data.get("attestations") or {}
    last = recorded[-1] if recorded else None
    # A headless gate that nobody attested was stamped without a receipt (a
    # session still running a pre-receipt sdlc): no evidence, so stale.
    if not last or last in attested or _on(branch_data, heads.get(last), head):
        return []
    return [
        g
        for g in recorded
        if g not in attested and not _on(branch_data, heads.get(g), head)
    ]


def _on(branch_data, evidence, head):
    """True when a gate earned on `evidence` counts as on `head`: the same
    commit, or one a recorded carry reached from it."""
    return bool(evidence) and head in gc.carried_heads(branch_data, evidence)


def carry_chain(branch_data):
    """The commits the last recorded skill gate counts as on: its evidence
    head, then each commit recorded carries reached from it. [] when no skill
    gate carries an evidence head."""
    gates = (branch_data or {}).get("gates", {})
    recorded = [g for g in SKILL_GATE_ORDER if gates.get(g)]
    if not recorded:
        return []
    head = (branch_data or {}).get(EVIDENCE_HEADS_KEY, {}).get(recorded[-1])
    return gc.carried_heads(branch_data, head) if head else []


def carry_source(branch_data, head=None):
    """The commit a new carry starts from, or None. With `head`, the newest
    commit in the chain that is in `head`'s history, so a reset back past a
    carry and a new commit carries from where HEAD actually forked."""
    chain = carry_chain(branch_data)
    if not chain:
        return None
    if head:
        for commit in reversed(chain):
            if commit == head or gc.is_ancestor(commit, head):
                return commit
    return chain[-1]


def branch_head(branch, cwd=None):
    """The commit `branch` points at locally, or None (e.g. a fork's
    `--head owner:branch` that isn't a local ref): staleness then can't be
    judged and is skipped rather than guessed."""
    try:
        return gs._git("rev-parse", "--verify", "-q", f"refs/heads/{branch}", cwd=cwd)
    except (OSError, subprocess.CalledProcessError):
        return None


def stale_message(branch_data, branch, stale):
    """Why these gates are stale and how to get out, shared by enforce and
    --status/--oneline."""
    heads = branch_data.get(EVIDENCE_HEADS_KEY, {})
    seen = ", ".join(
        f"{g} ({'reviewed ' + heads[g][:12] if heads.get(g) else 'no receipt'})"
        for g in stale
    )
    return (
        f"Stale ({len(stale)}): {seen}. A commit landed after the review chain "
        f'on "{branch}". If those commits are a clean rebase, release bookkeeping, whitespace, '
        "safe docs, or a small comment fix, carry the gates instead: "
        "record-gate.py --carry (it says which applies, and re-opens "
        "tests/lint/typecheck). Otherwise re-run the chain on the current "
        f"HEAD: /sdlc:gate --init {branch_data.get('tier')}, then the passes again."
    )


def stale_suffix(branch_data, branch, cwd=None):
    """A status-line warning when enforce would block as stale, else ''."""
    if not uses_receipts(branch_data):
        return ""
    stale = stale_skill_gates(branch_data, branch_head(branch, cwd))
    return f"\n⚠ {stale_message(branch_data, branch, stale)}" if stale else ""
