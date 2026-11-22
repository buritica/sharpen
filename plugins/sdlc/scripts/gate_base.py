#!/usr/bin/env python3
"""Which base a branch's review round covers, for stacked branches.

On a stack (main <- A <- B <- C), diffing C against origin/main reviews every
layer below it: the passes re-review A and B, a fix pass edits their code, the
round counter counts their lines as C's growth, and a carry over a restack
compares patches that include them. This module resolves one base for the
whole gate chain, and every consumer uses it: the grumpy passes (through
`.claude/sdlc/<branch>/base.json`), `--carry`, the round counter, auto-init's
tier pick and `--status`.

Resolution order, first hit wins:

  config    `git config branch.<branch>.sdlcBase <ref>`, set with
            `record-gate.py --set-base <ref>` (`--set-base auto` unsets it)
  pr        the open PR's base branch, from `gh pr view`, when the host has
            `gh` and the caller allows a network call (never from a hook)
  upstream  the branch's configured upstream, when it is another branch
            (`git branch --set-upstream-to=<parent>`), not its own remote copy
  default   the default branch (origin/HEAD, origin/main, origin/master,
            main, master)

The base commit is the merge-base of that ref and the branch head.

Host-neutral: stdlib, git, and optionally `gh`.
"""

import json
import os
import shutil
import subprocess

import gate_convergence as gc

CONFIG_KEY = "sdlcBase"
BASE_FILE = "base.json"
GH_TIMEOUT = 10

SOURCES = {
    "config": "set with --set-base",
    "pr": "the open PR's base branch",
    "upstream": "the branch's upstream",
    "default": "the default branch",
}


def _git(args, cwd=None):
    out = gc._run(args, cwd=cwd)
    return out.decode(errors="replace").strip() if out else ""


def _exists(ref, cwd=None):
    return bool(_git(["rev-parse", "--verify", "-q", f"{ref}^{{commit}}"], cwd))


def configured(branch, cwd=None):
    return _git(["config", "--get", f"branch.{branch}.{CONFIG_KEY}"], cwd) or None


def set_configured(branch, ref, cwd=None):
    """Set (or with ref None, unset) the explicit base. Returns an error
    string or None."""
    if ref is None:
        rc, _, _ = gc._git(
            ["config", "--unset", f"branch.{branch}.{CONFIG_KEY}"], cwd=cwd
        )
        return None if rc in (0, 5) else "git could not unset the base"
    # Pin a branch by name, never an expression that moves with HEAD
    # (`HEAD`, `HEAD~2`, `@{u}` is stored as the branch it names), and never
    # the branch itself: a base at HEAD measures an empty patch.
    full = _git(["rev-parse", "--symbolic-full-name", ref], cwd)
    if not full:
        # A parent that exists here only as a remote-tracking branch
        # (`origin/<ref>`), with no local branch of that name.
        for remote in _git(["remote"], cwd).split():
            candidate = f"refs/remotes/{remote}/{ref}"
            if _exists(candidate, cwd):
                full = candidate
                break
    if not full.startswith(("refs/heads/", "refs/remotes/")) or not _exists(full, cwd):
        return f'"{ref}" is not a local or remote branch here'
    own_remote = full.startswith("refs/remotes/") and (full.split("/", 3)[-1] == branch)
    if full == f"refs/heads/{branch}" or own_remote:
        return f'"{ref}" is the branch itself; name its parent branch'
    ref = _git(["rev-parse", "--abbrev-ref", full], cwd) or ref
    rc, _, _ = gc._git(["config", f"branch.{branch}.{CONFIG_KEY}", ref], cwd=cwd)
    return None if rc == 0 else "git could not set the base"


def _pr_base(branch, cwd=None):
    if not shutil.which("gh"):
        return None
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", branch, "--json", "baseRefName,state"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
        )
        info = json.loads(proc.stdout) if proc.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    name = info.get("baseRefName") if info.get("state") == "OPEN" else None
    if not name:
        return None
    for ref in (f"origin/{name}", name):
        if _exists(ref, cwd):
            return ref
    return None


def _upstream(branch, cwd=None):
    up = _git(["rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"], cwd)
    if not up or up == branch:
        return None
    local = _git(["config", "--get", f"branch.{branch}.remote"], cwd) == "."
    if not local and up.split("/", 1)[-1] == branch:
        # A remote-tracking upstream with the branch's own name is its pushed
        # copy, not a parent. A local upstream is always another branch.
        return None
    return up if _exists(up, cwd) else None


def _default(cwd=None):
    for ref in gc.default_branch_candidates(cwd):
        if _exists(ref, cwd):
            return ref
    return None


def resolve(branch, cwd=None, use_host=False):
    """{"ref", "source", "why"} for `branch`, or None when nothing resolves.
    `use_host` allows the `gh` lookup; hooks leave it off."""
    ref = configured(branch, cwd)
    if ref and _exists(ref, cwd):
        return {"ref": ref, "source": "config", "why": SOURCES["config"]}
    if use_host:
        ref = _pr_base(branch, cwd)
        if ref:
            return {"ref": ref, "source": "pr", "why": SOURCES["pr"]}
    ref = _upstream(branch, cwd)
    if ref:
        return {"ref": ref, "source": "upstream", "why": SOURCES["upstream"]}
    ref = _default(cwd)
    if ref:
        return {"ref": ref, "source": "default", "why": SOURCES["default"]}
    return None


def base_commit(ref, head, cwd=None):
    """The merge-base of `ref` and `head`, or None."""
    if not (ref and head):
        return None
    return _git(["merge-base", ref, head], cwd) or None


def describe(base):
    """One status line for a stored base, or None."""
    if not isinstance(base, dict) or not base.get("ref"):
        return None
    commit = str(base.get("commit") or "")[:12]
    at = f" at {commit}" if commit else ""
    return f"Base: {base['ref']}{at} ({base.get('why') or base.get('source')})"


def worktree_of(branch, cwd=None):
    """The root of the worktree that has `branch` checked out, else the
    caller's own worktree root."""
    out = _git(["worktree", "list", "--porcelain"], cwd)
    root = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            root = line[len("worktree ") :]
        elif line == f"branch refs/heads/{branch}" and root:
            return root
    return _git(["rev-parse", "--show-toplevel"], cwd) or None


def write_base_file(branch, base, cwd=None):
    """Write `.claude/sdlc/<branch>/base.json` (gitignored) for the grumpy
    passes. Best effort: returns the path, or None."""
    root = worktree_of(branch, cwd)
    if not root or not isinstance(base, dict):
        return None
    directory = os.path.join(root, ".claude", "sdlc", branch)
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, BASE_FILE)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(base, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
        return path
    except OSError:
        return None


def snapshot(branch, head, cwd=None, use_host=False):
    """The base record a cycle stores: resolve() plus the base commit."""
    base = resolve(branch, cwd, use_host)
    if not base:
        return None
    base["commit"] = base_commit(base["ref"], head, cwd)
    return base
