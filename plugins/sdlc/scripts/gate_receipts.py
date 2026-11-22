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
# The gate a fix receipt must be newer than.
_FIX_PREDECESSOR = {
    "grumpy-fix-post-review": "grumpy-review",
    "grumpy-fix-post-imagine": "grumpy-imagine",
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


def check_receipt(path, *, skill, gate, branch, head, branch_data):
    """Return None when the receipt at `path` earns `gate`, else the reason.

    A receipt earns a gate when it exists, is for this skill and branch,
    names the branch's current `head`, was written after the cycle started,
    and (for a fix gate) after the gate it follows. Freshness is what keeps a
    receipt left over from an earlier cycle, or the first fix's receipt, from
    earning a later gate."""
    if not path or not os.path.isfile(path):
        return f"no receipt yet at {path}" if path else "no receipt location"
    try:
        with open(path) as f:
            receipt = json.load(f)
    except (OSError, ValueError) as e:
        return f"receipt at {path} is unreadable ({e})"
    if not isinstance(receipt, dict):
        return f"receipt at {path} is not a JSON object"
    if receipt.get("protocol_version") != RECEIPT_PROTOCOL:
        return f"receipt at {path} has an unknown protocol_version"
    if receipt.get("skill") != RECEIPT_NAME_FOR_SKILL.get(skill):
        return f"receipt at {path} is for {receipt.get('skill')!r}, not {skill}"
    if receipt.get("branch") != branch:
        return f'receipt at {path} is for branch "{receipt.get("branch")}"'
    if not head or receipt.get("head") != head:
        return (
            f"receipt at {path} reviewed {str(receipt.get('head'))[:12]}, but "
            f"{branch} is now at {str(head)[:12]} — re-run the pass"
        )
    written = _parse_ts(receipt.get("written_at"))
    if written is None:
        return f"receipt at {path} has no valid written_at"
    started = _parse_ts((branch_data or {}).get("created_at"))
    if started and written < started:
        return f"receipt at {path} predates this gate cycle"
    before = _FIX_PREDECESSOR.get(gate)
    if before:
        prior = _parse_ts((branch_data or {}).get("gates", {}).get(before))
        if prior and written <= prior:
            return f"receipt at {path} predates the {before} gate it must follow"
    return None


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
    skill gate stamped against an older head is stale."""
    if not uses_receipts(branch_data) or not head:
        return []
    heads = branch_data.get(EVIDENCE_HEADS_KEY, {})
    gates = branch_data.get("gates", {})
    stamped = [g for g in SKILL_GATE_ORDER if gates.get(g) and heads.get(g)]
    if not stamped or heads[stamped[-1]] == head:
        return []
    return [g for g in stamped if heads[g] != head]
