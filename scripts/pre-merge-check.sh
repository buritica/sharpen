#!/usr/bin/env bash
# Repo-local PreToolUse hook: run the marketplace consistency check before
# `gh pr create` / `gh pr merge`, so version drift or a broken plugin can't
# reach a PR. Reads the Claude Code hook JSON from stdin; denies (exit 2)
# with the check output when it fails.
#
# Wired in this repo's .claude/settings.json. Self-enforcing for the
# sharpen marketplace only -- it does not affect other repos.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

INPUT=$(cat)
TOOL=$(printf '%s' "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_name',''))" 2>/dev/null || echo "")
[ "$TOOL" = "Bash" ] || exit 0

COMMAND=$(printf '%s' "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('command',''))" 2>/dev/null || echo "")

# Only gate the operations that publish/merge a PR.
printf '%s' "$COMMAND" | grep -qE 'gh\s+pr\s+(create|merge)' || exit 0

OUTPUT=$(python3 "$REPO_ROOT/scripts/check-marketplace.py" 2>&1) && exit 0

# Failed: deny with a single-line reason (JSON-escaped via python).
printf '%s' "$OUTPUT" | python3 -c "
import sys, json
msg = sys.stdin.read().strip()
print(json.dumps({'decision': 'deny', 'reason': 'Marketplace check failed; fix before opening/merging a PR:\n' + msg}))
" >&2
exit 2
