# SDLC spec — the canonical pipeline patterns

This is the single source of truth both `/sdlc:init` (writes toward it) and
`/sdlc:audit` (compares reality against it) read. It is language-agnostic: it
says *what the pipeline must do*, never *how to lint Python*. Stack-specific
commands (install, lint, test, build) come from `stacks.md` (or from research
when the stack isn't listed). The Node/bun files in `examples/node-bun/` are
**one concrete instantiation** of these patterns, not a template to copy blindly.

A generated pipeline is correct when it implements every pattern below using the
*idiomatic* tooling for the detected stack.

## Core principle: separate **enforcement** from **feedback**

The failure this spec exists to prevent: several layers (git hooks, CI, the gate
chain) each *re-running the same checks*, with **none designated the source of
truth** — so enforcement ends up wherever it accidentally lands (usually git
hooks), while everyone *thinks* CI or the gate is "the gate."

- **Enforcement = exactly one authoritative point**, at the boundary to shared
  state → a **required CI check** before merge. One place. This is *the* gate.
- **Feedback = as early as is cheap**, everywhere else. Pre-commit hooks and
  `/sdlc:gate` are *echoes* of CI for fast local signal — never the source of
  truth. They may be skipped, bypassed, or absent; merge safety must not depend
  on them.

Once split, "should it be a hook or CI?" stops being either/or: **CI enforces,
hooks and the gate echo early.**

## Cadence model: cost × trigger-frequency

Rule: **trigger frequency (commit ≫ push ≫ PR) must be inversely proportional to
check cost.** Run a check at the earliest stage where it's fast enough for that
trigger's frequency and has the inputs it needs; *enforce* at the one boundary
before shared state; *echo* earlier only when cheap.

| Check tier | Cost | Runs at | Role |
|---|---|---|---|
| Format / deterministic safe auto-fix | ms | pre-commit, **changed files only** | echo + fix |
| Fast lint, secret scan on staged | <1s | pre-commit, changed files | echo |
| Project lint / typecheck / syntax | secs | CI | **authoritative (required)** |
| Unit tests | secs–min | CI (+ pre-push *only if* worth catching before push) | authoritative |
| Integration / e2e / slow | min+ | CI | authoritative |
| Human judgment (review, imagine, simplify) | — | `/sdlc:gate`, manual | pre-PR pass |

Two consequences baked into `init`:

1. **"Commit too often → wasteful" only bites if slow or whole-tree checks sit at
   pre-commit.** Scope pre-commit to *changed files* and keep it sub-second →
   committing often means tiny diffs means tiny runs. Frequency stops being the
   enemy.
2. **pre-push is the squeezed middle.** Its only legitimate tier is "too slow for
   every commit, but you want it before push" (e.g. a full test suite). **When
   every check in the repo is fast, that tier is empty → don't scaffold pre-push.**
   The two surfaces that matter are fast pre-commit (echo) + required CI (enforce).

**Auto-fix safety rule:** auto-fix only deterministic, fast, *safe* transforms
(e.g. whitespace, import sorting a formatter owns). **Never wire opinionated
reformatting that fights an intentional style** — e.g. `ansible-lint --fix`
*always* reformats YAML and cannot be scoped to spare intentional comment
alignment. Auto-fix that fights an intentional style is a footgun; leave it out.

## 1. PR validation workflow (`ci.yml`)

- **Triggers on `pull_request` with no `branches:` filter.** That key filters on
  the PR's *base* branch, so the intuitive `branches: [main]` skips CI entirely
  for any PR stacked on another branch — and GitHub then reports that PR as
  **clean**, because "clean" means nothing is blocking the merge, and with zero
  checks configured for that base there is nothing to block. Unvalidated is
  indistinguishable from passing, in the UI and in `gh pr view`. Omit the filter
  so every PR is validated regardless of what it targets; the extra runner time
  is cheaper than one false green.
  - **Include `edited` in `types:`.** The default activity set
    (`opened`/`synchronize`/`reopened`) omits it, and `edited` is what fires
    when a PR's **base changes**. Without it, a stacked PR that GitHub
    auto-retargets after its parent merges keeps the green it earned against
    the old base — including a path gate computed by diffing against a branch
    that no longer exists. A PR can be docs-only against its parent and
    code-heavy against the default branch, so that stale green can be
    *structurally* wrong, not merely old. `edited` also fires on title/body
    edits; the wasted runs are the price of a required check that means what it
    says.
  - **A green check on a non-default base is advisory, not enforced.** Branch
    protection applies to the default branch only (pattern 2). Before this
    change a stacked PR showed *zero* checks, which at least looked unenforced;
    now it shows a green `ci-pass` under the same context name as the required
    one. Better — a real run beats no run — but say so in review, because
    `mergeStateStatus` reads `CLEAN` either way.
- **Concurrency:** `group: ci-${{ github.ref }}`, `cancel-in-progress: true` —
  superseded pushes don't pile up runners.
- **Permissions:** top-level `permissions: { contents: read }`; elevate per-job
  only where a job needs more.
- **Always-runs path gate:** a first job (e.g. dorny/paths-filter for GitHub, or
  the platform equivalent) classifies the diff into buckets and exposes one
  boolean per bucket. Every downstream job gates on its bucket. This both makes
  CI run only what's relevant AND keeps a *required* check satisfiable on
  docs-only PRs (unlike top-level `paths-ignore`, which hangs required checks).
  - **Private repos need `pull-requests: read` on this job.** On a
    `pull_request` trigger, `dorny/paths-filter` can skip `actions/checkout`
    entirely and list the PR's changed files via the GitHub API instead — do
    that; it's leaner. That API call needs `pull-requests: read`, which the
    workflow-level `contents: read` does not cover once the repo is private,
    and the job fails with "Resource not accessible by integration" on every
    PR. Add `permissions: { pull-requests: read }` at the job level. Public
    repos don't hit this, which is exactly why it goes unnoticed until a repo
    flips private. Job-level `permissions` *replaces* the workflow-level
    block rather than merging with it — if this job ever needs a real
    checkout, add `contents: read` back explicitly alongside it.
  - **Exclusion-only filters need `predicate-quantifier: 'every'`.** A filter
    expressed purely as negated globs (`'!**/*.md'`, `'!web/**'`, …) defaults
    to matching if *any* negated pattern doesn't match a changed file — so a
    root-level `.md` file still matches `'!web/**'` and wrongly flips a
    docs-only PR's `code` bucket to `true`. Set
    `predicate-quantifier: 'every'` on the `paths-filter` step whenever a
    filter is exclusion-only.
- **Validation jobs:** one per check the stack supports — typically
  format-check, lint, typecheck/static-analysis, test, build. Wire only the
  checks the repo actually has. **For compiled languages a build job is
  mandatory** — a repo can be fmt/lint/test-clean and still fail to compile
  (see `stacks.md` → "Compiled languages"). Heavy, path-scoped suites (web e2e,
  a `db/` or `infra/` area) get their own bucket + gated job.

## 2. The `ci-pass` aggregator + required branch protection (enforcement)

This pattern is what makes CI **the** authoritative enforcement point. Without
it, "CI" is advisory — a red or pending run does not block merge — and
enforcement silently falls back to git hooks.

- **Aggregator job.** Add one `ci-pass` job that `needs:` every validation job,
  runs `if: always()`, and **fails if any dependency failed or was cancelled**:

  ```yaml
  ci-pass:
    needs: [changes, lint, typecheck, test, build]   # the gate job + every validation job
    if: always()
    runs-on: ubuntu-latest
    steps:
      - name: Verify all required jobs passed
        run: |
          results='${{ join(needs.*.result, ' ') }}'
          for r in $results; do
            if [ "$r" != "success" ] && [ "$r" != "skipped" ]; then
              echo "A required job did not pass: $results"; exit 1
            fi
          done
  ```

  Because validation jobs are **path-gated**, marking them required directly
  would hang a docs-only PR forever (the skipped job never reports). `ci-pass`
  treats `skipped` as acceptable but `failure`/`cancelled` as fatal, so it is
  always satisfiable yet never green on a real failure. **One required gate, no
  deadlock.**

  The gate job (`changes`) itself must be in this `needs:` list, not just the
  jobs it gates. If it's omitted, a failure in the gate job makes every
  downstream job report `skipped` (their `if` conditions never evaluate true),
  and `ci-pass` treats skipped as fine — a broken gate silently collapses to a
  green required check with nothing actually validated.

- **Branch protection.** The default branch must require the `ci-pass` status
  check (and nothing else from CI). `/sdlc:init` sets this by default via
  `gh api`; when it can't (no admin), it prints the exact command and flags it.
  `/sdlc:audit` reports **FAIL** when CI exists but `ci-pass` is not required —
  this is the check that would have caught the original incident.

## 3. Reusable setup workflow

Factor the install/cache/runner boilerplate into one `workflow_call` workflow the
validation jobs invoke with a `command` input. It must:

- Check out the repo.
- Install the stack's toolchain (the right setup-* action / version manager).
- Cache dependencies using the stack's cache path + lockfile hash as the key.
- On self-hosted runners, skip install-cache and instead clean untracked cruft
  so stale artifacts can't mask a failure.
- Run the passed `command`.

## 4. Secret scanning (universal)

A `pull_request` job that runs a secret scanner (gitleaks) over the diff. Same
for every language — see `workflows/secret-scan.yml`.

## 5. Dependency hygiene (universal)

`.github/dependabot.yml` for the `github-actions` ecosystem (keeps pinned action
versions current). Add the stack's package ecosystem too when the repo wants
dependency PRs (e.g. `npm`, `pip`, `gomod`, `cargo`).

## 6. Local dev setup (feedback only — echoes CI, never enforces)

- **Formatter/linter:** if the repo has none, set up the stack's idiomatic one
  (see `stacks.md`) with config + scripts. If it already has one, leave it.
- **`.editorconfig`** for editor-level consistency (universal — `editorconfig`).
- **Git hooks — pre-commit only by default.** `pre-commit` runs **scoped to
  changed/staged files**: deterministic safe auto-format (per the safety rule
  above) + fast lint/secret-scan echo, and blocks direct commits to the default
  branch. Keep it sub-second. **Only scaffold `pre-push` when the repo has a
  genuinely slow check** (e.g. a full test suite) worth catching before push;
  when every check is fast, omit pre-push entirely (the squeezed-middle tier is
  empty). Wire via the stack's idiomatic hook-install path (a `prepare` script
  setting `core.hooksPath`, or lefthook/pre-commit framework if already present).
