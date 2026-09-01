---
name: guard
description: "Manage main-branch commit protection for the current repo (opt-in). on|off|status|list, or default-on|default-off for the global default."
---

# /guard — Main-Branch Protection

Manage the sdlc-guardrails `block-main-commits` policy. Protection is
**opt-in per repo**: by default no repo is protected, so the hook is silent
until you turn it on here. State lives in a user-level config at
`${CLAUDE_CONFIG_DIR:-~/.claude}/sdlc-guardrails.json` and applies on this
machine across every repo.

Map the argument `$ARGUMENTS` (default to `status` if empty) to a subcommand
and run the helper, then report its output verbatim to the user:

| Argument                  | Action                                                       |
| ------------------------- | ------------------------------------------------------------ |
| `status` (or empty)       | Show whether the current repo is protected                   |
| `on`                      | Protect the current repo (block commits/pushes to main)      |
| `off`                     | Stop protecting the current repo                              |
| `list`                    | List every protected repo (and exceptions, if default-on)    |
| `default-on`              | Protect every repo by default; `off` on a specific repo then records an exception for it |
| `default-off`             | Default to no protection (the shipped default)               |

Run exactly one of:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/_guardrails_config.py" status
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/_guardrails_config.py" protect    # on
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/_guardrails_config.py" unprotect  # off
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/_guardrails_config.py" list
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/_guardrails_config.py" default-on
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/_guardrails_config.py" default-off
```

`status`/`on`/`off` act on the current working directory's repo. If it is not
a git repo, the helper exits non-zero — relay that to the user. `list` and the
`default-*` commands are global and work outside any repo.

For a one-off override without changing config, the user can prefix a single
command with `SDLC_ALLOW_MAIN=1`.
