#!/usr/bin/env python3
"""Portable CI gate validator.

Reads the shared gate store for the PR head branch and verifies:
  1. A gate cycle exists for the branch (opt-in: no cycle -> allow).
  2. All required gates for the tier are recorded.
  3. If a review report is attached, its provenance matches the PR head SHA.

This is the cross-clone enforcement point the local PreToolUse hook cannot be:
it runs in CI against the PR's own commits, so forked PRs and agents without
local hooks are held to the same standard. The local hook remains the fast
feedback loop; this script is the merge authority.

Pure stdlib. Exits 0 on allow, 1 on deny, 2 on unrecoverable error.
"""

import os
import subprocess
import sys

import gate_store as gs
import review_report


def _log(msg):
    sys.stderr.write(msg + "\n")


def _git(*args, cwd=None):
    return (
        subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.DEVNULL)
        .decode()
        .strip()
    )


def _resolve_head_sha(cwd=None):
    """The commit the PR is proposing to merge.

    In GitHub Actions this is `github.event.pull_request.head.sha`, exposed as
    `GITHUB_SHA` for `pull_request` events. Locally, fall back to HEAD.
    """
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        return sha
    try:
        return _git("rev-parse", "HEAD", cwd=cwd)
    except (OSError, subprocess.CalledProcessError):
        return None


def _resolve_branch(cwd=None):
    """The branch the gate cycle is keyed under.

    In GitHub Actions this is `github.event.pull_request.head.ref`, exposed as
    `GITHUB_HEAD_REF` for `pull_request` events. Locally, fall back to the
    current branch.
    """
    branch = os.environ.get("GITHUB_HEAD_REF")
    if branch:
        return branch
    return gs.detect_branch(cwd)


def validate(branch, head_sha, cwd=None):
    """Return (allowed, reason) for the branch's gate cycle.

    allowed=True means the PR may merge. reason is None on allow, a string on
    deny. The caller decides how to surface it.
    """
    path = gs.default_store_path(cwd)
    try:
        data = gs.load_store(path)
    except gs.StoreCorruptError as e:
        return False, f"gate store at {path} is unreadable: {e}"

    bd = data.get(branch)
    if not bd:
        return True, None  # opt-in: no cycle -> allow

    missing = gs.missing_gates(bd)
    if missing:
        tier = bd.get("tier", "unknown")
        completed = gs.completed_gates(bd)
        required = gs.required_gates(bd)
        return False, (
            f'SDLC gates incomplete for branch "{branch}" (tier "{tier}").\n\n'
            f"Completed ({len(completed)}/{len(required)}): "
            f"{', '.join(completed) or '(none)'}\n"
            f"Missing ({len(missing)}): {', '.join(missing)}\n\n"
            "Run the gate chain locally, then push the updated branch."
        )

    report = bd.get("review_report")
    if report is not None:
        try:
            review_report.validate_report(report)
        except ValueError as e:
            return False, f"attached review report is invalid: {e}"
        provenance = report.get("provenance", {})
        if provenance.get("kind") == "git-range":
            report_head = provenance.get("head", "")
            if head_sha and report_head != head_sha:
                return False, (
                    f"review report head {report_head} does not match "
                    f"PR head {head_sha}. The review was run against a "
                    "different commit than the one being merged."
                )
        if report.get("status") == "fail":
            return False, (
                "attached review report has status=fail. "
                "Fix the findings and re-run the review before merging."
            )

    return True, None


def main():
    branch = _resolve_branch()
    if not branch:
        _log("[ci-validate] could not detect branch — not a PR event?")
        return 2
    head_sha = _resolve_head_sha()
    allowed, reason = validate(branch, head_sha)
    if not allowed:
        _log(f"[ci-validate] BLOCKED: {reason}")
        return 1
    _log(f"[ci-validate] gates complete for {branch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
