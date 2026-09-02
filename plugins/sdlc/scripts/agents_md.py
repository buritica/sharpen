#!/usr/bin/env python3
"""
Write the SDLC contract into a repo's AGENTS.md, and make CLAUDE.md include it.

`/sdlc:init` derives a repo's toolchain, tier rules and PR conventions, then
(step 10) has to leave them somewhere every agent host will read. That place is
`AGENTS.md` — Codex, Gemini, Cursor and Copilot read it directly, and Claude
Code reads it through a one-line `CLAUDE.md` containing `@AGENTS.md`. This
module renders `templates/agents-sdlc.md` with the derived facts and upserts it
between `<!-- sdlc:begin -->` / `<!-- sdlc:end -->` markers, so re-running init
replaces exactly its own block and nothing a human wrote. The tier names and
the gate → skill chain come from `gate_store.py` (the enforcer), so the block
cannot describe a tier the store rejects.

It also migrates the pre-4.11 `## Run gates before every PR` reminder that
init used to append to CLAUDE.md: the section is removed (AGENTS.md now
carries it) only when its heading matches exactly and its body mentions
`/sdlc:gate`. Any other `/sdlc:gate` mention left in CLAUDE.md is reported by
line number, never removed.

Both files are refused when either is a symlink: with `CLAUDE.md -> AGENTS.md`
the second write would follow the link and erase the block the first one
wrote. Line endings are preserved per file; writes go through a temp file and
rename, and a failure names what was already written.

Usage:
  agents_md.py --root DIR [--test-cmd C] [--lint-cmd C] [--format-cmd C]
               [--typecheck-cmd C] [--default-branch main] [--grumpy]
               [--deploy TEXT] [--check]

Prints one `path | action` line per file. Exit 0; with `--check`, exit 1 when
anything would change (nothing is written); exit 2 on bad arguments, an
unreadable file, or a refused layout. Pure stdlib.
"""

import argparse
import os
import re
import sys

# Same-directory sibling (the plugin cache is version-nested, so this is the
# only kind of cross-file import a plugin script may make).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import gate_store as gs
except ImportError:  # vendored without its sibling
    gs = None

BEGIN = "<!-- sdlc:begin -->"
END = "<!-- sdlc:end -->"
INCLUDE = "@AGENTS.md"
OLD_HEADING = "## Run gates before every PR"
TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates",
    "agents-sdlc.md",
)
NOT_CONFIGURED = "not configured — wire one and re-run `/sdlc:init`"
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")

# One-line judgment per tier; the names themselves come from gate_store.TIERS.
TIER_NOTES = {
    "tiny": "≤3 lines, no behavior change",
    "small-medium": "any code change — the default",
    "significant": "new behavior, new integration, >3 files or >200 lines",
}

GRUMPY_OFF = (
    "`grumpy` is not installed: small-medium and significant cycles cannot complete "
    "until it is. Install it, or use `tiny` only when the change genuinely qualifies."
)


class WireError(Exception):
    """Malformed markers, a refused layout, or an unusable root — exit 2."""


def _store():
    if gs is None:
        raise WireError(
            "gate_store.py is missing beside this script; it supplies the tier names "
            "and gate chain, so the block cannot be rendered without it"
        )
    return gs


def tiers_line():
    return ", ".join(
        "**%s** (%s)" % (tier, TIER_NOTES.get(tier, "see `/sdlc:gate`"))
        for tier in _store().TIERS
    )


def skill_gates():
    """[(gate number, skill)] for the skill-gated gates of a full cycle, in order."""
    store = _store()
    gates = store.GATES_BY_TIER["small-medium"]
    return [
        (i, store.SKILL_FOR_GATE[g])
        for i, g in enumerate(gates, 1)
        if g in store.SKILL_FOR_GATE
    ]


def skill_chain():
    return [skill for _, skill in skill_gates()]


def grumpy_on_line():
    pairs = skill_gates()
    if not pairs:
        return "Every gate is bash-verifiable; nothing is skill-gated."
    chain = ["`%s`" % s for _, s in pairs]
    numbers = [n for n, _ in pairs]
    return (
        "Gates %d–%d are %s, then %s. Run the skills; reviewing the diff in your head "
        "records nothing." % (numbers[0], numbers[-1], chain[0], " → ".join(chain[1:]))
    )