- These are *echoes*. CI (pattern 2) remains the authoritative gate; nothing here
  may be the only thing standing between bad code and `main`.

## 7. Secrets (universal)

Handled by `/sdlc:secrets` — 1Password Environments, one read-only service
account per tier. Independent of language.

## 8. Deploy (only when the repo ships something)

Not universal — skip entirely for libraries and repos with no runtime. When the
repo *does* deploy, the enforcement principle at the top of this file extends one
step: **CI gates the merge, deploy ships the merge, and deploy must be able to
undo itself without a human.** A pipeline that can only go forward turns a bad
merge into an outage.

Deploy is **not** a required check. It triggers on push to the default branch —
i.e. after `ci-pass` has already enforced — never on `pull_request`.

**Derive the runtime-ownership model; never assume a host.** Two shapes, and the
repo's own files say which:

| | Self-managed runtime | Platform-managed runtime |
|---|---|---|
| Signal | process-manager config (`ecosystem.config.js`, a systemd unit, a supervisor config), or a self-hosted runner label already in `ci.yml` | a platform manifest (`fly.toml`, `render.yaml`, `railway.json`, `.do/app.yaml`, `Procfile`) |
| Restart | the process manager, **via its config file** | the platform's deploy CLI |
| Rollback | a previous-release snapshot, **only if the manager actually has one** | the platform's release-rollback command |
| Prod token | on the box, out of the CI provider's reach | the CI provider's protected production environment |

