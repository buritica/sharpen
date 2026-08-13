#!/usr/bin/env python3
"""
Shared shell-command parsing for the SDLC hooks.

The hooks receive one opaque string (the Bash tool's `command`) and must answer
two questions about it: *does this actually invoke <command>?* and *which
directory does that invocation run in?* Regexes over the raw string get both
wrong in both directions — they miss `bash -c "gh pr create"` and they fire on
`echo "gh pr create"`. This module answers them by tokenizing the string into
argv-shaped command segments, so the answers are about commands rather than
substrings.

Public API:

    command_segments(command) -> [[str, ...], ...]
        Every command in the string, as a normalized argv list. Env-var
        prefixes, wrapper programs (`env`, `nohup`, …) and interpreter prefixes
        (`python3 foo.py`) are stripped; `bash -c "..."` and `eval "..."` are
        recursed into, in place, so nested commands keep their position in the
        sequence.

    invokes(command, name) -> bool
        True when some segment invokes `name`, where `name` is a
        space-separated command chain such as "gh pr create", "git commit" or
        "record-gate.py". Leading option flags are skipped (`git -C dir commit`
        matches "git commit"); a non-matching positional ends the match, so
        `git log --grep commit` does NOT match "git commit".

    matching_segments(command, name) -> [[str, ...], ...]
        argv of each segment `invokes` matched — for callers that also need the
        invocation's own flags.

    resolve_workdir(command, name) -> str | None
        Directory the `name` invocation runs in: its own `-C` flag, else the
        last `cd` that ran before it in the same command string.

    flag_value(argv, *flags) -> str | None
        First value of `--flag value` / `--flag=value` in an argv list.

## Scope: this is a lexer, not a security boundary

It exists because agents legitimately wrap commands — `bash -c "…"`, `cd x &&
…`, a subshell — and a hook that only recognizes the bare form silently stops
applying. It closes that gap. It cannot close every gap: `printf 'gh pr create'
| sh`, `$(echo gh) pr create`, a shell function, a script file, or a PATH shim
all still read as something else, and `_MAX_DEPTH` is an admission of the same
limit. Local hooks are the fast signal; CI stays the authoritative check. Don't
grow this module chasing adversarial evasions.

## Newlines and heredocs (deliberate, and different from sdlc-guardrails)

Newlines ARE command separators here: `foo\ngh pr create` is two commands and
the second must be seen. Heredoc bodies are the reason that's normally unsafe —
in

    gh pr create --body-file - <<'EOF'
    ... git commit ...
    EOF

the body is argument data, not commands. `_strip_heredocs` removes those bodies
before anything is split, so the body can't produce segments *and* real
newline-separated commands are still seen. The sibling hook
`sdlc-guardrails/hooks/_block-main-commits.py` makes the opposite trade (no
newline split, no heredoc machinery) and documents why at its own `_SHELL_SEP`;
it cannot import this module — separate plugins, and cross-plugin
`$CLAUDE_PLUGIN_ROOT`-relative paths don't resolve.

## Failure posture

Parsing is best-effort. On an unparseable string (unbalanced quotes) we retry
once with the quote closed and then fall back to a whitespace split, so a
malformed command degrades toward more matching, or at worst to one inert
segment — never to zero segments, which every caller would read as "nothing to
enforce here". (`echo 'unclosed && gh pr create` is the at-worst case: it
collapses to a single `echo` segment.)

Two places can still degrade toward LESS matching, both bounded and both
commented where they live: `_VALUE_FLAGS`, when a value-taking flag is missing
from the table, and `_strip_heredocs`, if a heredoc's body were mis-detected —
which is why it consumes nothing unless it finds the terminator.

Pure stdlib.
"""

import os
import re
import shlex

# Characters that separate one command from the next. shlex groups runs of them
# into standalone tokens — `&&`, but also `;(` in `foo;(gh pr create)` — so a
# token is a separator when it is punctuation ALL the way through, not when it
# matches a fixed list. "\n" is in here, which is why newlines separate commands
# while a newline *inside* quotes stays part of its token.
_PUNCTUATION = "();<>|&\n"

