"""Every way a tool call can open a pull request, as one host-neutral answer.

The PR gate used to recognize exactly one thing: a Bash call whose argv was
`gh pr create`. Everything else (the `gh pr new` alias, `gh api` POSTs, glab,
hub, GitLab push options, a GitHub/GitLab MCP tool, Claude Code's Monitor tool,
any pi tool but bash) went through as "not a PR", and so did a `gh pr create`
whose `--head` the shell had to expand. This module is the single place that
answers "does this tool call open a PR, and for which branch in which
checkout?" so every host adapter (Claude Code and Codex via hooks.json, pi via
its extension) asks the same question and the gate policy stays in
enforce-sdlc-gates.py.

    pr_opens(tool_name, tool_input) -> [PrOpen, ...]

What it deliberately does not try to see: opaque scripts (`make pr`,
`./open-pr.sh`, `python -c ...`), user-defined gh aliases, `curl` against the
REST API, and shell spellings the lexer can't read (`g"h"`, `$GH`). Guessing at
those fails toward allow anyway. They are what the git pre-push floor
(pre-push.py) is for.

Pure stdlib. @fires-on nothing (library)
"""

import os
import re
import subprocess

import gate_store as gs
import shell_parse as sp

# Tools whose input is a shell command string. `Bash` covers Claude Code and
# Codex (Codex reports both its shell and `exec_command` as `Bash`); `Monitor`
# is Claude Code's background-watch tool, which runs a shell `command` outside
# the Bash matcher; `bash` is pi's tool name.
SHELL_TOOLS = {"Bash", "Monitor", "bash"}

# MCP PR-creation tools, matched by suffix so the server's name doesn't matter:
# Claude Code and Codex name them `mcp__<server>__<tool>`, pi's MCP extension
# `mcp_<server>_<tool>`. GitHub's server calls it create_pull_request, GitLab's
# create_merge_request.
_MCP_PR_TOOL = re.compile(r"(?:^|_)create_(?:pull|merge)_request$")

# Shell commands that open a PR on their own (argv chains for shell_parse).
_GH_CREATE = ("gh pr create", "gh pr new")  # `new` is gh's built-in alias

# gh pr create flags that consume the following token. Needed so the scan below
# can tell a flag from a value: `--title "-Hotfix: ..."` must not be read as a
# head branch. Over-listing is harmless here (a missed flag only costs us the
# `-H` clustered form); under-listing is what creates a bypass.
GH_VALUE_FLAGS = {
    "--title",
    "-t",
    "--body",
    "-b",
    "--body-file",
    "-F",
    "--base",
    "-B",
    "--head",
    "-H",
    "--label",
    "-l",
    "--assignee",
    "-a",
    "--reviewer",
    "-r",
    "--milestone",
    "-m",
    "--project",
    "-p",
    "--template",
    "-T",
    "--repo",
    "-R",
}

# gh api flags that send a request body, which makes gh default to POST.
_GH_API_FIELD_FLAGS = {"-f", "--raw-field", "-F", "--field", "--input"}
_GH_API_VALUE_FLAGS = _GH_API_FIELD_FLAGS | {
    "-X",
    "--method",
    "-H",
    "--header",
    "-q",
    "--jq",
    "-t",
    "--template",
    "--hostname",
    "--cache",
    "-p",
    "--preview",
}
_PULLS_PATH = re.compile(r"^/?repos/([^/]+)/([^/]+)/pulls/?$")


class PrOpen:
    """One attempted PR open.

    head: the branch the PR is for, normalized to how the gate store keys it,
        or None to mean "the branch checked out in `workdir`".
    workdir: the checkout to judge it against, or None for this process's cwd.
    names_workdir: the call asked for a directory that could not be resolved.
    no_checkout: the call named a repo that has no local checkout in reach.
    notes: caveats about how head/workdir were reached, for the verdict.
    label: how to name this path in a message ("gh pr create", "MCP ...").
    """

    __slots__ = ("label", "head", "workdir", "names_workdir", "notes", "no_checkout")

    def __init__(
        self, label, head, workdir, names_workdir=False, notes=None, no_checkout=False
    ):
        self.label = label
        self.head = head
        self.workdir = workdir
        self.names_workdir = names_workdir
        self.notes = list(notes or [])
        self.no_checkout = no_checkout

    def __repr__(self):
        return f"PrOpen({self.label!r}, head={self.head!r}, workdir={self.workdir!r})"


def pr_opens(tool_name, tool_input):
    """Every PR this tool call would open (usually zero or one)."""
    if not isinstance(tool_input, dict):
        return []
    if tool_name in SHELL_TOOLS:
        command = tool_input.get("command")
        return shell_pr_opens(command) if isinstance(command, str) else []
    if isinstance(tool_name, str) and _MCP_PR_TOOL.search(tool_name):
        return [_mcp_pr_open(tool_name, tool_input)]
    return []


