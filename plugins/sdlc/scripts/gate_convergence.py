#!/usr/bin/env python3
"""Keep a receipt-backed gate chain from re-running for commits that can't
change what the review saw (sharpen#37), and count the rounds it does re-run.

Two pieces, both host-neutral (stdlib + git):

* **Carry.** A receipt-backed cycle goes stale the moment HEAD moves past the
  last skill gate (gate_receipts.stale_skill_gates). `classify_carry` names the
  commits that may carry the skill gates forward instead of a full reset:

    rebase  HEAD is what git produces replaying the reviewed commits onto
            the new base with no conflicts (`_clean_replay`), or the
            branch's own changed lines are byte-for-byte unchanged, each side
            taken against its own base (`_branch_patch`; context lines a
            lower layer rewrote around the hunks don't count)
    format  only whitespace at line ends and blank lines changed (never
            indentation or whitespace inside a line)
    docs    every changed path, both sides of a rename included, is on the
            docs safe list (`is_docs_safe`)
    bookkeeping
            the reviewed patch and the current one differ only in release
            bookkeeping: version fields of the plugin and bundle manifests,
            the AGENTS.md/CLAUDE.md render stamp, and UPDATE.md/CHANGELOG.md
            notes that kept all their reviewed text (see BOOKKEEPING_*). In a
            stack nearly every rebase conflicts on exactly these files
    manual  anything else the operator vouches for with a reason: at most
            MANUAL_CARRY_MAX_LINES changed lines and no new files. The reason
            is the only check that it is a comment or wording fix; this is the
            honest path for a small wording fix in an agent-executed file,
            recorded as a carry, never as a review. On a rewrite (a restack
            that resolved a conflict) the lines counted are those where the
            reviewed and the current branch patch differ (sharpen#65), not
            the whole diff between the two heads, which takes in every change
            the layers below made

  Anything else stays strict. `apply_carry` records the hop (from, to, class,
  reason) and clears tests/lint/typecheck so they run again on the new HEAD;
  the skill gates keep the head they were earned on, and staleness follows the
  recorded hops.

* **Rounds.** A `--init` that replaces a cycle which had already earned skill
  gates is a new round. The store keeps each round's head and the branch's
  added-line count, so `--init` and `--status` can say how much the branch has
  grown since round 1. Advisory only: it never blocks.

Store shape this adds to a branch entry:

  "carries": [{"from": sha, "to": sha, "class": "rebase", "reason": str|null,
               "at": iso}]                 # cleared by --init
  "rounds":  [{"head": sha, "added": int|null, "at": iso}]   # survives --init
"""

import difflib
import os
import re
import subprocess

import gate_store as gs

CARRIES_KEY = "carries"
# The review round's base (gate_base.snapshot): {ref, source, why, commit}.
BASE_KEY = "base"
ROUNDS_KEY = "rounds"
MANUAL_CARRY_MAX_LINES = 30
# From this round on, `--init` and `--status` suggest shipping or splitting.
ROUND_ADVICE_FROM = 3
RERUN_AFTER_CARRY = ("tests", "lint", "typecheck")