# Shell keywords that can precede a command inside a compound statement. Left
# in place they become the segment's head, and `if true; then gh pr create; fi`
# reads as an invocation of `then`.
_KEYWORDS = {"if", "then", "elif", "else", "do", "while", "until", "!", "{", "}"}


def _is_separator(token):
    return bool(token) and all(ch in _PUNCTUATION for ch in token)


# Wrapper programs that run another command given as their arguments. Their own
# leading flags are skipped, then parsing continues with the wrapped command.
_WRAPPERS = {
    "env",
    "nohup",
    "time",
    "command",
    "exec",
    "builtin",
    "nice",
    "stdbuf",
    "sudo",
    "timeout",
    "xargs",
}

# Known misses, listed so the next reader can tell omission from decision:
# `uv run …`, `poetry run …`, `npx …`, `direnv exec <dir> …` each take a
# subcommand (and sometimes a positional) before the real command, so they'd
# need their own rule rather than a table entry. They're absent because none of
# them is a plausible way to reach `gh pr create` or `git commit` — add one when
# that stops being true, not preemptively.

# Wrappers taking positional arguments of their own before the command:
# `timeout 60 gh pr create` — the duration is not the command.
_WRAPPER_POSITIONALS = {"timeout": 1}

# Wrapper flags that consume the following token as their value. `env -S` is
# deliberately absent: its value is itself a command string, so it's recursed
# into rather than skipped (skipping it would hide `env -S 'gh pr create'`).
_WRAPPER_VALUE_FLAGS = {
    "env": {"-u", "--unset", "-C", "--chdir"},
    "nice": {"-n"},
    "stdbuf": {"-i", "-o", "-e"},
    "xargs": {"-I", "-i", "-n", "-P", "-d", "-E", "-L", "-s", "-a"},
}
_ENV_STRING_FLAGS = {"-S", "--split-string"}

# Shells whose `-c <string>` argument is a nested command string.
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}

# Interpreters invoked as `<interp> [flags] <script> [args]`: the script is the
# real command, so the interpreter and its flags are dropped.
_INTERPRETERS = re.compile(r"^(?:python[\d.]*|pypy[\d.]*|node|bun|deno|ruby|perl)$")

# Flags that consume the following token as their value, per head command. Only
# flags that can appear *before* a subcommand matter here.
#
# This table is the one place that fails toward LESS matching: a value-taking
# flag missing from it eats the subcommand, so `git --exec-path /x commit` is
# not seen as a commit. Additions are cheap; prefer adding one over widening
# the matcher, which would let `git log --grep commit` back in.
_VALUE_FLAGS = {
    "git": {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--config-env",
        "--super-prefix",
    },
    "gh": {"-R", "--repo"},
}

_MAX_DEPTH = 5  # guards against pathological `bash -c "bash -c ..."` nesting


# `<<WORD` / `<<-WORD` / `<<'WORD'`. The lookarounds exclude `<<<` (a
# here-string, whose operand is a value, not a delimiter) — without them
# `cat <<< foo` reads as a heredoc named `foo` that never terminates. The
# delimiter charset is deliberately loose (`EOF-1`, `EOF.txt` are legal words):
# too narrow and a real heredoc goes unrecognized, leaking its body as commands.
_HEREDOC_RE = re.compile(
    r"(?<!<)<<-?(?!<)\s*"
    r"(?:\"([^\"]+)\"|'([^']+)'|([A-Za-z_][A-Za-z0-9_.-]*))"
)


def _unquoted_positions(line, positions):
    """Which of `positions` (ascending) in `line` sit outside quotes.

    One forward pass over the line, carrying quote state across positions.
    Checking each position independently would rescan from index 0 every time —
    quadratic in line length, which a 97KB single line turns into ~13s, past
    the hook's timeout."""
    result = []
    quote = None
    cursor = 0
    for pos in positions:
        for ch in line[cursor:pos]:
            if quote:
                if ch == quote:
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
        cursor = pos
        result.append(quote is None)
    return result


