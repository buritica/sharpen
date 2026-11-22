#!/usr/bin/env python3
"""Receipt-backed skill-gate evidence (sharpen#52, #53).

A cycle initialized with `evidence: receipts` stamps a skill gate only when
the grumpy pass that earns it left a receipt naming the branch's current HEAD.
The receipt is written by the pass itself, at its end, so "the skill was
loaded" (all a PostToolUse Skill event can prove) no longer counts as "the
work ran". This is a guard against skipping steps, not against forgery: we're
coworkers.

Kept apart from gate_store so the store stays about storage. Store shape it
adds to a branch entry:

  "evidence": "receipts",                        # absent on a legacy cycle
  "evidence_heads": {"grumpy-review": "<sha>"}   # HEAD each gate was earned on

`--init` replaces the whole entry, so a reset clears `evidence_heads` too.
"""

import json
import os
import subprocess
from datetime import datetime, timezone

import gate_store as gs

EVIDENCE_KEY = "evidence"
EVIDENCE_RECEIPTS = "receipts"
EVIDENCE_HEADS_KEY = "evidence_heads"
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


def uses_receipts(branch_data):
    """True when this cycle stamps skill gates only on a fresh receipt."""
    return (
        isinstance(branch_data, dict)
        and branch_data.get(EVIDENCE_KEY) == EVIDENCE_RECEIPTS
    )


def receipt_path(worktree_root, branch, skill):
    """Where the grumpy pass for `skill` leaves its receipt for `branch`.

    Same directory the passes already write their reports to
    (`<worktree>/.claude/grumpy/<branch>/`), which `/sdlc:init` and this
    repo already gitignore, so receipts never reach git."""
    name = RECEIPT_NAME_FOR_SKILL.get(skill)
    if not name or not worktree_root:
        return None
    return os.path.join(
        worktree_root, ".claude", "grumpy", branch, "receipts", f"{name}.json"
    )


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
        return None, (f"no receipt yet at {path}" if path else "no receipt location")
    try:
        with open(path) as f:
            receipt = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"receipt at {path} is unreadable ({e})"
    if not isinstance(receipt, dict):
        return None, f"receipt at {path} is not a JSON object"
    return receipt, None


def _predecessor_written(path, gate, branch_data):
    """When the pass a fix gate follows wrote ITS receipt (sibling file), or
    that gate's stamp when no such receipt is on disk. Comparing against the
    predecessor's receipt, not its stamp, keeps a late --verify or a sweep
    that records review AFTER fix finished from refusing the fix receipt."""
    if gate not in _FIX_PREDECESSOR:
        return None, None
    before, name = _FIX_PREDECESSOR[gate]
    sibling, _ = read_receipt(os.path.join(os.path.dirname(path), f"{name}.json"))
    ts = _parse_ts((sibling or {}).get("written_at"))
    if ts is None:
        ts = _parse_ts((branch_data or {}).get("gates", {}).get(before))
    return before, ts


def check_receipt(path, *, skill, gate, branch, head, branch_data, reachable=()):
    """Return (receipt_head, None) when the receipt at `path` earns `gate`,
    else (None, reason).

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
    started = _parse_ts((branch_data or {}).get("created_at"))
    if started and written < started:
        return None, f"receipt at {path} predates this gate cycle"
    before, prior = _predecessor_written(path, gate, branch_data)
    if prior and written <= prior:
        return None, f"receipt at {path} predates the {before} pass it must follow"
    return rhead, None


def receipt_heads(worktree_root, branch, head, cwd=None):
    """Receipt heads on disk for `branch` that are strict ancestors of
    `head`. Runs git, so call it before taking the store lock."""
    found = set()
    for skill in RECEIPT_NAME_FOR_SKILL:
        receipt, _ = read_receipt(receipt_path(worktree_root, branch, skill))
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
    heads are normal (fix commits), so only the last one decides."""
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
    if not last or last in attested or not heads.get(last) or heads[last] == head:
        return []
    return [g for g in recorded if heads.get(g) and heads[g] != head]
