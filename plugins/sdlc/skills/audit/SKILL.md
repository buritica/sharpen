---
name: audit
description: "Read a repo's existing .github/workflows/, enforcement posture, and local setup; report drift against the SDLC spec as a severity table. Read-only — does not write files."
---

# /sdlc:audit

Audit **this** repo's CI pipeline and enforcement posture against the
language-agnostic patterns in `${CLAUDE_PLUGIN_ROOT}/templates/spec.md`. This
command is **read-only** — it reports drift; it does not fix anything. To apply
fixes, run `/sdlc:init` when offered at the end.

The most important thing this audit does that a plain CI lint does not: it checks
whether **enforcement is actually authoritative** (a required `ci-pass` check)
or has silently fallen back to git hooks. That gap is what it exists to catch.

## 0. Parse arguments

From `$ARGUMENTS`:
- `--worktree <path>` (alias `--path <path>`) → resolve all paths relative to
  that worktree root instead of cwd. Strip the flag before further processing.

Set `ROOT` to the worktree path if provided, otherwise `ROOT="."`.

## 1. Read the spec

Before inspecting the repo, read the reference patterns so every finding maps to
a specific rule:

```
${CLAUDE_PLUGIN_ROOT}/templates/spec.md
```

Hold the patterns in context; each finding below cites its pattern number.

## 2. Discover what exists

Collect the raw material — do not interpret yet:

```bash
# Workflow files
ls "$ROOT/.github/workflows/" 2>/dev/null || echo "(no .github/workflows/ directory)"

# Runtime evidence — pattern 8 is gated on this; without it, a service with no
# deploy pipeline is indistinguishable from a library that needs none.
ls "$ROOT/ecosystem.config.js" "$ROOT/fly.toml" "$ROOT/render.yaml" \
   "$ROOT/railway.json" "$ROOT/heroku.yml" "$ROOT/.do/app.yaml" "$ROOT/Procfile" \
   "$ROOT/vercel.json" "$ROOT/netlify.toml" "$ROOT/Dockerfile" 2>/dev/null
find "$ROOT" -name '*.service' -not -path '*/.git/*' 2>/dev/null | head
ls -d "$ROOT/k8s" "$ROOT/helm" 2>/dev/null
ls "$ROOT/.github/workflows/"deploy*.yml 2>/dev/null || echo "(no deploy workflow)"

# Dependabot
ls "$ROOT/.github/dependabot.yml" "$ROOT/.github/dependabot.yaml" 2>/dev/null || echo "(no dependabot config)"

# Git hooks
ls "$ROOT/.git/hooks/" 2>/dev/null
ls "$ROOT/.githooks/" 2>/dev/null
git -C "$ROOT" config core.hooksPath 2>/dev/null || echo "(core.hooksPath not set)"

# EditorConfig
ls "$ROOT/.editorconfig" 2>/dev/null || echo "(no .editorconfig)"

# Secrets wiring (pattern 7) — the .env.op template /sdlc:secrets scaffolds,
# and whether CI resolves secrets via op:// references at all.
ls "$ROOT/.env.op"* "$ROOT/.env.example" 2>/dev/null || echo "(no .env.op template)"
grep -rl 'op://' "$ROOT/.github/workflows/" 2>/dev/null || echo "(no op:// references in CI)"

# Branch protection (enforcement posture). Derive owner/repo from the remote so
# this works against --worktree paths (gh has no -C; it reads cwd's remote).
OWNER_REPO=$(git -C "$ROOT" remote get-url origin 2>/dev/null | sed 's|.*github.com[:/]||;s|\.git$||')
DEFAULT_BRANCH=$(gh repo view "$OWNER_REPO" --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo main)
gh api "repos/$OWNER_REPO/branches/$DEFAULT_BRANCH/protection/required_status_checks" 2>/dev/null \
  || echo "(no branch protection / no required status checks / no admin)"

# Stack manifests (to know which checks are expected)
ls "$ROOT/package.json" "$ROOT/bun.lock"* "$ROOT/pnpm-lock.yaml" \
   "$ROOT/pyproject.toml" "$ROOT/go.mod" "$ROOT/Cargo.toml" \
   "$ROOT/Gemfile" "$ROOT/pom.xml" "$ROOT/build.gradle"* 2>/dev/null
# Ansible / IaC the gate linters often miss:
ls "$ROOT/playbook"*.yml "$ROOT/site.yml" "$ROOT/ansible.cfg" "$ROOT/.yamllint"* 2>/dev/null
```

Read every file under `.github/workflows/` in full. Read `.github/dependabot.yml`
and any `.githooks/*` / `pre-commit` if present.

## 3. Evaluate against patterns

For each check below, determine PASS / WARN / FAIL and collect a one-line reason.

### Pattern 1 — PR validation workflow (`ci.yml` or equivalent)

**P1-A: Concurrency block** — top-level or per-job `concurrency:` with
`cancel-in-progress: true`.
- PASS: present with `cancel-in-progress: true`
- WARN: `concurrency` present but `cancel-in-progress` false/missing
- FAIL: no `concurrency` key → superseded pushes pile up runners