# --- shell ---------------------------------------------------------------


def shell_pr_opens(command):
    found = []
    for name in _GH_CREATE:
        for inv in sp.invocations(command, name):
            found.append(_gh_create(name, inv))
    for inv in sp.invocations(command, "gh api"):
        pr = _gh_api(inv)
        if pr:
            found.append(pr)
    for inv in sp.invocations(command, "glab mr create"):
        head = sp.flag_value(inv.argv, "--source-branch", "-s")
        found.append(_shell_open("glab mr create", head, inv))
    for inv in sp.invocations(command, "hub pull-request"):
        head = sp.flag_value(inv.argv, "--head", "-h")
        found.append(_shell_open("hub pull-request", head, inv))
    for inv in sp.invocations(command, "git push"):
        pr = _git_push_mr(inv)
        if pr:
            found.append(pr)
    return found


def _shell_open(label, raw_head, inv, repo=None):
    head, notes = normalize_head(raw_head)
    workdir, found, more = resolve_checkout(inv.workdir, repo)
    return PrOpen(label, head, workdir, inv.names_workdir, notes + more, not found)


def _gh_create(name, inv):
    repo = sp.flag_value(inv.argv, "--repo", "-R")
    return _shell_open(name, extract_head_flag(inv.argv), inv, repo)


def extract_head_flag(argv):
    """The raw head branch from `--head`/`-H`, in every spelling gh accepts.

    Parsed positionally rather than via sp.flag_value because gh (pflag) also
    accepts the clustered `-Hbranch`, and recognizing that requires knowing
    which tokens are flags. A naive scan reads `--title "-Hotfix: crash"` as a
    head branch. normalize_head() turns the result into a store key."""
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("--head", "-H") and i + 1 < len(argv):
            return argv[i + 1]
        if tok.startswith("--head=") or tok.startswith("-H="):
            return tok.split("=", 1)[1]
        # Clustered `-Hbranch`, but only in a flag position — never a value.
        if tok.startswith("-H") and len(tok) > 2 and not tok.startswith("--"):
            return tok[2:]
        i += 2 if tok in GH_VALUE_FLAGS else 1
    return None


def _gh_api(inv):
    """`gh api` that creates a PR: a POST to repos/<o>/<r>/pulls, or a graphql
    createPullRequest mutation."""
    argv = inv.argv
    positionals, fields, method = [], {}, None
    i = 2  # past `gh api`
    while i < len(argv):
        tok = argv[i]
        flag, eq, inline = tok.partition("=") if tok.startswith("--") else (tok, "", "")
        if flag in _GH_API_VALUE_FLAGS:
            value = inline if eq else (argv[i + 1] if i + 1 < len(argv) else "")
            i += 1 if eq else 2
            if flag in ("-X", "--method"):
                method = value.upper()
            elif flag in _GH_API_FIELD_FLAGS:
                key, _, val = value.partition("=")
                fields[key] = val
            continue
        if not tok.startswith("-"):
            positionals.append(tok)
        i += 1
    if not positionals:
        return None
    endpoint = positionals[0]
    if method is None:
        method = "POST" if fields else "GET"
    if endpoint == "graphql":
        query = fields.get("query", "")
        if "createPullRequest" not in query:
            return None
        m = re.search(r'headRefName\s*:\s*\\?"([^"\\]+)', query)
        return _shell_open("gh api graphql createPullRequest", m and m.group(1), inv)
    m = _PULLS_PATH.match(endpoint)
    if not m or method != "POST":
        return None
    owner, name = m.groups()
    repo = None if "{" in owner + name else f"{owner}/{name}"
    return _shell_open("gh api .../pulls", fields.get("head"), inv, repo)


_MR_PUSH_OPTIONS = ("merge_request.create", "mr.create")


def _git_push_mr(inv):
    """`git push -o merge_request.create`: GitLab opens an MR on receive."""
    argv = inv.argv
    opts, positionals = [], []
    i = 2
    while i < len(argv):
        tok = argv[i]
        if tok in ("-o", "--push-option") and i + 1 < len(argv):
            opts.append(argv[i + 1])
            i += 2
            continue
        if tok.startswith("--push-option="):
            opts.append(tok.split("=", 1)[1])
        elif tok.startswith("-o") and len(tok) > 2:
            opts.append(tok[2:])
        elif not tok.startswith("-"):
            positionals.append(tok)
        i += 1
    if not any(o.split("=", 1)[0] in _MR_PUSH_OPTIONS for o in opts):
        return None
    head = None
    if len(positionals) >= 2:
        head = positionals[1].lstrip("+").split(":", 1)[0]
    return _shell_open("git push -o merge_request.create", head, inv)