When the platform isn't one already known, **research its current deploy and
rollback commands from its official docs before generating anything**, and state
which doc was consulted. A guessed CLI flag is a broken deploy.

**Not every process manager has a rollback.** systemd and supervisord have no
release concept and retain nothing; PM2's `pm2 revert` belongs to its `pm2 deploy`
subsystem and does nothing for a checkout-and-reload workflow. Verify the
mechanism exists before writing invariant 4 against it — **inventing a plausible
`systemctl` incantation is worse than having no rollback**, because the runbook
then claims a recovery path that has never run. When there is no real rollback,
say so in the workflow and in the report, and make redeploying a known-good ref
the documented path.

### Invariants — both models

1. **Skip escape, evaluated first.** A commit-message opt-out (`[skip deploy]`)
   checked before backup, restart, or notify can run — the cheapest way to land a
   change on the default branch without shipping it.
2. **Back up before mutating, unconditionally.** Durable state gets a backup step
   *every deploy*, not only on deploys that look like they carry a migration —
   the deploy that corrupts data is usually the one where a code bug writes
   garbage, which no code rollback undoes. Prune old backups on a retention
   window in the same step; unbounded dumps fill the disk and take the database
   and the runner down together. **Ask for the retention window, the dump
   command, and the backup location — never invent them.** A guessed
   `find -delete` against a production backup directory looks deliberate and
   deletes real recovery points; "I don't know your retention policy" is the
   correct output when nobody has stated one.