**P1-A2: Trigger scope** — `branches:` under `pull_request` filters on the PR's
*base*, so a PR stacked on any other branch skips CI and reports **clean**
(nothing blocks a merge when no checks exist for that base). Separately, `types:`
must include `edited` — that is the activity fired when a PR's base changes, and
without it a stacked PR retargeted after its parent merges keeps a green earned
against the old base.
- PASS: no `branches:` filter (or `branches: ['**']`, which is equivalent) **and**
  `types:` includes `edited`
- WARN: `branches-ignore:`, or a multi-branch allowlist (`[main, develop]`) —
  deliberate and narrower, but still blind to stacking on anything unlisted
- WARN: filter fine but `types:` omits `edited` → base-change re-runs never fire
- **FAIL: a single-branch allowlist** (`branches: [main]`) → every stacked PR is
  unvalidated and looks green

**P1-B: Least-privilege permissions** — top-level `permissions:` not granting
`write-all`.
- PASS: top-level `permissions` present, not `write-all`
- WARN: top-level `permissions: write-all`
- FAIL: no top-level `permissions` key

**P1-C: Always-runs path gate** — an always-runs first job emitting boolean
bucket flags; flag top-level `paths-ignore:` on required-check workflows.
- PASS: path gate present; downstream jobs gate on its outputs
- WARN: `paths:` filter instead of a path gate
- FAIL: `paths-ignore:` without a path gate, OR no path filtering at all

**P1-D: Jobs are path-gated** — downstream jobs reference path-gate outputs.
- PASS: at least one non-gate job references path-gate outputs
- WARN: path gate exists but no downstream job uses it
- FAIL: no path gate, no path-referencing `if:`

### Pattern 2 — Enforcement: `ci-pass` aggregator + required check

**This is the highest-signal section — it would have caught the original incident.**

**P2-A: `ci-pass` aggregator job exists** — a job that `needs:` the validation
jobs, runs `if: always()`, and fails on any dependency `failure`/`cancelled`.
- PASS: aggregator present and shaped correctly
- WARN: an aggregator exists but doesn't `needs:` all validation jobs, or lacks
  `if: always()` (will itself be skipped on a path-gated PR)
- FAIL: no aggregator → individual path-gated jobs can't be required without
  hanging docs-only PRs

**P2-B: `ci-pass` is a required status check** (the enforcement point) — from the
branch-protection query in step 2.
- PASS: default branch requires the `ci-pass` context
- WARN: branch protection requires *individual* CI jobs directly (works until a
  path-gated job is skipped, then hangs) — should require `ci-pass` instead
- **FAIL: CI workflows exist but NO required status check** → CI is advisory;
  a red or pending run does not block merge. Cite the incident pattern.

**P2-C: Enforcement is not living only in git hooks** — derived from P2-B + the
hook scan. If there are blocking git hooks (lint/test in pre-commit/pre-push) but
no required `ci-pass` check, the git hooks are the *accidental* enforcer.
- PASS: required `ci-pass` is the authoritative gate
- FAIL: hooks block locally but CI isn't required → enforcement is an accident of
  the hooks; anyone who bypasses or lacks them can merge unchecked code

### Pattern 3 — Reusable setup workflow

**P3: Setup/install/cache factored into a `workflow_call` workflow** the
validation jobs invoke (rather than each job repeating checkout + install +
cache). `/sdlc:init` writes this, so audit must check it.
- PASS: a `workflow_call` reusable setup workflow exists and validation jobs
  `uses:` it
- WARN: setup boilerplate is duplicated across jobs (no reusable workflow) — drift
  risk, but functional
- SKIP: trivial single-job CI where factoring adds nothing

### Pattern 4 — Secret scanning

**P4: Secret-scan job** on `pull_request` (gitleaks/trufflehog/detect-secrets).
- PASS: found · FAIL: missing

### Pattern 5 — Dependency hygiene

**P5-A: Dependabot for github-actions** — `package-ecosystem: github-actions`.
- PASS: present · WARN: dependabot.yml exists without it · FAIL: no dependabot.yml

**P5-B: Dependabot for the stack ecosystem** — matching `npm`/`pip`/`gomod`/etc.
- PASS: present · WARN: stack detected but ecosystem missing · SKIP: no stack manifest

### Pattern 6 — Local dev setup (feedback)

**P6-A: Git hooks configured** — active hook manager or non-sample hook files.
- PASS: at least one active · WARN: only `.sample` files · FAIL: none

**P6-B: Pre-commit auto-fix is safe** — scan any pre-commit hook for opinionated
reformatters run against an intentional style (the footgun), e.g.
`ansible-lint --fix` / `--write` reformatters on a repo with a custom
`.yamllint` or aligned comments.
- PASS: pre-commit only runs deterministic safe formatters, or no auto-fix
- WARN: an auto-fixer runs but the repo has no conflicting style config
- **FAIL: an auto-fixer reformats against the repo's own config** (e.g.
  `ansible-lint --fix` with a custom `.yamllint`) → silently inert or harmful

