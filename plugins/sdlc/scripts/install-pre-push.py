#!/usr/bin/env python3
"""
Install (or refresh) sdlc's git pre-push hook in a repo.

    install-pre-push.py [--root <dir>] [--mode warn|block|off] [--check]

Writes a small shell shim to the repo's COMMON hooks dir (`git rev-parse
--git-common-dir`/hooks), so every linked worktree gets it. The shim does not
embed this plugin's path: plugin caches are version-nested and move on every
update, and a hook pointing at a deleted path would fail every push. Instead
the path lives in `git config sharpen.sdlcScripts`, which this script refreshes
on every run (`/sdlc:gate` runs it on every invocation, and
auto-init-gate-cycle.py runs it whenever it arms a cycle), and the shim fails
open with a message when that path is gone, or when `python3` isn't on the
hook's PATH (GUI git clients, unloaded pyenv shims).

It never overwrites someone else's hook. If a pre-push hook that isn't ours
exists, or `core.hooksPath` points git elsewhere (husky, lefthook, a repo's
own `.githooks/`), it prints the one line to add to that hook and exits 0
(for lefthook, which regenerates its hook files, a `lefthook.yml` snippet
instead). That line exits 0 when sdlc isn't configured on the machine running
it, even under `sh -e` (husky v9) or `set -e`, since a committed `.githooks/`
runs on teammates' clones and CI too. Our own shim is
rewritten whenever its content differs from SHIM, so a fix to it reaches
repos that installed an older one.

`--mode` sets `git config sharpen.prePush` (default warn; see pre-push.py).
`--check` reports whether the shim is installed and current, changing nothing.
Under `core.hooksPath` it can't see whether the chain line was added, so it
says so and exits 0.

Pure stdlib.
"""

import argparse
import os
import subprocess
import sys
import tempfile

MARKER = "# sdlc pre-push shim"
SCRIPTS = os.path.dirname(os.path.abspath(__file__))

SHIM = f"""#!/bin/sh
{MARKER} (installed by install-pre-push.py; safe to delete)
command -v python3 >/dev/null 2>&1 || {{ echo "[sdlc] pre-push: python3 not found; not checked." >&2; exit 0; }}
dir=$(git config --get sharpen.sdlcScripts || true)
if [ -n "$dir" ] && [ -f "$dir/pre-push.py" ]; then
  exec python3 "$dir/pre-push.py" "$@"
fi
echo "[sdlc] pre-push: sdlc scripts not found (sharpen.sdlcScripts=$dir); not checked. Run the sdlc plugin's scripts/install-pre-push.py, or /sdlc:gate, which runs it, to refresh." >&2
exit 0
"""


def git(root, *args, check=True):
    r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)
    if check and r.returncode:
        raise SystemExit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def chain_line():
    """What to add to a pre-push hook someone else owns. Path-free for the same
    reason as the shim: the scripts dir moves on every plugin update. A hook
    that also reads stdin must capture it once and feed this line a copy."""
    # An `if` rather than an `&&` list: with the config unset or stale, an
    # `&&` list exits 1, and as a hook's last line that refuses every push on
    # a machine without sdlc, silently. `|| true` because `git config --get`
    # exits 1 on an unset key, and under `sh -e` (husky v9) or `set -e` a
    # failing command substitution in an assignment exits the hook.
    return (
        "dir=$(git config --get sharpen.sdlcScripts || true); "
        'if [ -n "$dir" ] && [ -f "$dir/pre-push.py" ] && '
        "command -v python3 >/dev/null 2>&1; then "
        'python3 "$dir/pre-push.py" "$@" || exit $?; '
        'elif [ -n "$dir" ]; then echo "[sdlc] pre-push: skipped (no '
        'pre-push.py under sharpen.sdlcScripts=$dir, or no python3)." >&2; fi'
    )


def lefthook_snippet():
    """lefthook rewrites its hook files on `lefthook install`, so a hand edit
    is lost; the command belongs in lefthook.yml. `use_stdin: true` is what
    hands it the push's ref lines: without it pre-push.py reads nothing and
    passes every push."""
    return (
        "pre-push:\n"
        "  commands:\n"
        "    sdlc:\n"
        "      use_stdin: true\n"
        "      run: |\n"
        f"        {chain_line()}"
    )


def common_dir(root):
    """The repo's common git dir, absolute. `--path-format=absolute` needs git
    2.31+; older git prints the path relative to `root` (or absolute), which
    abspath against `root` resolves the same way."""
    r = subprocess.run(
        ["git", "-C", root, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    out = r.stdout.strip()
    if r.returncode or not os.path.isabs(out):
        out = git(root, "rev-parse", "--git-common-dir")
    return os.path.abspath(os.path.join(root, out))


def _write_atomic(path, content):
    """Write via a temp file in the same dir and os.replace, so a push racing
    an install (or two worktrees installing at once) never runs a half-written
    shim."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".pre-push.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp, 0o755)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read(path):
    if not os.path.isfile(path):
        return None
    with open(path, errors="replace") as f:
        return f.read()


def install(root=".", mode=None):
    """Install or refresh the shim -> (status, message). status is one of
    "installed", "updated", "current", "elsewhere". Raises SystemExit when
    `root` is not a git repo."""
    common = common_dir(root)
    hooks_path = git(root, "config", "--get", "core.hooksPath", check=False)
    hook = os.path.join(common, "hooks", "pre-push")
    content = _read(hook)
    ours = content is not None and MARKER in content

    git(root, "config", "sharpen.sdlcScripts", SCRIPTS)
    if mode:
        git(root, "config", "sharpen.prePush", mode)

    if hooks_path or (content is not None and not ours):
        where = hooks_path or hook
        target = os.path.join(root, hooks_path, "pre-push") if hooks_path else hook
        if "lefthook" in (_read(target) or ""):
            return "elsewhere", (
                f"pre-push: not installed, because lefthook manages git hooks ({where}).\n"
                "Add this to lefthook.yml (lefthook regenerates the hook file, so "
                "don't edit it), then run `lefthook install`:\n" + lefthook_snippet()
            )
        return "elsewhere", (
            f"pre-push: not installed, because git hooks are managed elsewhere ({where}).\n"
            f"Add this line to that pre-push hook to chain sdlc's check:\n  {chain_line()}"
        )
    if ours and content == SHIM:
        return "current", "pre-push: already installed; scripts path refreshed"
    _write_atomic(hook, SHIM)
    status = "updated" if ours else "installed"
    return status, f"pre-push: {status} at {hook}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=".")
    ap.add_argument("--mode", choices=("warn", "block", "off"))
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)

    if a.check:
        common = common_dir(a.root)
        hooks_path = git(a.root, "config", "--get", "core.hooksPath", check=False)
        content = _read(os.path.join(common, "hooks", "pre-push"))
        if hooks_path:
            print(
                f"pre-push: hooks managed elsewhere ({hooks_path}); can't tell "
                "whether the chain line is present"
            )
            return 0
        current = git(a.root, "config", "--get", "sharpen.sdlcScripts", check=False)
        if content is None or MARKER not in content:
            print("pre-push: not installed")
            return 1
        # A stale sharpen.sdlcScripts (an old cache dir kept after a plugin
        # update) reports here as "stale"; the shim itself can't tell.
        fresh = current == SCRIPTS and content == SHIM
        print(f"pre-push: installed ({'current' if fresh else 'stale'})")
        return 0 if fresh else 1

    print(install(a.root, a.mode)[1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
