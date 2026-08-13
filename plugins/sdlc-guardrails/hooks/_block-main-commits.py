#!/usr/bin/env python3
"""
PreToolUse hook logic: deny git commit on main/master and git push to
main/master -- but only for repos that opt in to protection.

Protection is opt-in per repo (see _guardrails_config.py). By default no
repo is protected, so the hook is silent everywhere until you run
`/guard on` (or `python3 _guardrails_config.py protect`) in a repo, or flip
`protectMainDefault` on. Set SDLC_ALLOW_MAIN=1 to bypass for one command.

Branch detection is target-aware. If the agent's command uses `git -C <path>`
or is prefixed with `cd <path> && ...`, the branch is checked in that target
directory rather than the hook process's cwd. This lets a session anchored on
main of repo A legitimately drive commits in a feature worktree of repo B.

SCOPE: this is a speed bump, not a security boundary. Detection is best-effort
shell parsing and fails OPEN by design — anything it cannot parse is allowed
rather than blocked, because a guard that blocks legitimate work on every
commit is worse than one with gaps. Known gaps are listed beside the parser
below and pinned in tests/test_block_main.py. It stops an agent (or a human)
from committing to main by habit; it does not stop one that is trying to.
"""

import json
import os
import re
import shlex
import subprocess
import sys

from _guardrails_config import bypass_env, is_protected, load, repo_root

_ESCAPE = (
    "If committing on {branch} is actually intended here, prepend "
    "SDLC_ALLOW_MAIN=1 to this one command, or run `/guard off` to stop "
    "protecting this repo."
)

# Detection runs TWO passes and denies if either fires: this textual one, and
# the argv-level one below. Splitting on hard separators (;|&) — but NOT on \n,
# since only the first command of a multi-line script reaches head position
# either way. Within each segment, git must be the first token (after optional
# env-var assignments), so "echo 'git commit'" is not a match.
#
# This pass is the FLOOR, and it is load-bearing for exactly one case: a
# command that fails to lex. `git commit -m x && echo can't` has an unbalanced
# quote, the argv pass returns nothing, and only this regex still sees the
# commit. Deleting it passes the whole suite and silently reopens that hole.
#
# The sdlc plugin's shell_parse.py answers the same question, and this hook
# still cannot import it: plugins are separate, cross-plugin
# ${CLAUDE_PLUGIN_ROOT}/../ paths don't resolve (the cache is version-nested),
# and guardrails must keep working with sdlc uninstalled. The follow-up that
# comment called for is the argv pass below — hand-rolled rather than vendored,
# because this hook needs two behaviours shell_parse deliberately does not
# have: it fails CLOSED on pathological nesting, and it parses the escape
# hatch as a token. shell_parse is a lexer for gates that defer to CI; a miss
# there costs a CI round-trip, a miss here lands a commit on a protected
# branch. The two are expected to drift; that is the trade.
_SHELL_SEP = re.compile(r"[;|&]")