def _strip_heredocs(command):
    """Remove heredoc bodies, keeping the line that introduces them.

    A heredoc body is argument data; leaving it in would let quoted text inside
    a PR body or commit message look like a command. The redirection operator
    itself is kept so the introducing line still parses normally. Multiple
    heredocs may open on one line — they terminate in order, so all of them are
    consumed rather than just the first.

    Two guards keep this from eating real commands. A `<<` inside quotes is
    text, not an operator (`git commit -m "A << B"`, a `<<<<<<< HEAD` conflict
    marker). And a heredoc with no terminator in the string consumes nothing:
    dropping to EOF on a mis-parse would silently hide every following
    command."""
    lines = command.split("\n")
    # Where each possible delimiter line lives, indexed once. Scanning forward
    # per opener instead is O(openers × lines): 6000 unterminated `<<` on one
    # line took ~0.6s, and the work is repeated for every opener that never
    # terminates.
    terminators = {}
    for idx, line in enumerate(lines):
        terminators.setdefault(line.strip(), []).append(idx)

    def first_terminator_after(delim, start):
        for idx in terminators.get(delim, ()):
            if idx >= start:
                return idx
        return None

    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        matches = list(_HEREDOC_RE.finditer(line))
        if not matches:
            continue
        unquoted = _unquoted_positions(line, [m.start() for m in matches])
        for m, is_real in zip(matches, unquoted):
            if not is_real:
                continue
            delim = m.group(1) or m.group(2) or m.group(3)
            end = first_terminator_after(delim, i)
            if end is not None:
                i = end + 1  # body consumed, delimiter line dropped with it
    return "\n".join(out)


def _tokenize(command):
    """shlex tokens for the whole command, operators and newlines included.

    Degrades instead of raising: shlex only reports an unbalanced quote at EOF,
    so we retry once with the quote closed, then fall back to a whitespace
    split. Returning nothing would read as 'no commands here' to every caller —
    the wrong direction to fail in."""
    for attempt in (command, command + '"', command + "'"):
        lex = shlex.shlex(attempt, posix=True, punctuation_chars=_PUNCTUATION)
        lex.whitespace = " \t\r"  # not \n: it's a separator, handled above
        lex.whitespace_split = True
        # shlex's default commenters ('#') would swallow from '#' to end of
        # LINE — including the newline that separates the next command. One
        # trailing comment would then hide everything after it.
        lex.commenters = ""
        try:
            return list(lex)
        except ValueError:
            continue
    # Last resort (a trailing backslash inside an unclosed quote defeats all
    # three passes). A plain .split() would glue `cd /tmp;gh` into one token
    # and lose the command, so keep the separators as their own tokens — that
    # is what makes this fallback degrade toward more matching, not less.
    return [
        t
        for t in re.split(r"([" + re.escape(_PUNCTUATION) + r"]+|\s+)", command)
        if t and not t.isspace()
    ]


def _strip_env_prefix(argv):
    i = 0
    while i < len(argv) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[i]):
        i += 1
    return argv[i:]


def _skip_flags(argv, value_flags=frozenset()):
    while argv and argv[0].startswith("-"):
        argv = argv[2:] if argv[0] in value_flags else argv[1:]
    return argv


def _normalize(argv, depth, scope=0):
    """Reduce one raw argv to the (argv, scope) pairs of what it really runs.

    Returns a list because `bash -c "a; b"` and `eval` expand to several."""
    while argv and argv[0] in _KEYWORDS:
        argv = argv[1:]
    argv = _strip_env_prefix(argv)
    if not argv:
        return []
    head = os.path.basename(argv[0])

    if depth < _MAX_DEPTH:
        if head == "eval":
            # `eval a b c` concatenates its arguments into one command string.
            return _segments(" ".join(argv[1:]), depth + 1, scope)
        if head in _SHELLS:
            for i, tok in enumerate(argv[1:], start=1):
                if not tok.startswith("-"):
                    break  # `bash script.sh` — a file, nothing to parse
                # Short flags cluster: `bash -lc "…"` and `bash -c"…"` are as
                # common as the bare `-c`, and each is a full wrapper.
                if tok.startswith("--") or "c" not in tok[1:]:
                    continue
                after_c = tok[tok.index("c", 1) + 1 :]
                if after_c:
                    return _segments(after_c, depth + 1, scope)
                if i + 1 < len(argv):
                    return _segments(argv[i + 1], depth + 1, scope)
        if head in _WRAPPERS:
            rest = argv[1:]
            if head == "env":
                for i, tok in enumerate(rest):
                    if tok in _ENV_STRING_FLAGS and i + 1 < len(rest):
                        return _segments(rest[i + 1], depth + 1, scope)
            rest = _skip_flags(rest, _WRAPPER_VALUE_FLAGS.get(head, frozenset()))
            rest = rest[_WRAPPER_POSITIONALS.get(head, 0) :]
            return _normalize(rest, depth + 1, scope) if rest else []
        if _INTERPRETERS.match(head):
            rest = _skip_flags(argv[1:])
            return _normalize(rest, depth + 1, scope) if rest else []

    # An empty head is not a command (it comes from a stray empty token in a
    # malformed string); dropping it keeps callers from having to guard argv[0].
    return [(argv, scope)] if argv[0] else []