3. **Smoke check: retried, and fatal.** Poll a health endpoint with bounded
   retries, connection-refused tolerance, and a per-attempt timeout — a process
   up but not yet listening is not a failure; one that never listens is. Non-2xx
   or exhausted retries **fail the workflow**. Budget for the distance: a
   localhost check needs a few short retries, a remote platform URL roughly
   double, because a cold start is slower than a restart. **If the service has no
   health endpoint, add one or agree an endpoint before generating** — a smoke
   check pointed at a 404 fails every deploy, and by invariant 4 it then rolls
   back every deploy, forever.
4. **Rollback fires on smoke failure, guarded on the deploy having succeeded,
   then re-verifies.** The condition is `if: failure() && <deploy-step>.outcome ==
   'success'` — **never a bare `if: failure()`**. Bare `failure()` is true when
   *any* earlier step failed, including a dependency install, a secret fetch, or
   a Slack notify with a stale token: nothing was deployed, production is
   healthy, and the workflow reverts it anyway. That is a self-inflicted outage
   caused by the safety mechanism. After rolling back, run the smoke check again
   — a rollback nobody verified is just a second unverified deploy — and when the
   re-verify *also* fails, emit an explicit error annotation demanding a human.
   That is the one state where someone must be woken up.
5. **Prod-tier secrets, never the CI tier.** The deploy job reads the prod
   Environment's read-only service account (pattern 7) — never the token CI uses.
   On a platform-managed runtime this requires the job to **name the environment**
   (`environment: production`); without that key the job silently resolves the
   *repo-scoped CI token* of the same name instead, the deploy appears to work,
   and the tier isolation is gone with no error. On a self-managed runtime the
   token lives on the box, so there is no environment to name — say which case
   applies in the generated file.