**P6-C: EditorConfig** — `.editorconfig` at root.
- PASS: present · WARN: absent

### Pattern 7 — Secrets

**P7: 1Password-backed secrets wiring** — delegated in depth to `/sdlc:secrets`,
but audit still reports the surface signal: a `.env.op`-style template and CI
resolving secrets via `op://` references rather than raw platform secrets for
anything beyond the deploy platform's own credential.
- PASS: `.env.op` template present and CI has at least one `op://` reference
- WARN: secrets exist (repo calls an external API/service) but are wired as
  plain GitHub Actions secrets with no 1Password layer — functional, no
  tier/blast-radius isolation
- SKIP: repo has no secrets to manage (no external API keys/tokens referenced
  anywhere in the codebase or CI)

### Pattern 8 — Deploy (skip entirely if the repo ships nothing)

Only audit this when a deploy workflow exists or the repo clearly has a runtime —
use the runtime evidence gathered in step 2. A library with no deploy workflow is
not a finding — say "no runtime; pattern 8 N/A" and move on.

**Runtime evidence but no deploy workflow at all is the most severe P8 state, not
an N/A.** A service shipping to production with no smoke check, no rollback, and
no serialization is exactly the drift this pattern exists to surface. Report it
as **FAIL: no deploy pipeline** and name the evidence that says the repo ships.

**P8-A: Rollback is guarded on the deploy step** — the rollback step's condition
must be `failure() && <deploy-step>.outcome == 'success'`.
- PASS: guarded · WARN: no rollback step but the workflow says why
- **FAIL: a bare `if: failure()` rollback** → any earlier failure (dependency
  install, secret fetch, notify) reverts a production release that never changed

**P8-B: Smoke check present and fatal** — a retried health poll after the deploy
step whose failure fails the workflow.
- PASS: present and fatal · **FAIL: absent, or its failure is swallowed** →
  a broken deploy reports green

**P8-C: Prod-tier token isolation** — a platform-managed deploy job must name its
environment (`environment: production`); a self-managed one reads the token from
the box.
- PASS: named, or self-managed by design
- **FAIL: reads a repo-scoped token of the same name as CI's** → the deploy runs
  on the CI-tier credential and tier isolation is silently gone

**P8-D: Serialized and bounded** — a concurrency group keyed to the runtime with
`cancel-in-progress: false`, plus an explicit job timeout.
- PASS: both · WARN: group present, no timeout
- **FAIL: neither** → concurrent deploys race, and a wedged run blocks the rest

**P8-E: Least privilege on the deploy job** — minimum token scope.
- PASS: explicitly scoped · WARN: inherits the repo default

### Pattern 9 — Enforcement visibility

**P9-A: Gate/lint tooling recognizes the stack** — if the repo is Ansible / a
stack `/sdlc:gate`'s linter auto-detection doesn't cover, the lint gate is a
silent no-op.
- PASS: the repo's primary stack is covered by gate auto-detection
- WARN: a secondary stack isn't covered
- **FAIL: the repo's primary lint surface (e.g. Ansible: ansible-lint + yamllint)
  isn't recognized** by gate auto-detection → "leave it to the gate" is false

**P9-B: Main-branch protection posture** — report (don't grade) whether
`sdlc-guardrails` is active for this repo (check `${CLAUDE_CONFIG_DIR:-~/.claude}/sdlc-guardrails.json`
for the repo path / `protectMainDefault`) and whether a pre-commit main-guard is
present. Surface the advisory-vs-enforced state so it's never invisible.

## 4. Build the findings table

Emit findings as a Markdown table sorted by severity (FAIL → WARN → PASS):

```
| Severity | Check | Pattern | Finding |
|----------|-------|---------|---------|
| FAIL | P2-B ci-pass required | Pattern 2 | CI exists but no required status check — merge proceeds on red |
| FAIL | P6-B Safe auto-fix | Pattern 6 | pre-commit runs `ansible-lint --fix` against custom .yamllint |
| PASS | P4 Secret scan | Pattern 4 | gitleaks job found |
…
```

After the table, a one-line **summary**: count of FAILs, WARNs, PASSes. Example:
> **3 FAIL · 2 WARN · 4 PASS** — enforcement is advisory; CI is not a required check.

## 5. Explain each FAIL and WARN

For every non-PASS finding, add a short paragraph (2–4 sentences):
1. The risk/consequence.
2. The correct pattern (cite `spec.md`).

Do NOT generate or write any YAML here. This is a report, not a fix.

## 6. Offer to fix

If any FAILs/WARNs are present, end with:

> Run `/sdlc:init` to scaffold or update the pipeline to address these findings.
> It reads existing workflows, presents diffs before overwriting, applies every
> pattern from `spec.md`, and sets the `ci-pass` required check.

If everything is PASS:

> No drift detected. The pipeline matches the spec and `ci-pass` is the required
> enforcement point.