# Tokens that open and close a nested scope. A `cd` inside one doesn't apply
# to commands outside it — `(cd /tmp && x) && gh pr create` runs gh in the
# original directory — so segments carry the scope depth they were found at.
_SCOPE_OPEN = {"if", "for", "while", "until", "case", "select"}
_SCOPE_CLOSE = {"fi", "done", "esac"}


def _segments(command, depth=0, scope=0):
    """Yield (argv, scope_depth) for every command in `command`.

    `scope_depth` counts enclosing subshells and compound statements; it's what
    lets resolve_workdir tell a `cd` that applies to a later command from one
    that the shell confines to a block."""
    segs = []
    current = []
    skip_next = False

    def flush():
        if current:
            # _normalize carries `scope` down through its own recursion, so the
            # pairs come back already tagged — don't add it twice.
            segs.extend(_normalize(current, depth, scope))

    for tok in _tokenize(_strip_heredocs(command)):
        if skip_next:
            skip_next = False
            continue
        if _is_separator(tok):
            # A redirection doesn't end the command, and its target is a
            # filename: `> out gh pr create` is still a pr-create, and the
            # `out` must not become a segment head.
            if "<" in tok or ">" in tok:
                skip_next = True
                continue
            flush()
            current = []
            scope += tok.count("(") - tok.count(")")
        else:
            if tok in _SCOPE_OPEN:
                scope += 1
            elif tok in _SCOPE_CLOSE:
                scope = max(0, scope - 1)
            current.append(tok)
    flush()
    return segs


def _scoped_segments(command):
    # Non-str is a malformed payload, not a command. Guarded here rather than
    # left to raise: every caller turns an exception into "allow", so a changed
    # payload shape would silently disable enforcement instead of finding zero
    # commands in it — which is the same answer, arrived at honestly.
    if not command or not isinstance(command, str):
        return []
    # A `\`-newline is a line continuation, not a separator. Joining before
    # tokenizing is what makes `git \<newline> commit` one command.
    return _segments(re.sub(r"\\\n", " ", command))


def command_segments(command):
    """Every command in `command`, as normalized argv lists."""
    return [argv for argv, _ in _scoped_segments(command)]


def _argv_invokes(argv, words):
    if not argv or os.path.basename(argv[0]) != words[0]:
        return False
    value_flags = _VALUE_FLAGS.get(words[0], frozenset())
    want = words[1:]
    i = 1
    while i < len(argv) and want:
        tok = argv[i]
        if tok.startswith("-"):
            i += 2 if tok in value_flags else 1  # value flags eat the next token
            continue
        if tok != want[0]:
            return False  # a different positional: this is another subcommand
        want = want[1:]
        i += 1
    return not want


def _mentions(command, words):
    """Cheap pre-filter: skip tokenizing when the head word isn't even present.

    These hooks run on every Bash call, so the common case (`ls`, `cat`, `rg`)
    should cost a substring scan, not a parse. Only the head word is checked:
    requiring all of them would drop `gh "pr" create`, which tokenizes fine.
    Even the head check is a heuristic, not a proof — `g"h" pr create` defeats
    it, as it defeats the rest of this lexer (see Scope above)."""
    return isinstance(command, str) and words[0] in command


def matching_segments(command, name):
    """argv of every segment invoking `name` (e.g. "gh pr create")."""
    words = name.split()
    if not words or not command or not _mentions(command, words):
        return []
    return [a for a in command_segments(command) if _argv_invokes(a, words)]