# --- argv-level detection ---------------------------------------------------
# The regex above only sees `git` when it sits at the *textual* start of a
# [;|&]-delimited segment, so every form that hides git behind another word or
# a paren slips through: `bash -c "git commit"`, `sh -c '...'`, `eval '...'`,
# `(git commit)`, `env git commit`. Widening that regex is not the fix: it
# would have to split on parens, which turns prose like `echo "run (git
# commit) later"` into a match, and a false positive here blocks every
# legitimate commit.
#
# Instead, tokenize like a shell does. shlex in posix mode collapses a quoted
# string into a single token, so `echo "run (git commit) later"` yields
# ['echo', 'run (git commit) later'] — the mention can never be a command.
# That protection covers QUOTED text only, which is why heredoc bodies (which
# are unquoted at the lexer level) must be stripped separately below.
#
# punctuation_chars splits `();<>|&` into their own tokens, so a subshell's
# `(` becomes a real segment boundary. Wrappers that take a command are then
# unwrapped and re-parsed.
#
# The union of the two passes can only ever ADD denies, never remove them —
# so the argv pass's risk profile is false positives, not false negatives.
#
# Heredoc bodies are stripped BEFORE tokenizing. This is the root fix for the
# false positive that quoting alone does not prevent: a heredoc body is not
# quoted at the lexer level, so `gh pr create --body-file - <<'EOF' ... you
# (git commit) later ... EOF` used to tokenize its prose as commands, and the
# `(` started a segment whose head was `git`. Stripping the body means prose
# in PR/issue bodies can never be read as a command, whatever punctuation it
# contains.
#
# Deliberately NOT handled (known gaps, non-exhaustive — see README):
#   - Newline-separated commands: newlines are not token separators for shlex,
#     so only the first command of a multi-line script occupies head position.
#     Splitting on them would re-read quoted multi-line arguments as commands.
#   - Command substitution inside double quotes: `echo "$(git commit)"` keeps
#     `$(git commit)` inside one token. Reaching into quoted tokens is exactly
#     what would resurrect the prose false positive.
#   - `sudo`/`xargs`-style wrappers not in _ARGV_WRAPPERS below.
_PUNCT_CHARS = set("();<>|&")

# `<<WORD`, `<<'WORD'`, `<<-"WORD"` — the body runs to a line equal to WORD.
_HEREDOC_START = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")

# Shells whose `-c <script>` argument is another command to parse.
_WRAPPER_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}

# Commands that run their remaining arguments as a command, transparently.
_ARGV_WRAPPERS = {
    "env",
    "command",
    "exec",
    "nohup",
    "time",
    "sudo",
    "doas",
    "timeout",
    "stdbuf",
    "nice",
    "ionice",
    "setsid",
    "script",
    "xargs",
}

# Wrapper options that consume the following token as an opaque value
# (`env -u NAME`, `xargs -I{}`, `nice -n 5`).
# `-S`/`--split-string` is NOT here: its argument is a command, so it is
# handled with the shell `-c` case below.
_WRAPPER_OPTS_WITH_VALUE = {"-u", "--unset", "-C", "--chdir", "-n", "-I", "-P", "-L"}

# Leading shell syntax that precedes a command without being one.
_LEADING_NOISE = {"{", "}", "!", "then", "do", "else", "elif", "in"}

# git global options that consume a following value. `--exec-path` is
# deliberately absent: its value is optional (`git --exec-path commit` is a
# real commit), so treating it as value-consuming swallowed the subcommand
# and allowed the commit.
_GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}

_ASSIGNMENT = re.compile(r"^[A-Za-z_]\w*=")
# bash accepts bundled flags in any order: -c, -lc, -cx, -exc all take a script.
_SHELL_C_FLAG = re.compile(r"^-[a-zA-Z]*c[a-zA-Z]*$")
_SPLIT_STRING_FLAG = re.compile(r"^(?:-S|--split-string)$")

# Past this nesting the command is pathological, so the hook DENIES rather
# than falling through to allow. Costs a determined user one SDLC_ALLOW_MAIN=1;
# the alternative was letting five nested `eval`s walk a commit onto main.
_MAX_UNWRAP_DEPTH = 6

# Lexing cost grows superlinearly in single-token length, and this hook runs on
# every Bash call. Past this, skip the argv pass and rely on the regex floor.
_MAX_LEX_CHARS = 100_000


def _strip_heredocs(cmd):
    """Drop heredoc bodies, keeping the line that opens them.

    The body is argument data, never a command. Removing it before lexing is
    what lets prose in a `gh pr create --body-file - <<'EOF'` body contain
    parens, angle brackets or list markers without being read as a command.
    """
    lines = cmd.split("\n")
    kept = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        match = _HEREDOC_START.search(line)
        if not match:
            continue
        delimiter = match.group(2)
        while index < len(lines) and lines[index].strip() != delimiter:
            index += 1
        index += 1  # drop the delimiter line too
    return "\n".join(kept)