def _cmd(value):
    value = (value or "").strip()
    return "`%s`" % value if value else NOT_CONFIGURED


def fill(template, values):
    """Replace `{name}` placeholders only. Literal braces elsewhere (a JSON
    example, `{}`) pass through; an unknown `{name}` is an error, since it is
    almost certainly a typo in the template."""

    def sub(match):
        key = match.group(1)
        if key not in values:
            raise WireError("template placeholder {%s} has no value" % key)
        return values[key]

    return PLACEHOLDER.sub(sub, template)


def render(facts, template_path=TEMPLATE):
    """The managed block, markers included."""
    with open(template_path, encoding="utf-8") as fh:
        template = fh.read()
    deploy = (facts.get("deploy") or "").strip()
    body = fill(
        template,
        {
            "default_branch": facts.get("default_branch") or "main",
            "test_cmd": _cmd(facts.get("test_cmd")),
            "lint_cmd": _cmd(facts.get("lint_cmd")),
            "format_cmd": _cmd(facts.get("format_cmd")),
            "typecheck_cmd": _cmd(facts.get("typecheck_cmd")),
            "tiers_line": tiers_line(),
            "grumpy_line": grumpy_on_line() if facts.get("grumpy") else GRUMPY_OFF,
            "deploy_line": ("- deploy: %s\n" % deploy) if deploy else "",
        },
    )
    return "%s\n%s\n%s" % (BEGIN, body.strip("\n"), END)


def upsert_block(text, block):
    """Replace the marked block in `text`, or append it. Bytes outside the
    markers are untouched. One marker without the other is an error — a
    half-marked file would otherwise get a second block."""
    has_begin, has_end = BEGIN in text, END in text
    if has_begin != has_end:
        raise WireError("found one of %s / %s but not the other" % (BEGIN, END))
    if has_begin:
        start = text.index(BEGIN)
        end_at = text.find(END, start)
        if end_at < 0:
            raise WireError("%s appears before %s" % (END, BEGIN))
        return text[:start] + block + text[end_at + len(END) :]
    if not text:
        return block + "\n"
    if not text.endswith("\n"):
        text += "\n"
    return text + "\n" + block + "\n"


