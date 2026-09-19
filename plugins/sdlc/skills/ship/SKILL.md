---
name: ship
description: "Open a PR after gates pass. Handles branch push, PR creation with conventional commit title, and squash merge."
---

# Ship

Open a PR (or merge an existing one) after the gate chain passes.

## Pre-flight

Before opening, verify:

1. **All gates passed.** If you ran `/sdlc:gate`, confirm all gates completed successfully. If not, run it first.
2. **No uncommitted changes.** Run `git status` — everything should be committed.
3. **Branch is up to date.** If the branch is old:
   ```bash
   git fetch origin main
   git rebase origin/main
   ```
   If rebase introduces conflicts, resolve them and re-run `/sdlc:gate`.

## Open the PR

Push and create:

```bash
git push -u origin HEAD
```

Then create the PR with `gh pr create`. Follow these conventions:

**Title:** Conventional commit prefix, under 70 chars. Detail goes in the body.
- `feat(scope):` — new capability
- `fix(scope):` — bug fix
- `chore(scope):` — refactor, deps, tooling
- `docs(scope):` — documentation
- `refactor(scope):` — restructure without behavior change

**Body:** Include `Closes #XXX` when a linked issue exists. Include `## Deferred`
only when the branch added at least one `ponytail:` marker — check
`.claude/grumpy/<branch>/fix.md`'s own `## Deferred` section first; if that
artifact doesn't exist (gated with an older installed grumpy, or fixed by
hand with no `/grumpy:fix` run), fall back to
`git diff origin/main...HEAD -- . ':!*.md' ':!*.mdx' | grep -E '^\+[[:space:]]*(#|//|--|<!--)[[:space:]]*ponytail:'`
— anchored to an actual comment prefix (not a bare substring match, which a
PR merely *documenting* the convention in prose would trip) AND scoped away
from documentation files (`.md`/`.mdx` — extend this list for any other
prose format the repo uses), since a real deferral lands in the source a
`/grumpy:fix` run touched, never in documentation — a plugin repo whose own docs show the
marker syntax inside a fenced code example (exactly what this PR does) would
otherwise self-trigger a false `## Deferred` section, which is how this
exact edge case got caught. Even with both guards this is pattern-matching
text, not parsing comments, so it can still be fooled by an unusual case;
it exists only as a backstop when the fix report is missing, not as the
primary source. So a marker never ships invisibly just because the report
wasn't written. Omit the whole section when neither source finds anything —
most PRs defer nothing.

```bash
gh pr create --title "<prefix>(<scope>): <short description>" --body "$(cat <<'EOF'
## Summary
<1-3 bullet points describing what changed and why>

Closes #XXX

## Verification
<how to verify this works — manual test steps, key scenarios to check>

## Deferred
<one line per marker this branch added: [Serious|Questionable] file:line — ceiling/trigger — #issue or "not filed" (carry the severity tag straight from fix.md's own report when it exists; the grep fallback can't recover severity, so mark those lines "severity unknown — fix report missing" instead of guessing)>
<omit this whole section when the branch added no markers>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

## Update the Plan

After the PR is created, update the plan artifact if it exists:

```bash
BRANCH=$(git rev-parse --abbrev-ref HEAD)
GIT_ROOT=$(git rev-parse --show-toplevel)
PLAN="$GIT_ROOT/.claude/sdlc/$BRANCH/plan.md"
```

If `$PLAN` exists, append:

```markdown
## Shipped
PR #<number> — YYYY-MM-DD
<diff stats from `git diff --stat origin/main...HEAD` — e.g. "+342/-28 across 6 files">
```

If the PR closes an issue, add `Closes #XXX` on the next line. Commit the updated plan.md and push.

## Merge

When `--merge` is passed or the user says "merge it":

1. Confirm CI checks are green: `gh pr checks <number>`
2. Remove the worktree first (from the repo root, not from inside the worktree):
   ```bash
   git -C <repo-root> worktree remove <worktree-path>
   ```
3. Squash merge and delete the branch:
   ```bash
   gh pr merge <number> --squash --delete-branch
   ```

4. Confirm the merge landed: `gh pr view <number> --json state,mergeCommit` shows `MERGED`.

Post-merge verification is a consumer-repo concern, not generic sdlc's: repos that run a deploy + verify loop (e.g. a `verify-deploy` script, a confirmation window, Slack reporting) own that in their own `.claude/skills/sdlc/SKILL.md` overlay. Generic sdlc ships the merge and stops.

## Rules

- Never force-push to main.
- Never merge without green CI.
- Always squash merge — keeps main history clean.
- Remove worktree before merge so `--delete-branch` can clean up both local and remote.
- If CI fails, investigate and fix.

## Never bypass hooks

`--no-verify`, `--no-gpg-sign`, and equivalent hook-bypass flags are forbidden. No exceptions. This includes pre-commit, commit-msg, pre-push, and pre-receive hooks. If a hook blocks your push, you have three legitimate options and only three:

1. **Fix the underlying failure.** The hook is telling you something is wrong. Read the failure, fix it, re-push.
2. **Use the hook's documented escape hatch.** Read the hook script (check `.git/hooks/` AND `core.hookspath` from `git config`, since projects often relocate hooks to a `.githooks/` directory). Hooks that ship with escape hatches document them via environment variables (e.g. `SKIP_AFFECTED_TESTS=1`). Use the documented mechanism — it leaves an auditable trail.
3. **Ask the user.** If you believe the hook is genuinely broken or the failure is a known pre-existing flake, stop and surface it. Do not decide unilaterally to bypass.

Never fabricate a justification for `--no-verify`. If your only argument for bypass is "the tests are pre-existing flakes" or "the hook is destructive," you must (a) confirm by reading the hook script (not by inference from behavior) and (b) surface to the user before pushing. A plausible-sounding post-hoc rationale is not a substitute for a real fix.

**Detection:** any hook-bypass flag in a commit or push command should be surfaced by whatever wraps the shipping loop (crew, CI review, PR comment lint). The rule is only as good as its enforcement.