# The docs safe list. In a repo of agent plugins the markdown IS the product:
# a command, skill, agent, hook or template file is executed by an agent, so
# no extension makes it safe. A path is docs-safe only when it is NOT under an
# agent-executed location AND it is one of: a top-level-style project doc by
# basename (README, CHANGELOG, ...), a prose file under a `docs/` directory,
# or a raster image. Everything else (including UPDATE.md, plain *.md
# elsewhere, and .svg) is not safe, and the carry falls back to strict.
DOCS_SAFE_BASENAME_STEMS = {
    "README",
    "CHANGELOG",
    "CHANGES",
    "HISTORY",
    "LICENSE",
    "NOTICE",
    "COPYING",
    "CONTRIBUTING",
    "AUTHORS",
}
DOCS_DIR_EXTENSIONS = {".md", ".mdx", ".txt", ".rst", ".adoc"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"}
# Path segments an agent loads and acts on. A match anywhere in the path wins
# over every safe rule above.
AGENT_EXECUTED_SEGMENTS = {
    "commands",
    "skills",
    "agents",
    "hooks",
    "prompts",
    "templates",
    "extensions",
    "scripts",
    ".claude",
    ".claude-plugin",
    ".codex",
    ".cursor",
    ".github",
    ".githooks",
    ".pi",
}
AGENT_EXECUTED_BASENAMES = {
    "SKILL.md",
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    ".cursorrules",
    "copilot-instructions.md",
}


def is_docs_safe(path):
    """True only for a path on the docs safe list (see the module doc)."""
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if not parts:
        return False
    base = parts[-1]
    if base in AGENT_EXECUTED_BASENAMES:
        return False
    if any(p in AGENT_EXECUTED_SEGMENTS for p in parts[:-1]):
        return False
    stem, ext = os.path.splitext(base)
    ext = ext.lower()
    if stem.upper() in DOCS_SAFE_BASENAME_STEMS and ext in ("", *DOCS_DIR_EXTENSIONS):
        return True
    if ext in IMAGE_EXTENSIONS:
        return True
    return "docs" in parts[:-1] and ext in DOCS_DIR_EXTENSIONS


# Pin every setting that changes what `git diff` prints, so the classifier
# never parses a user's configured format: no prefixes (diff.noprefix) would
# leave no file headers to split on, an external driver or textconv prints
# whatever it likes, and quoted paths must stay ASCII.
_GIT_CONFIG = [
    "-c",
    "diff.noprefix=false",
    "-c",
    "diff.mnemonicPrefix=false",
    "-c",
    "core.quotePath=true",
    "-c",
    "color.ui=never",
]
_DIFF_FLAGS = [
    "--no-ext-diff",
    "--no-textconv",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    "--binary",
    "--no-renames",
]
# The last git failure's stderr, so a refusal can say what git said instead of
# sending the operator to a reset for an environment problem.
last_git_error = None


def _git(args, cwd=None, stdin=None):
    """(returncode, stdout, stderr) of a git call; returncode None when git
    could not be run at all."""
    global last_git_error
    try:
        proc = subprocess.run(
            ["git", *_GIT_CONFIG, *args],
            cwd=cwd,
            input=stdin,
            capture_output=True,
        )
    except OSError as exc:
        last_git_error = str(exc)
        return None, b"", b""
    if proc.returncode != 0:
        last_git_error = proc.stderr.decode(errors="replace").strip() or (
            f"git {args[0]} exited {proc.returncode}"
        )
    return proc.returncode, proc.stdout, proc.stderr


def _run(args, cwd=None, stdin=None):
    """git output, or None on any failure (callers fall back to strict)."""
    rc, out, _ = _git(args, cwd=cwd, stdin=stdin)
    return out if rc == 0 else None


def _git_failure(what):
    """A refusal for a git failure: it says what git said and that the fix is
    in the repo, not a reset."""
    detail = f": {last_git_error}" if last_git_error else ""
    return (
        f"git could not {what}{detail}. This is a repo problem, not a reason to "
        "reset: fix it (e.g. `git fetch --unshallow`, `git remote set-head "
        "origin --auto`) and run --carry again"
    )


def default_branch_candidates(cwd=None):
    """Diff bases to try, most authoritative first: origin/HEAD's target when
    known, then origin/main, origin/master, main, master."""
    candidates = []
    symbolic = _run(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=cwd)
    symbolic = symbolic.decode(errors="replace").strip() if symbolic else ""
    if symbolic.startswith("refs/remotes/"):
        candidates.append(symbolic[len("refs/remotes/") :])
    for base in ("origin/main", "origin/master", "main", "master"):
        if base not in candidates:
            candidates.append(base)
    return candidates


def merge_base(rev, cwd=None):
    """Merge-base of `rev` with the first resolvable default branch, or None."""
    for base in default_branch_candidates(cwd):
        out = _run(["merge-base", base, rev], cwd=cwd)
        if out:
            return out.decode(errors="replace").strip()
    return None


def _branch_patch(rev, cwd=None, base=None):
    """The branch's own changed lines at `rev`, file by file and in order,
    without the parts a restack changes around them (blob ids, hunk headers,
    context lines), or None. Compared byte-for-byte, unlike `git patch-id`,
    which ignores whitespace and would let a conflict resolution that
    re-indents a line pass as a rebase.
    `base` is the review round's base commit (gate_base); without it, the
    merge-base with the default branch."""
    base = base or merge_base(rev, cwd)
    if not base:
        return None
    diff = _run(["diff", *_DIFF_FLAGS, f"{base}..{rev}"], cwd=cwd)
    if diff is None:
        return None
    kept = []
    for line in diff.split(b"\n"):
        # Only the branch's own lines are compared: blob ids, hunk headers and
        # context lines all come from outside the branch's hunks, so a lower
        # layer rewriting text next to them (or above them) changes them while
        # the branch's own change is the same. Hunk boundaries go too: a
        # shorter gap between two hunks can merge them. The +/- lines stay in
        # order, file by file, and the carry re-opens tests, lint and typecheck.
        if line.startswith((b"index ", b"@@", b" ")):
            continue
        kept.append(line)
    return b"\n".join(kept)


# Release bookkeeping, as explicit files and fields. Anything outside this list
# must match exactly for the `bookkeeping` class.
BOOKKEEPING_VERSION_FILES = re.compile(
    r"^(\.claude-plugin/marketplace\.json|package\.json|"
    r"plugins/[^/]+/\.claude-plugin/plugin\.json|plugins/[^/]+/package\.json)$"
)
BOOKKEEPING_STAMP_FILES = re.compile(r"^(AGENTS\.md|CLAUDE\.md)$")
BOOKKEEPING_NOTE_FILES = re.compile(r"^plugins/[^/]+/(UPDATE|CHANGELOG)\.md$")
# The only changed lines those files may differ in.
_VERSION_LINE = re.compile(rb'^[+-]\s*"version":\s*"[^"]*",?\s*$')
_STAMP_LINE = re.compile(
    rb"^[+-]<!-- rendered by sdlc \S+ from templates/agents-sdlc\.md; .* -->$"
)
_SEMVER = re.compile(rb"\d+\.\d+\.\d+")


def _split_by_file(patch):
    """{path: that file's part of a `_branch_patch`}, or None when the patch
    has changes but no file header was recognized (never read that as
    "nothing differs")."""
    files, path = {}, None
    for line in patch.split(b"\n"):
        if line.startswith(b"diff --git a/"):
            path = line.split(b" b/", 1)[-1].decode(errors="replace")
            files[path] = []
        if path is not None:
            files[path].append(line)
    if not files and patch.strip():
        return None
    return {p: b"\n".join(lines) for p, lines in files.items()}


def _changes(file_patch, allowed):
    """The +/- lines of one file's patch, minus those `allowed` matches."""
    return [
        line
        for line in file_patch.split(b"\n")
        if line[:1] in (b"+", b"-")
        and not line.startswith((b"+++", b"---"))
        and not allowed.match(line)
    ]


def _note_lines(file_patches, paths, sign=b"+"):
    """Non-blank added (or removed) lines of the note files, version numbers
    normalized. Blank lines are left out: diff alignment moves them freely."""
    lines = (
        _SEMVER.sub(b"X", line[1:]).strip()
        for p in paths
        for line in file_patches.get(p, b"").split(b"\n")
        if line.startswith(sign) and not line.startswith(sign * 3)
    )
    return {ln for ln in lines if ln}


def _only_bookkeeping(old_patch, new_patch, new, cwd=None, new_base=None):
    """(True, None) when the two branch patches differ only in release
    bookkeeping, else (False, the first path that differs for real).

    Note files may renumber, move text between UPDATE.md and CHANGELOG.md and
    pick up upstream notes (lines already on the new merge-base), but every
    reviewed line must still be added, and a line may only be removed if the
    reviewed patch removed it too or it reappears elsewhere in the notes (a
    move), so a restack can't quietly drop another branch's release notes."""
    old_files, new_files = _split_by_file(old_patch), _split_by_file(new_patch)
    if old_files is None or new_files is None:
        return False, "(the diff could not be parsed)"
    notes = []
    for path in sorted(set(old_files) | set(new_files)):
        o, n = old_files.get(path, b""), new_files.get(path, b"")
        if o == n:
            continue
        if BOOKKEEPING_NOTE_FILES.match(path):
            notes.append(path)
            continue
        allowed = (
            _VERSION_LINE
            if BOOKKEEPING_VERSION_FILES.match(path)
            else _STAMP_LINE
            if BOOKKEEPING_STAMP_FILES.match(path)
            else None
        )
        if allowed is None or _changes(o, allowed) != _changes(n, allowed):
            return False, path
    if not notes:
        return True, None
    reviewed, current = _note_lines(old_files, notes), _note_lines(new_files, notes)
    dropped = _note_lines(new_files, notes, b"-") - _note_lines(old_files, notes, b"-")
    base = new_base or merge_base(new, cwd)
    upstream = set()
    for path in notes:
        text = _run(["show", f"{base}:{path}"], cwd=cwd) if base else None
        upstream |= {_SEMVER.sub(b"X", ln).strip() for ln in (text or b"").split(b"\n")}
    ok = reviewed <= current and current - reviewed <= upstream and dropped <= current
    return (True, None) if ok else (False, sorted(notes)[0])


def is_ancestor(old, new, cwd=None):
    """True, False, or None when git failed (exit 1 means "no"; anything else
    is an error, not an answer)."""
    rc, _, _ = _git(["merge-base", "--is-ancestor", old, new], cwd=cwd)
    return {0: True, 1: False}.get(rc)


def reviewed_base(old, stored=None, ref=None, cwd=None):
    """The base commit `old` was reviewed on, or None.

    The cycle's stored base wins when it is in `old`'s history, unless the
    fork point below is newer (the branch was rebased between arming and
    the chain). A cycle that has none (armed before 4.17) falls back to
    where `old` forked from `ref` (the base it is restacked onto) by that
    ref's reflog, as `git rebase` does, then to their plain merge-base.
    Without this, the reviewed patch is taken against the default branch
    and a lower layer that moved between review and restack reads as the
    branch's own change."""
    fork = None
    if ref:
        for args in (["--fork-point", ref, old], [ref, old]):
            out = _run(["merge-base", *args], cwd=cwd)
            if out:
                fork = out.decode(errors="replace").strip()
                break
    if stored and is_ancestor(stored, old, cwd) is True:
        # A stored base taken before a pre-chain rebase sits below what the
        # gates were earned on; the newer fork point is the reviewed base.
        if fork and fork != stored and is_ancestor(stored, fork, cwd) is True:
            return fork
        return stored
    return fork


def _numstat(old, new, cwd=None):
    """(added, deleted, binary files changed) from `old` to `new`, or None
    when git fails. A binary file has no line count, so it is counted apart:
    a line cap must never read it as zero."""
    out = _run(
        ["diff", "--numstat", "--no-renames", "--no-ext-diff", old, new], cwd=cwd
    )
    if out is None:
        return None
    added = deleted = binary = 0
    for line in out.decode(errors="replace").splitlines():
        a, d, _ = line.split("\t", 2)
        if not (a.isdigit() and d.isdigit()):
            binary += 1
            continue
        added += int(a)
        deleted += int(d)
    return added, deleted, binary


def _clean_replay(old, new, cwd=None, old_base=None, new_base=None):
    """True when HEAD is exactly what git produces by merging the reviewed
    commit onto HEAD's base with no conflicts and no hand edits; False when
    it is not; None when this git can't tell (`merge-tree --write-tree`
    needs git 2.38, and 2.40 when the reviewed base is pinned with
    `--merge-base`). This is what a clean rebase is, including a restack
    after a parent branch squash-merged: the parent's changes are on both
    sides and merge away. It is the same textual merge the PR's own merge
    would make."""
    base = new_base or merge_base(new, cwd)
    if not base:
        return None
    # With the reviewed base known, replay only the branch's own commits
    # (old_base..old) onto the new base, as `git rebase --onto` does: on a
    # stack, the layer below may have been rewritten or squash-merged.
    pinned = [f"--merge-base={old_base}"] if old_base else []
    rc, out, _ = _git(["merge-tree", "--write-tree", *pinned, base, old], cwd=cwd)
    if rc == 1:
        return False
    if rc != 0:
        return None
    tree = _run(["rev-parse", f"{new}^{{tree}}"], cwd=cwd)
    if tree is None:
        return None
    return out.split(b"\n", 1)[0].strip() == tree.strip()


def _whitespace_only(old, new, cwd=None):
    """True when `old` -> `new` changes only trailing spaces/tabs and blank
    lines: no file added, removed, renamed or re-moded, no binary, no CR
    (an LF -> CRLF change breaks a shebang), no indentation."""
    stat = _numstat(old, new, cwd)
    if stat is None or stat[2]:
        return False
    out = _run(["diff", *_DIFF_FLAGS, "-U0", "--ignore-blank-lines", old, new], cwd=cwd)
    if out is None:
        return False
    minus, plus = [], []
    for line in out.split(b"\n"):
        if line.startswith((b"new file", b"deleted file", b"old mode", b"new mode")):
            return False
        if line.startswith((b"+++", b"---")):
            continue
        side = {b"-": minus, b"+": plus}.get(line[:1])
        text = line[1:].rstrip(b" \t")
        if side is not None and text:  # blank lines may come and go
            side.append(text)
    return minus == plus


def classify_carry(
    old, new, reason=None, cwd=None, spent=0, old_base=None, new_base=None
):
    """(class, None, lines) when the commits from `old` to `new` may carry the
    skill gates, else (None, why-not, None). `lines` is what a manual carry
    spends of the cap, None for every other class. `reason` asks for the
    manual class, used only when no mechanical class applies. `spent` is the
    lines earlier manual carries in this cycle already used: the cap covers
    them all, so manual hops can't chain past it without a review.
    `old_base`/`new_base` are the round's base commit when the gates were
    earned and now (gate_base); the branch's own patch is measured from them,
    never from below them."""
    if not old or not new:
        return None, "no evidence head to carry from", None
    if old == new:
        return (
            None,
            "nothing to carry: the last review gate is already on HEAD",
            None,
        )
    ancestor = is_ancestor(old, new, cwd)
    if ancestor is None:
        return (
            None,
            _git_failure(f"tell whether {old[:12]} is in HEAD's history"),
            None,
        )
    sides = ((old_base, old, "--reviewed-base"), (new_base, new, "--set-base"))
    for base, head, flag in sides:
        # A base at or past the head it measures (`--set-base HEAD`, the
        # branch itself) makes the branch's patch empty on both sides, which
        # would compare equal over any change at all.
        if not base:
            continue
        past = base == head or is_ancestor(head, base, cwd)
        if past is None:
            why = _git_failure(f"tell whether {base[:12]} is below {head[:12]}")
            return None, why, None
        if past:
            return (
                None,
                (
                    f"the review base {base[:12]} is not below {head[:12]}, so "
                    "the branch's own patch can't be measured; point "
                    f"{flag} at the parent branch"
                ),
                None,
            )
    bases = (old_base, new_base)
    if not ancestor:
        return _classify_rewrite(old, new, reason, cwd, spent, bases)
    return _classify_descendant(old, new, reason, cwd, spent, bases)


def _classify_rewrite(old, new, reason=None, cwd=None, spent=0, bases=(None, None)):
    """classify_carry for a `new` that does not have `old` in its history."""
    old_base, new_base = bases
    if _clean_replay(old, new, cwd, old_base, new_base):
        return "rebase", None, None
    old_patch = _branch_patch(old, cwd, old_base)
    new_patch = _branch_patch(new, cwd, new_base)
    if old_patch is None or new_patch is None:
        return None, _git_failure("compute the branch's patch"), None
    if old_patch == new_patch:
        return "rebase", None, None
    ok, path = _only_bookkeeping(old_patch, new_patch, new, cwd, new_base)
    if ok:
        return "bookkeeping", None, None
    if reason is None:
        return (
            None,
            (
                f"{old[:12]} is not an ancestor of HEAD and the branch's patch "
                f"changed beyond release bookkeeping, first in {path} (a rebase "
                "that resolved real conflicts, or a rewrite); a mechanical "
                "conflict resolution can carry with --carry --reason "
                '"<what the resolution did>"'
            ),
            None,
        )
    why, lines = _manual_rewrite_refusal(old_patch, new_patch, reason, spent)
    # ponytail: recorded as `manual`, like a comment fix; give a restack carry its own class once anything reads the class to tell them apart
    return (None, why, None) if why else ("manual", None, lines)


def _classify_descendant(old, new, reason, cwd=None, spent=0, bases=(None, None)):
    """classify_carry for a `new` that has `old` in its history."""
    if _whitespace_only(old, new, cwd):
        return "format", None, None
    # --no-renames: a rename prints both paths, so moving a command file into
    # docs/ is judged on the command file it removes, not only where it lands.
    names = _run(["diff", "--name-only", "--no-renames", old, new], cwd=cwd)
    if names is None:
        return None, _git_failure("list the changed files"), None
    paths = [p for p in names.decode(errors="replace").splitlines() if p]
    if paths and all(is_docs_safe(p) for p in paths):
        return "docs", None, None
    old_base, new_base = bases
    old_patch = _branch_patch(old, cwd, old_base)
    new_patch = _branch_patch(new, cwd, new_base)
    if old_patch is None or new_patch is None:
        return None, _git_failure("compute the branch's patch"), None
    if old_patch == new_patch:
        # A clean merge of the default branch: the branch's own patch is
        # untouched, exactly as after a clean rebase.
        return "rebase", None, None
    if _only_bookkeeping(old_patch, new_patch, new, cwd, new_base)[0]:
        return "bookkeeping", None, None
    if reason is None:
        unsafe = [p for p in paths if not is_docs_safe(p)]
        return (
            None,
            (
                "the change touches code or agent-executed files "
                f"({', '.join(unsafe[:3])}{', ...' if len(unsafe) > 3 else ''}); "
                "a comment, docstring or wording fix can carry with "
                '--carry --reason "<why>"'
            ),
            None,
        )
    why, lines = _manual_refusal(old, new, reason, cwd, spent)
    return (None, why, None) if why else ("manual", None, lines)


# Refusals both manual paths share.
_NO_REASON = "a manual carry needs a non-empty --reason"
_ADDS_FILES = "a manual carry cannot add files (a rename counts as one)"
_CHANGES_BINARY = "a manual carry cannot change binary files"


def _over_cap(lines, spent):
    """The cap refusal for a manual carry of `lines`, or None."""
    if lines + spent <= MANUAL_CARRY_MAX_LINES:
        return None
    earlier = f" plus {spent} from earlier manual carries" if spent else ""
    return (
        f"manual carries are capped at {MANUAL_CARRY_MAX_LINES} changed "
        f"lines (added plus deleted) per cycle; this change has {lines}"
        f"{earlier}. Reset and re-run the chain"
    )


def _manual_refusal(old, new, reason, cwd=None, spent=0):
    """(why a manual carry over commits on top is refused, None), or
    (None, the lines it spends) when it is allowed."""
    if not reason.strip():
        return _NO_REASON, None
    added = _run(
        ["diff", "--name-only", "--no-renames", "--diff-filter=A", old, new], cwd=cwd
    )
    stat = _numstat(old, new, cwd)
    if added is None or stat is None:
        return _git_failure("measure the change"), None
    if added.strip():
        return _ADDS_FILES, None
    if stat[2]:
        return _CHANGES_BINARY, None
    lines = stat[0] + stat[1]
    return _over_cap(lines, spent), lines


def _new_files(file_patches):
    return {p for p, text in file_patches.items() if b"\nnew file mode " in text}


def _binary_files(file_patches):
    return {p for p, text in file_patches.items() if b"\nGIT binary patch" in text}


def _manual_rewrite_refusal(old_patch, new_patch, reason, spent=0):
    """(why a manual carry over a rewrite is refused, None), or (None, the
    lines it spends) when it is allowed (sharpen#65).

    The two heads don't share history, so the diff between them is every
    change the layers below made too. What the reason vouches for is the
    difference between the reviewed and the current branch patch: the lines
    a conflict resolution changed in the branch's own +/- lines. Those are
    counted with a line diff, against the same cap as every manual carry."""
    if not reason.strip():
        return _NO_REASON, None
    old_files, new_files = _split_by_file(old_patch), _split_by_file(new_patch)
    if old_files is None or new_files is None:
        return "the branch's patch could not be parsed", None
    if _new_files(new_files) - _new_files(old_files):
        return _ADDS_FILES, None
    binary = _binary_files(old_files) | _binary_files(new_files)
    if any(old_files.get(p) != new_files.get(p) for p in binary):
        return _CHANGES_BINARY, None
    # ponytail: one conflicting line spends 4 (the lower layer's - line counts too); count only + lines if a mechanical restack of 7 or fewer conflicting lines is refused over the cap
    # ponytail: diffs the whole patch, not only the files whose sections differ; diff per file if a rewrite --carry takes over a second
    lines = 0
    old_lines, new_lines = old_patch.split(b"\n"), new_patch.split(b"\n")
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            lines += (i2 - i1) + (j2 - j1)
    return _over_cap(lines, spent), lines


def manual_lines_spent(branch_data):
    """Lines the cycle's manual carries already used."""
    return sum(
        c.get("lines") or 0 for c in carries(branch_data) if c.get("class") == "manual"
    )


def carried_heads(branch_data, head):
    """`head`, then every commit recorded carries reach from it, in order."""
    hops = {c.get("from"): c.get("to") for c in carries(branch_data)}
    chain = [head]
    while chain[-1] in hops and hops[chain[-1]] not in chain:
        chain.append(hops[chain[-1]])
    return chain


def carries(branch_data):
    value = (branch_data or {}).get(CARRIES_KEY)
    return [c for c in value if isinstance(c, dict)] if isinstance(value, list) else []


def apply_carry(data, branch, old, new, klass, reason=None, lines=None):
    """Store mutator: record the hop and clear tests/lint/typecheck. Returns
    the gates it cleared."""
    bd = data.get(branch)
    if not isinstance(bd, dict):
        raise ValueError(f'No gate cycle for branch "{branch}"')
    stamp = gs._now()
    if not isinstance(bd.get(CARRIES_KEY), list):
        bd[CARRIES_KEY] = []
    hop = {
        "from": old,
        "to": new,
        "class": klass,
        "reason": reason.strip() if reason else None,
        "at": stamp,
    }
    if lines is not None:
        hop["lines"] = lines
    bd[CARRIES_KEY].append(hop)
    cleared = []
    for g in RERUN_AFTER_CARRY:
        if bd.get("gates", {}).pop(g, None) is not None:
            bd.setdefault("invalidated_by", {})[g] = {
                "gate": f"carry ({klass})",
                "at": stamp,
            }
            cleared.append(g)
    return cleared


def format_carries(branch_data):
    """Status lines for each carry, or [] when there are none."""
    lines = []
    for c in carries(branch_data):
        why = f": {c['reason']}" if c.get("reason") else ""
        lines.append(
            f"Carried ({c.get('class')}): {str(c.get('from'))[:12]} -> "
            f"{str(c.get('to'))[:12]}{why}"
        )
    return lines


# --- rounds -----------------------------------------------------------------


def added_lines(head, cwd=None, base=None):
    """Lines the branch adds over its merge-base at `head`, or None. Pass
    `base` when the caller already has it, to skip the merge-base walk."""
    base = base or (merge_base(head, cwd) if head else None)
    stat = _numstat(base, head, cwd) if base else None
    return stat[0] if stat else None


def rounds(branch_data):
    value = (branch_data or {}).get(ROUNDS_KEY)
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


def next_rounds(previous, head, added):
    """The round history the new cycle should carry. A cycle that earned any
    skill gate counts as a finished round; one that earned none (a re-init
    before any pass) is the same round started again."""
    if not isinstance(previous, dict):
        previous = None  # a hand-edited entry: start over, don't crash --init
    history = rounds(previous)
    gates = (previous or {}).get("gates")
    earned = isinstance(gates, dict) and any(g in gs.SKILL_FOR_GATE for g in gates)
    entry = {"head": head, "added": added, "at": gs._now()}
    if not history and earned:
        # A cycle from before the counter (or armed without one) that already
        # earned skill gates was round 1, even though nobody wrote it down.
        history = [{"head": None, "added": None, "at": previous.get("created_at")}]
    if not history:
        return [entry]
    if earned:
        return history + [entry]
    return history[:-1] + [entry]


def round_summary(branch_data):
    """One advisory line about the round count, or None in round 1."""
    history = rounds(branch_data)
    if len(history) < 2:
        return None
    first, last = history[0].get("added"), history[-1].get("added")
    growth = (
        f" The branch grew from {first} to {last} added lines since round 1."
        if isinstance(first, int) and isinstance(last, int)
        else ""
    )
    advice = (
        " Consider shipping what is here and filing the rest as issues, or "
        "splitting the branch."
        if len(history) >= ROUND_ADVICE_FROM
        else ""
    )
    return f"Round {len(history)} of the review chain.{growth}{advice}"
