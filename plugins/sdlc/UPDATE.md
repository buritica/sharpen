# sdlc UPDATE — v4.16.0

`/sdlc:init` reads this file first (step 0) to learn what this version adds and what an
existing repo may need wired up. If you are installing for the first time, there is
nothing to migrate — read "What a repo gets" below and skip the rest. Notes for older
releases move to [`CHANGELOG.md`](CHANGELOG.md) once superseded.

## New in 4.16.0 — every way to open a PR is gated, plus a git pre-push floor

The PR gate used to recognize one thing: a Bash call that ran `gh pr create`. Now one
shared detector (`scripts/pr_paths.py`) feeds the same missing-and-stale check from:

- `gh pr create` and its built-in alias `gh pr new`; `gh api` POSTs to
  `repos/<o>/<r>/pulls` and the graphql `createPullRequest` mutation; `glab mr create`;
  `hub pull-request`; `git push -o merge_request.create`.
- Claude Code's `Monitor` tool, which runs shell commands outside the `Bash` matcher.
- MCP `create_pull_request` / `create_merge_request` tools on Claude Code and Codex (a
  second `PreToolUse` matcher in `hooks/hooks.json`) and on pi (the `tool_call` handler
  now passes those tools through, not only bash).

Three bypasses inside the old path are closed: `gh pr new`, a head the shell expands
(`--head "$(git branch --show-current)"`, `$B`, xargs' `{}`), and `--head HEAD`/`@`. Each
used to read as an unknown branch, so "no cycle, allow". They now resolve to the branch
checked out in the working directory. When the session's cwd isn't the PR's checkout (an
MCP call, `gh pr create -R owner/repo`, a cloud session sitting above several repos), the
hook finds the checkout whose remote matches instead of missing the store. If no remote
matches and the cwd is itself a checkout, that checkout is judged, so a `-R` spelling
never turns into "allow" from inside a gated repo. An MCP PR for
a branch with no cycle is still allowed, as before, but now says so: a branch pushed only
through MCP never had a local commit to arm one.

**The git pre-push floor.** `/sdlc:gate` now runs `scripts/install-pre-push.py`, which
puts a small path-free shim in the repo's common hooks dir (every worktree gets it) and
records the plugin's scripts dir in `git config sharpen.sdlcScripts`. On each push it runs
the same check for every pushed branch that has a cycle. This catches what no tool hook
sees: `make pr`, scripts, gh aliases, `curl`, instructions-only hosts, and a push followed
by the web UI. It **warns by default**. `git config sharpen.prePush block` makes it refuse
the push, with `SDLC_ALLOW_PUSH=1 git push` as the one-shot escape. `off` silences it. A
corrupt gate store or an error in the script lets the push through with a message, even in
block mode. It never overwrites another hook. If husky, lefthook or `core.hooksPath` own
your hooks, it prints one line to add to theirs, and that line exits 0 on a machine without
sdlc configured, so it is safe in a committed `.githooks/`.

**`/sdlc:ship` no longer commits after the chain.** The up-to-date check now says to
rebase before `/sdlc:gate`, and the post-PR step that committed `plan.md` (a gitignored
file) is gone, since any commit after the chain makes the review gates stale.

**What an existing repo needs to do:** nothing for the tool hooks. They arrive with the
plugin update. The pre-push shim is installed the next time `/sdlc:gate` initializes a
cycle, or right away with `python3 <sdlc>/scripts/install-pre-push.py`. Delete
`.git/hooks/pre-push` to remove it.

## What a repo gets

`/sdlc:init` scaffolds, and `/sdlc:audit` grades, against
[`templates/spec.md`](templates/spec.md):

- **PR validation** with a path gate, concurrency, and least-privilege permissions.
- **`ci-pass`** — the single aggregator job that branch protection requires. This is the
  one authoritative enforcement point; marking path-gated jobs required directly would
  hang a docs-only PR forever.
- **A deploy workflow** (pattern 8) when the repo ships a runtime — derived from the
  repo's own files, on the self-managed vs platform-managed axis. Reversible by
  construction: a retried smoke check, and a rollback guarded on the deploy step actually
  having succeeded.
- **Secret scanning, dependabot, and changed-files pre-commit hooks** as fast local echoes
  — never the enforcement point.
- **Three-tier secrets** via `/sdlc:secrets`, one read-only service account per tier, so a
  leaked CI token cannot reach production.

## Re-running on a repo that already has sdlc scaffolding

`init` is idempotent: it reads what exists, diffs against the spec, and offers to merge in
only what's missing. It does not overwrite hand-tuned steps.

Run `/sdlc:audit` first to see the gap as a severity table. The finding most likely to be
present in a hand-written deploy workflow is **P8-A**: a rollback step conditioned on a
bare `if: failure()` rather than `failure() && <deploy-step>.outcome == 'success'`. Bare
`failure()` fires when *any* earlier step failed — a dependency install, a secret fetch, a
notification with a stale token — so the workflow reverts a production release that was
never touched. Fix that one first.

## Gate cycles

The chain records to `.sharpen/data/gates.json` (an existing install that still only has
`.claude/data/gates.json` keeps using that file until the newer path exists), keyed by
branch and shared across every worktree of the repo. Two things worth knowing before you
run it:

- **Gates 2–6 are skill-gated.** They are recorded only when their skill actually runs.
  `record-gate.py --record grumpy-review` is refused by the hook and again by the store.
  If `grumpy` is not installed, a `small-medium` cycle cannot complete — `/sdlc:gate` says
  so before it initializes rather than after you've done the work.
- **A reset clears the skill-gated gates too**, and they can only be re-earned by running
  their skills again. That is correct — a gate that passed against different code proved
  nothing — but it means "just re-init" is not a cheap reflex. Batch your fixes and reset
  once.