def _tokenize(cmd):
    """Shell-like tokens, or None if the command doesn't lex.

    Only ValueError is caught — that is what shlex raises for unbalanced
    quotes (`echo it's fine`) and trailing backslashes. Anything else is a
    real defect and must reach the top-level guard in `main`, not be laundered
    into "no git here, allow". Note the constructor is inside the try: on an
    interpreter without `punctuation_chars` it raises TypeError, which the
    caller treats as "did not lex" and falls back to the regex floor.

    Requires Python 3.8+, where punctuation_chars and whitespace_split were
    fixed to work together. Older interpreters mis-tokenize rather than raise,
    which degrades to the same regex-only floor.
    """
    if not isinstance(cmd, str) or len(cmd) > _MAX_LEX_CHARS:
        return None
    try:
        lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        # Pinned rather than inherited: shlex defaults to `#`, which matches
        # shell comment semantics here, but a security check should not rest
        # on an unpinned default.
        lexer.commenters = "#"
        return list(lexer)
    except (ValueError, TypeError):
        return None


def _candidates(cmd):
    """Command strings to inspect, most faithful first.

    Normally this is just the heredoc-stripped command. If that fails to lex,
    a single stray apostrophe anywhere would otherwise blind the whole argv
    pass — so decompose and inspect the pieces that *do* lex. Decomposition
    happens ONLY on lex failure: doing it unconditionally would re-read the
    continuation lines of quoted multi-line arguments as commands, which is
    the false positive this design exists to avoid.
    """
    stripped = _strip_heredocs(cmd) if isinstance(cmd, str) else cmd
    yield stripped
    if _tokenize(stripped) is not None:
        return
    for line in str(stripped).split("\n"):
        if _tokenize(line) is not None:
            yield line
            continue
        for segment in _SHELL_SEP.split(line):
            yield segment


def _segments(tokens):
    """Split a token list on shell operators into per-command token lists."""
    segment = []
    for token in tokens:
        if token and all(char in _PUNCT_CHARS for char in token):
            if segment:
                yield segment
            segment = []
        else:
            segment.append(token)
    if segment:
        yield segment


def _git_subcommand(tokens):
    """The subcommand of a `git ...` token list, skipping global options."""
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in _GIT_OPTS_WITH_VALUE:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    return None


def _segment_invokes_git(tokens, subcommand, depth):
    """True iff this segment's command resolves to `git <subcommand>`."""
    if depth > _MAX_UNWRAP_DEPTH:
        # Fail CLOSED. Past this depth the command is pathological, and
        # returning False here is indistinguishable from "not a git commit" —
        # which is how nested wrappers walked a commit onto main.
        return True

    tokens = list(tokens)
    while tokens:
        token = tokens[0]

        # Inline `VAR=value` prefixes bind to the command that follows.
        if _ASSIGNMENT.match(token):
            tokens.pop(0)
            continue

        head = os.path.basename(token)

        # `{ git commit ; }`, `if ...; then git commit; fi`, `! git commit` —
        # syntax that precedes a command without being one.
        if head in _LEADING_NOISE:
            tokens.pop(0)
            continue

        # `env -i git commit`, `sudo git commit`, `timeout 60 git commit`:
        # drop the wrapper and its own options, then judge what remains.
        if head in _ARGV_WRAPPERS:
            tokens.pop(0)
            while tokens:
                # `env -S '<script>'` runs its argument as a command.
                if _SPLIT_STRING_FLAG.match(tokens[0]) and len(tokens) > 1:
                    return _invokes_git(tokens[1], subcommand, depth + 1)
                if tokens[0].startswith("--split-string="):
                    return _invokes_git(
                        tokens[0].split("=", 1)[1], subcommand, depth + 1
                    )
                if not (tokens[0].startswith("-") or _ASSIGNMENT.match(tokens[0])):
                    break
                takes_value = tokens[0] in _WRAPPER_OPTS_WITH_VALUE
                tokens.pop(0)
                if takes_value and tokens:
                    tokens.pop(0)
            # `timeout 60 git ...`, `nice 5 git ...`: a bare numeric operand.
            while tokens and tokens[0].replace(".", "", 1).isdigit():
                tokens.pop(0)
            continue

        # `bash -c "<script>"`: the script argument is a command in its own right.
        if head in _WRAPPER_SHELLS:
            for index in range(1, len(tokens) - 1):
                if _SHELL_C_FLAG.match(tokens[index]):
                    return _invokes_git(tokens[index + 1], subcommand, depth + 1)
            return False

        # `eval 'git commit'`: the remaining words are re-parsed as a command.
        if head == "eval":
            return _invokes_git(" ".join(tokens[1:]), subcommand, depth + 1)

        return head == "git" and _git_subcommand(tokens) == subcommand

    return False