6. **Mask resolved secret values, and persist them deliberately.** Values
   resolved from `op://` at deploy time must be masked in the log before use
   (`::add-mask::`), or they appear verbatim in any step that dumps the
   environment. Step-level env does not survive into the next step: write
   resolved values to the job's environment file, or later steps get an empty
   string — which `pg_dump` will happily read as "connect to localhost".
7. **Notify on start, success, and failure/rollback.** Deploy is the one workflow
   whose outcome someone needs to learn without asking.
8. **Serialize, and bound the run.** A concurrency group keyed to the **target
   runtime** — not the git ref — with `cancel-in-progress: false`, so two deploys
   never race one runtime. Note what that actually does: the provider holds one
   pending run per group and a third arrival *cancels the pending one*, so
   intermediate commits are skipped and the newest wins. That's usually what you
   want; it is not a FIFO queue, and someone will otherwise lose an hour to a run
   that shows "cancelled" with no explanation. The group key must name the
   *destination* — repo plus service plus environment, whatever distinguishes one
   runtime from another — so two repos deploying to one box still serialize. Set
   an explicit job timeout sized to a normal deploy plus headroom: the default is
   hours, and one wedged restart holding a self-hosted runner blocks every later
   deploy behind this very concurrency group.
9. **Least privilege.** The deploy job holds a production credential and runs on
   the default branch: give it the minimum provider token scope (`contents: read`
   unless a step demonstrably needs more). Least privilege matters *more* here
   than in `ci.yml`, not less.

### Additional invariants — self-managed runtime only

10. **Name the runner.** The deploy job must run *on the machine it deploys to* —
    the self-hosted runner label, not the provider's default hosted image. This
    is the invariant whose absence fails most confusingly: on a hosted runner the
    restart command is simply absent, or worse, a port-hygiene fallback
    "succeeds" against a throwaway container and the workflow reports green while
    production is untouched.
11. **Port hygiene, applied narrowly.** If the service's port is still held when
    the restart runs, the restart fails with a bind error that reads like a code
    bug. A *crashed* process holds nothing — the kernel reclaims the socket — so
    the real causes are a wedged-but-alive process, an orphaned child that
    inherited the listening descriptor, or `TIME_WAIT` without address reuse.
    Prefer the process manager's own reload, which hands the socket over without
    a gap. Only fall back to killing whatever holds the port when the manager
    can't do that, and only against a port derived with certainty: an
    unconditional kill turns a zero-downtime reload into a downtime window on
    every deploy, and a mis-derived port kills an unrelated service on a shared
    box.
12. **Restart through the config file, not by process name**, so environment
    baked into that config is actually applied.
13. **Persist the process list after restart and after rollback.** A manager that
    resurrects from a saved list on reboot will otherwise come back running
    whatever version was current the last time someone saved — correct until the
    box reboots, which is exactly when nobody is thinking about it.

**Rollback has a floor, and the scaffold must say so.** Where a snapshot mechanism
exists at all it typically keeps exactly one, so a second consecutive bad deploy
has nothing to roll back to; the recovery path is deploying a known-good ref.
Whoever is on call should learn that limit while reading the generated workflow,
not at 3am.

## 9. Enforcement visibility (audited, not scaffolded)

`/sdlc:audit` reports the *enforcement posture* so advisory-vs-enforced is never
invisible:

- Is `ci-pass` a **required** check on the default branch? (pattern 2)
- Does the `pull_request` trigger carry a **base-branch filter**, or omit `edited`
  from `types:`? (pattern 1) — either one makes a stacked PR report green while
  unvalidated, which is the failure mode hardest to see from the outside.
- Is enforcement living **only** in git hooks (no required CI)? — the accidental-
  enforcer smell.
- Does any pre-commit fixer **fight the repo's own config** (the auto-fix footgun)?
- Does the gate/lint tooling **recognize this repo's stack** (e.g. Ansible)?
- Is `sdlc-guardrails` active for this repo, and is the pre-commit main-guard
  present? (the two main-branch-protection layers — agent-side + shell-side)
