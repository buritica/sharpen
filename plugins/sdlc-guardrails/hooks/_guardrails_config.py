#!/usr/bin/env python3
"""
Shared config for sdlc-guardrails main-branch protection.

Protection is OPT-IN per repo. By default no repo is protected, so
`git commit`/`git push` on main/master are allowed everywhere and the hook
stays out of the way. A repo becomes protected when it is listed in
`protectedRepos`, or when `protectMainDefault` is true (in which case
`unprotectedRepos` carves out exceptions).

Config lives at ${CLAUDE_CONFIG_DIR:-~/.claude}/sdlc-guardrails.json:

    {
      "protectMainDefault": false,
      "protectedRepos": ["/abs/path/to/repo"],
      "unprotectedRepos": []
    }

Set SDLC_ALLOW_MAIN=1 to bypass every check for a single command/session.

Run directly to manage the current (or given) repo:

    python3 _guardrails_config.py status     [path]
    python3 _guardrails_config.py protect    [path]
    python3 _guardrails_config.py unprotect  [path]
    python3 _guardrails_config.py list
    python3 _guardrails_config.py default-on
    python3 _guardrails_config.py default-off
"""

import json
import os
import subprocess
import sys


def config_path():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(base, "sdlc-guardrails.json")


def load():
    try:
        with open(config_path()) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("protectMainDefault", False)
    cfg.setdefault("protectedRepos", [])
    cfg.setdefault("unprotectedRepos", [])
    return cfg


def save(cfg):
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def repo_root(target):
    """Absolute, symlink-resolved toplevel of the repo at `target`, or None."""
    try:
        out = subprocess.check_output(
            ["git", "-C", target, "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return os.path.realpath(out.decode().strip())


def bypass_env():
    return os.environ.get("SDLC_ALLOW_MAIN", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _norm_set(paths):
    return {os.path.realpath(os.path.expanduser(p)) for p in paths}


def is_protected(root, cfg=None):
    if root is None:
        return False
    cfg = cfg if cfg is not None else load()
    norm = os.path.realpath(root)
    if norm in _norm_set(cfg["unprotectedRepos"]):
        return False
    if norm in _norm_set(cfg["protectedRepos"]):
        return True
    return bool(cfg["protectMainDefault"])


def _cli():
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    cfg = load()

    if cmd in ("default-on", "default-off"):
        cfg["protectMainDefault"] = cmd == "default-on"
        save(cfg)
        print("protectMainDefault = {}".format(cfg["protectMainDefault"]))
        print("config = {}".format(config_path()))
        return 0

    if cmd == "list":
        print("protectMainDefault = {}".format(cfg["protectMainDefault"]))
        protected = sorted(_norm_set(cfg["protectedRepos"]))
        excepted = sorted(_norm_set(cfg["unprotectedRepos"]))
        if protected:
            print("protected repos ({}):".format(len(protected)))
            for p in protected:
                print("  + {}".format(p))
        else:
            print("protected repos: none")
        if cfg["protectMainDefault"] and excepted:
            print("exceptions ({}, allowed despite default-on):".format(len(excepted)))
            for p in excepted:
                print("  - {}".format(p))
        print("config = {}".format(config_path()))
        return 0

    target = args[1] if len(args) > 1 else os.getcwd()
    root = repo_root(target)
    if root is None:
        sys.stderr.write("Not a git repo: {}\n".format(target))
        return 1

    if cmd == "protect":
        cfg["unprotectedRepos"] = [
            p
            for p in cfg["unprotectedRepos"]
            if os.path.realpath(os.path.expanduser(p)) != root
        ]
        if root not in _norm_set(cfg["protectedRepos"]):
            cfg["protectedRepos"].append(root)
        save(cfg)
        print("Protected (main commits blocked): {}".format(root))
    elif cmd == "unprotect":
        cfg["protectedRepos"] = [
            p
            for p in cfg["protectedRepos"]
            if os.path.realpath(os.path.expanduser(p)) != root
        ]
        # Only need an explicit exception when the global default is on.
        if cfg["protectMainDefault"] and root not in _norm_set(cfg["unprotectedRepos"]):
            cfg["unprotectedRepos"].append(root)
        save(cfg)
        print("Unprotected (main commits allowed): {}".format(root))
    elif cmd == "status":
        state = "PROTECTED" if is_protected(root, cfg) else "not protected"
        print("{}: main commits {}".format(root, state))
        print("protectMainDefault = {}".format(cfg["protectMainDefault"]))
        print("config = {}".format(config_path()))
    else:
        sys.stderr.write(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