def _invokes_git(cmd, subcommand, depth=0):
    """True iff any command in `cmd` (after unwrapping) is `git <subcommand>`.

    This is the argv pass ONLY — recursion must target this function and never
    `_git_invocation`, so the regex floor is applied once to the outer command
    string rather than re-run on every unwrapped fragment.
    """
    for candidate in _candidates(cmd) if depth == 0 else (cmd,):
        tokens = _tokenize(candidate)
        if tokens is None:
            continue
        for segment in _segments(tokens):
            if _segment_invokes_git(segment, subcommand, depth):
                return True
    return False


def _git_invocation(cmd, subcommand):
    """True iff any shell segment invokes `git <subcommand>` as its command.

    Two independent passes: the original textual segment match, plus argv-level
    parsing that sees through shell wrappers. Either one is enough to deny. The
    regex stays as the floor so a command that fails to lex (unbalanced quotes,
    which `_tokenize` reports as None) is still caught exactly as before.
    """
    pattern = re.compile(
        r"^\s*(?:\w+=\S+\s+)*git"
        r"(?:\s+-C\s+\S+|\s+-c\s+\S+|\s+--\S+|\s+-\S+)*"
        r"\s+" + subcommand + r"\b"
    )
    for segment in _SHELL_SEP.split(cmd):
        if pattern.match(segment):
            return True
    return _invokes_git(cmd, subcommand)


_BYPASS_ASSIGN = re.compile(r"^SDLC_ALLOW_MAIN=(1|true|yes|on)$", re.IGNORECASE)


def _segment_has_bypass(tokens, depth=0):
    """True iff a segment carries the bypass as a real assignment prefix."""
    if depth > _MAX_UNWRAP_DEPTH:
        return False
    tokens = list(tokens)
    while tokens:
        token = tokens[0]
        if _BYPASS_ASSIGN.match(token):
            return True
        if _ASSIGNMENT.match(token) or os.path.basename(token) in _LEADING_NOISE:
            tokens.pop(0)
            continue
        head = os.path.basename(token)
        if head in _ARGV_WRAPPERS:
            tokens.pop(0)
            continue
        if head in _WRAPPER_SHELLS:
            for index in range(1, len(tokens) - 1):
                if _SHELL_C_FLAG.match(tokens[index]):
                    return _has_bypass(tokens[index + 1], depth + 1)
            return False
        if head == "eval":
            return _has_bypass(" ".join(tokens[1:]), depth + 1)
        return False
    return False


def _has_bypass(cmd, depth=0):
    tokens = _tokenize(cmd)
    if tokens is None:
        return False
    return any(_segment_has_bypass(seg, depth) for seg in _segments(tokens))


def _bypass_in_command(cmd):
    """Detect an inline `SDLC_ALLOW_MAIN=<truthy>` env-var prefix.

    Inline env vars propagate to the child process but NOT to the hook, which
    runs before the tool executes — so the hook must parse them out of the
    command string itself for the escape hatch to work.

    This is token-based, not textual. The old regex matched the variable name
    anywhere whitespace (or, briefly, a quote) preceded it, so a commit
    *message* mentioning the escape hatch — `git commit -m
    "SDLC_ALLOW_MAIN=1 is the way out"` — disarmed the guard. Requiring an
    actual assignment token in command-prefix position closes that, while
    still honoring the prefix inside `bash -c "..."` and `eval`.
    """
    if bypass_env():
        return True
    return _has_bypass(_strip_heredocs(cmd) if isinstance(cmd, str) else cmd)