def remove_old_section(text):
    """Drop init's pre-4.11 CLAUDE.md reminder. Returns (text, removed). The
    section runs to the next heading, fence-aware: a `# comment` inside the
    reminder's own code fence is not a heading."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip() != OLD_HEADING:
            continue
        j = i + 1
        in_fence = False
        while j < len(lines):
            if lines[j].lstrip().startswith("```"):
                in_fence = not in_fence
            elif not in_fence and lines[j].startswith("#"):
                break
            j += 1
        body = "".join(lines[i:j])
        if "/sdlc:gate" not in body:
            return text, False
        head = "".join(lines[:i]).rstrip("\n")
        tail = "".join(lines[j:]).lstrip("\n")
        if head and tail:
            return head + "\n\n" + tail, True
        return (head + "\n" if head else tail), True
    return text, False


def leftover_gate_mentions(text):
    """1-based line numbers of `/sdlc:gate` mentions that survive migration,
    outside any managed block pasted into the file — an older init may have
    written the reminder under a different heading, and a hand-varied copy
    left beside the include is two contracts loaded at once. Reported, never
    auto-removed."""
    found, in_block = [], False
    for i, line in enumerate(text.splitlines(), 1):
        if BEGIN in line:
            in_block = True
        if not in_block and "/sdlc:gate" in line:
            found.append(i)
        if END in line:
            in_block = False
    return found


def _read(path):
    """(text normalized to LF, crlf flag) or (None, False) when absent. A
    symlink is refused: writing through one can erase the other file."""
    if os.path.islink(path):
        raise WireError(
            "%s is a symlink; refusing to write through it (with CLAUDE.md -> AGENTS.md "
            "the second write would erase the block the first one wrote). Replace the "
            "link with a real file and re-run." % os.path.basename(path)
        )
    if not os.path.exists(path):
        return None, False
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            raw = fh.read()
    except UnicodeDecodeError as exc:
        raise WireError(
            "%s is not UTF-8 (%s); fix its encoding and re-run" % (path, exc)
        )
    crlf = "\r\n" in raw
    return raw.replace("\r\n", "\n"), crlf


def _write(path, text, crlf):
    """Temp-then-rename, so a crash mid-write cannot leave a truncated file."""
    if crlf:
        text = text.replace("\n", "\r\n")
    tmp = path + ".sdlc-tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)


def plan_agents(root, block):
    path = os.path.join(root, "AGENTS.md")
    current, crlf = _read(path)
    if current is None:
        heading = "# %s\n\n" % os.path.basename(os.path.abspath(root))
        return path, None, heading + block + "\n", [], crlf
    return path, current, upsert_block(current, block), [], crlf


def plan_claude(root):
    """Returns (path, current, new, notes, crlf)."""
    path = os.path.join(root, "CLAUDE.md")
    current, crlf = _read(path)
    notes = []
    if current is None:
        return path, None, INCLUDE + "\n", notes, crlf
    new, removed = remove_old_section(current)
    if removed:
        notes.append(
            "removed the old '%s' section (AGENTS.md carries it now)" % OLD_HEADING[3:]
        )
    if not any(line.strip() == INCLUDE for line in new.splitlines()):
        new = INCLUDE + "\n" + ("\n" + new if new.strip() else "")
        notes.append("prepended %s" % INCLUDE)
    leftover = leftover_gate_mentions(new)
    if leftover:
        notes.append(
            "CLAUDE.md still mentions /sdlc:gate at line %s — AGENTS.md carries that now; "
            "move or drop it" % ", ".join(str(n) for n in leftover)
        )
    return path, current, new, notes, crlf


def wire(root, facts, check=False):
    """Apply (or, with check=True, only compute) the AGENTS.md/CLAUDE.md
    changes. Returns [(relative path, action, note)] where action is one of
    created / updated / unchanged. Both plans are computed before either
    file is written; if the second write fails, the error names the first."""
    if not os.path.isdir(root):
        raise WireError("not a directory: %s" % root)
    block = render(facts)
    plans = [plan_agents(root, block), plan_claude(root)]
    results, written = [], []
    for path, cur, new, notes, crlf in plans:
        if cur is None:
            action = "created"
        elif cur == new:
            action = "unchanged"
        else:
            action = "updated"
        rel = os.path.relpath(path, root)
        if action != "unchanged" and not check:
            try:
                _write(path, new, crlf)
            except OSError as exc:
                already = (
                    (" (already written: %s)" % ", ".join(written)) if written else ""
                )
                raise WireError("could not write %s: %s%s" % (rel, exc, already))
            written.append(rel)
        results.append((rel, action, "; ".join(notes)))
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="agents_md.py",
        description="Upsert the SDLC contract into AGENTS.md and include it from CLAUDE.md.",
    )
    parser.add_argument("--root", default=".", help="repo root (default: cwd)")
    parser.add_argument("--test-cmd")
    parser.add_argument("--lint-cmd")
    parser.add_argument("--format-cmd")
    parser.add_argument("--typecheck-cmd")
    parser.add_argument("--default-branch", default="main")
    parser.add_argument(
        "--grumpy", action="store_true", help="grumpy plugin is installed"
    )
    parser.add_argument("--deploy", help="one line on how the repo deploys, if it does")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what would change without writing; exit 1 if anything would",
    )
    args = parser.parse_args(argv)
    facts = {
        "test_cmd": args.test_cmd,
        "lint_cmd": args.lint_cmd,
        "format_cmd": args.format_cmd,
        "typecheck_cmd": args.typecheck_cmd,
        "default_branch": args.default_branch,
        "grumpy": args.grumpy,
        "deploy": args.deploy,
    }
    try:
        results = wire(args.root, facts, check=args.check)
    except (WireError, OSError) as exc:
        print("agents_md: %s" % exc, file=sys.stderr)
        return 2
    drift = False
    for path, action, note in results:
        line = "%s | %s" % (path, action)
        if note:
            line += " | " + note
        if args.check and action != "unchanged":
            line += " (would change)"
            drift = True
        print(line)
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