# --- MCP -----------------------------------------------------------------


def _mcp_pr_open(tool_name, tool_input):
    owner, repo = tool_input.get("owner"), tool_input.get("repo")
    hint = f"{owner}/{repo}" if owner and repo else tool_input.get("project_id")
    raw = tool_input.get("head") or tool_input.get("source_branch")
    head, notes = normalize_head(raw if isinstance(raw, str) else None)
    workdir, found, more = resolve_checkout(
        None, hint if isinstance(hint, str) else None
    )
    return PrOpen(f"MCP {tool_name}", head, workdir, False, notes + more, not found)


# --- shared --------------------------------------------------------------


def normalize_head(head):
    """A raw head value -> (store key or None, notes).

    None means "use the branch checked out in the workdir". That is the right
    answer for no head at all, for `HEAD`/`@`, and for anything the shell would
    have expanded (`"$(git branch --show-current)"`, `$BRANCH`, xargs' `{}`):
    taken literally, those never match a stored branch and read as "no cycle,
    allow", a one-flag bypass of the gate."""
    if not head:
        return None, []
    if head in ("HEAD", "@"):
        return None, []
    if any(c in head for c in "$`{}*?"):
        return None, [
            f"The PR's head ({head!r}) is expanded by the shell, so the gate "
            "judged the branch checked out in the working directory instead."
        ]
    # `--head owner:branch` (a fork) and `refs/heads/branch` aren't how the
    # store is keyed. Owner first: `owner:refs/heads/branch` is legal too.
    if ":" in head:
        head = head.split(":", 1)[1]
    if head.startswith("refs/heads/"):
        head = head[len("refs/heads/") :]
    # A degenerate `owner:` normalizes to "", i.e. "use the workdir branch".
    # gh rejects that input anyway, so it lets no real command through.
    return head or None, []


def resolve_checkout(workdir, repo_hint):
    """The local checkout a PR for `repo_hint` ("owner/name") should be judged
    against -> (dir or None, found, notes). `found` is False only when a repo
    was named and no checkout of it is in reach.

    The session's cwd is not always a checkout: a cloud session with several
    repos sits in their parent directory, and an MCP call carries no path at
    all. Judged from there, the store lookup misses and every PR reads as "no
    cycle, allow". So: the given workdir (or cwd) when it is a checkout whose
    remotes match the hint (or there is no hint); otherwise the first checkout
    directly under it whose remote does."""
    base = workdir or os.getcwd()
    top = _git_out(base, "rev-parse", "--show-toplevel")
    if top and (not repo_hint or _remote_matches(top, repo_hint)):
        return workdir, True, []
    if repo_hint:
        for child in _child_checkouts(top or base):
            if _remote_matches(child, repo_hint):
                return child, True, []
        return (
            workdir,
            False,
            [
                f"No local checkout of {repo_hint} was found from {base}, so "
                "there was no gate cycle to check."
            ],
        )
    if not top:
        children = _child_checkouts(base)
        if len(children) == 1:
            return children[0], True, []
    return workdir, True, []


def _child_checkouts(base):
    """Git checkouts one level below `base` (and `base`'s siblings when `base`
    is itself a checkout, which is the multi-repo session's layout)."""
    roots = [base]
    if os.path.exists(os.path.join(base, ".git")):
        roots = [os.path.dirname(base)]
    found = []
    for root in roots:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            path = os.path.join(root, name)
            if path != base and os.path.exists(os.path.join(path, ".git")):
                found.append(path)
    return found


def _remote_matches(checkout, repo_hint):
    want = repo_hint.strip("/").lower().removesuffix(".git")
    out = _git_out(checkout, "remote", "-v") or ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and _repo_slug(parts[1]) == want:
            return True
    return False


def _repo_slug(url):
    """`owner/name` from any remote URL spelling: https, ssh `git@host:o/n`,
    or a proxy path that ends in `/o/n`. Nested GitLab groups keep only the
    last two segments, which is all an MCP owner/repo pair carries anyway."""
    path = re.sub(r"^[a-z+]+://[^/]+/", "", url.strip())
    path = re.sub(r"^[^/@]+@[^:/]+:", "", path)
    path = path.rstrip("/").removesuffix(".git")
    segs = [s for s in path.split("/") if s]
    return "/".join(segs[-2:]).lower() if len(segs) >= 2 else None


def _git_out(cwd, *args):
    try:
        return gs._git(*args, cwd=cwd)
    except (OSError, subprocess.CalledProcessError):
        return None