def deny(reason):
    sys.stderr.write(json.dumps({"decision": "deny", "reason": reason}))
    sys.exit(2)


def resolve_target(cmd):
    """Directory the git command will operate on (`-C`, then last `cd`, then cwd)."""
    m = re.search(r"\bgit\s+-C\s+(\"[^\"]+\"|'[^']+'|\S+)", cmd)
    if m:
        return os.path.expanduser(m.group(1).strip("\"'"))
    pre = re.split(r"\bgit\s+(?:commit|push)\b", cmd, maxsplit=1)[0]
    cd_matches = re.findall(
        r"(?:^|[\s;|&])cd\s+(\"[^\"]+\"|'[^']+'|\S+)",
        pre,
    )
    if cd_matches:
        return os.path.expanduser(cd_matches[-1].strip("\"'"))
    return os.getcwd()


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    if data.get("tool_name") != "Bash":
        sys.exit(0)

    tool_input = data.get("tool_input") or {}
    cmd = tool_input.get("command") if isinstance(tool_input, dict) else ""
    if not isinstance(cmd, str):
        cmd = ""
    is_push = _git_invocation(cmd, "push")
    is_commit = _git_invocation(cmd, "commit")
    if not (is_push or is_commit):
        sys.exit(0)

    # Escape hatch: explicit one-shot opt-out — env or inline.
    if bypass_env() or _bypass_in_command(cmd):
        sys.exit(0)

    target = resolve_target(cmd)
    cfg = load()

    # Opt-in: stay silent unless this repo is protected.
    if not is_protected(repo_root(target), cfg):
        sys.exit(0)

    # Block push whose destination ref is main or master. Matches:
    #   git push origin main
    #   git push -u origin main
    #   git push origin HEAD:main
    #   git push origin feature/foo:main
    # Allows refspecs that merely contain "main" in their name
    # (feature/main-rework, main-rework, etc.).
    if is_push and re.search(
        r"\bgit\s+(?:-C\s+\S+\s+)?push\b[^;|&\n]*?\s(?:\S+:)?(?:main|master)\b(?!-)",
        cmd,
    ):
        deny(
            "Don't push directly to main/master. Instead: push the current "
            "feature branch (`git push -u origin HEAD`) and open a PR with "
            "`gh pr create`, then squash-merge it. "
            + _ESCAPE.format(branch="main/master")
        )

    if is_commit:
        try:
            branch = (
                subprocess.check_output(
                    ["git", "-C", target, "rev-parse", "--abbrev-ref", "HEAD"],
                    stderr=subprocess.DEVNULL,
                )
                .decode()
                .strip()
            )
        except Exception:
            branch = "unknown"

        if branch in ("main", "master"):
            deny(
                "You're on {branch} in {target} — move this work onto a "
                "feature branch before committing, then re-run the commit. "
                "Pick one:\n"
                "  - Carry the current changes onto a new branch (simplest): "
                "git -C {target} switch -c feat/<name>\n"
                "  - Or isolate them in a worktree: "
                "git -C {target} worktree add -b feat/<name> "
                ".claude/worktrees/<name> origin/main, then commit there.\n"
                "Choose a short, descriptive <name> from what you're "
                "changing. ".format(branch=branch, target=target)
                + _ESCAPE.format(branch=branch)
            )

    sys.exit(0)


if __name__ == "__main__":
    # A traceback exits 1, which the harness reads as *allow* — a crash would
    # silently disable the guard. `deny()` raises SystemExit(2) and must pass
    # through untouched; everything else degrades to allowing the command
    # rather than spraying a traceback on every Bash call.
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