def invokes(command, name):
    """True when `command` actually invokes `name` as a command."""
    return bool(matching_segments(command, name))


class Invocation:
    """One matched command, with everything a caller needs about it.

    Bundled because deriving these separately means re-parsing the whole string
    per question — and, worse, `resolve_workdir` answers for the FIRST match
    only, so a second invocation would silently be judged against the first
    one's directory."""

    __slots__ = ("argv", "workdir", "names_workdir")

    def __init__(self, argv, workdir, names_workdir):
        self.argv = argv
        self.workdir = workdir
        # True when this invocation asked for a directory we could not resolve
        # — the caller falls back to its own cwd and should say so.
        self.names_workdir = names_workdir


def invocations(command, name):
    """Every invocation of `name`, each with its own resolved workdir.

    One parse for the whole question. See resolve_workdir for the three rules
    that decide which `cd`/`-C` applies to a given invocation."""
    words = name.split()
    if not words or not command or not _mentions(command, words):
        return []
    found = []
    last_cd = None
    for argv, scope in _scoped_segments(command):
        if _argv_invokes(argv, words):
            raw = _pre_subcommand_flag(argv, words, "-C")
            if raw is None and last_cd and last_cd[1] <= scope:
                raw = last_cd[0]
            found.append(Invocation(argv, _existing_dir(raw), raw is not None))
        elif argv[0] == "cd" and len(argv) > 1:
            last_cd = (argv[1], scope)
    return found


def resolve_workdir(command, name):
    """Existing directory the FIRST `name` invocation runs in, or None.

    Its own `-C` flag wins; otherwise the last `cd` that ran before it, in a
    scope that still encloses it. Three rules, each one closing a way the
    caller could end up inspecting the wrong repo — which is worse than
    inspecting none, because "wrong repo" reads as "no gate cycle" and allows:

    - Segment-scoped: a `cd` or `-C` quoted inside someone else's argument (a
      PR body, a commit message) is data, not a directory change.
    - Scope-aware: `(cd /tmp && x) && gh pr create` runs gh in the original
      directory, so a `cd` nested deeper than the invocation is ignored.
    - Verified: shlex doesn't expand `~`, `$VAR`, `$(…)` or `cd -`, so the word
      may not name a directory at all. Anything that isn't an existing dir
      returns None, which puts the caller back on its own cwd — the honest
      "I don't know" rather than a confident wrong answer.

    Callers handling more than one invocation want `invocations()` instead —
    this answers for the first one only."""
    found = invocations(command, name)
    return found[0].workdir if found else None


def _existing_dir(path):
    if not path:
        return None
    expanded = os.path.expanduser(path)
    return expanded if os.path.isdir(expanded) else None


def _pre_subcommand_flag(argv, words, flag):
    """Value of `flag` appearing BEFORE the subcommand, e.g. the chdir in
    `git -C dir commit`.

    The position is the whole meaning: after the subcommand, `git commit -C
    HEAD` is "reuse that commit's message", and reading it as a directory sends
    the hook looking for a repo at `HEAD`."""
    want = list(words[1:])
    i = 1
    while i < len(argv) and want:
        tok = argv[i]
        if tok == flag and i + 1 < len(argv):
            return argv[i + 1]
        if tok.startswith(flag + "="):
            return tok[len(flag) + 1 :]
        if not tok.startswith("-"):
            want.pop(0)
        elif tok in _VALUE_FLAGS.get(words[0], frozenset()):
            i += 1
        i += 1
    return None


def flag_value(argv, *flags):
    """First value of `--flag value` or `--flag=value` in `argv`.

    Deliberately does NOT understand clustered short flags (`-Hbranch`). It
    scans every token without knowing which are flags and which are values, so
    a clustered rule here reads `--title "-Hotfix: ..."` as a head branch — a
    gate bypass triggered by a plausible PR title. A caller that needs the
    clustered form must parse positionally, where it can tell a flag from a
    value; see enforce-sdlc-gates.extract_head_flag."""
    for i, tok in enumerate(argv):
        for flag in flags:
            if tok == flag and i + 1 < len(argv):
                return argv[i + 1]
            if tok.startswith(flag + "="):
                return tok[len(flag) + 1 :]
    return None
