---
name: init
description: "Scaffold a repo's CI and deploy pipeline tailored to its stack — PR validation, path-gated jobs, the ci-pass required-check enforcement, changed-files pre-commit hooks, and a reversible deploy workflow (smoke check + rollback) when the repo ships a runtime. Derives the idiomatic toolchain and the runtime-ownership model by reading the repo; assumes no language and no host. Idempotent."
---

# /sdlc:init

Scaffold the CI pipeline for **this** repo. CI is the *automated enforcement
surface* of the SDLC loop (plan → build → gate → ship → CI → deploy): it is the
**one authoritative point** that blocks bad code from reaching `main`. The job is
to understand the codebase, derive the idiomatic toolchain for its stack(s), and
instantiate the language-agnostic patterns — **not** to drop a fixed template.
There is no default language.

Read these first; they are the spec:
- `${CLAUDE_PLUGIN_ROOT}/templates/spec.md` — what every generated pipeline must
  do, including the enforcement/feedback split and the `ci-pass` required-check
  pattern.
- `${CLAUDE_PLUGIN_ROOT}/templates/stacks.md` — toolchain reference + the
  research protocol for stacks not listed.

The files under `${CLAUDE_PLUGIN_ROOT}/templates/examples/node-bun/` are **one
worked example** (bun/biome). Reuse them only if this repo actually is a
bun/Node repo; otherwise generate the stack-correct equivalent.

## 0. Read UPDATE.md

Before anything else, read `${CLAUDE_PLUGIN_ROOT}/UPDATE.md`. It describes what this version of sdlc adds and what an existing repo may need to upgrade. Use it to understand what new things to wire up if this repo already has sdlc scaffolding.

## 0b. Check grumpy availability

Detect grumpy by **command availability** (same rule as gate.md — do NOT probe
the filesystem): `/grumpy:review`, `/grumpy:fix`, and `/grumpy:imagine` are
listed in your available skills/commands for this session if and only if the
grumpy plugin is installed.

- **All three present** → grumpy is available; gates 3–6 will run real reviews.
  State: `✓ grumpy detected — full gate quality enabled`.
- **Any missing** → warn loudly before proceeding:

  ```
  ⚠️  grumpy not installed. Gates 3–6 (review, fix, imagine) will fall back to
  self-review, which is significantly weaker. Install grumpy first:

      claude plugin install grumpy@sharpen

  Then restart your session and re-run /sdlc:init.
  ```

  Stop here unless the user explicitly confirms they want to continue without
  grumpy. Do not silently proceed.

## 1. Parse arguments

From `$ARGUMENTS`:
- `--self-hosted <label>` → heavy jobs (test, and any build/e2e) target
  `'["self-hosted", "<label>"]'` instead of the hosted default.
- `--minimal` → PR validation + reusable setup + local hooks only. Skip
  secret-scan, dependabot, **and the deploy workflow (step 9)**.
- `--no-protect` → skip the branch-protection step (still write the `ci-pass`
  aggregator job). Use for solo/scratch repos or where you lack admin.
- `--deploy <target>` → the repo deploys to `<target>` (e.g. `fly`, `render`, or
  a self-hosted box). Authoritative for step 9: skip runtime *detection*, but
  still determine which **ownership model** the target implies (a platform name →
  platform-managed; a box or process manager → self-managed), because that choice
  selects which invariants apply. Needed for brand-new repos, where no manifest
  exists yet — `/sdlc:new` forwards it.
- **Precedence:** `--minimal` wins over `--deploy`. If both are passed, skip step
  9 and **say so explicitly** — silently discarding an explicit `--deploy` is the
  one outcome that must not happen.

## 2. Identify the stack(s)

Scan for manifests — do not stop at the first hit; repos are often polyglot or
monorepos:

```bash
ls package.json bun.lock* pnpm-lock.yaml package-lock.json yarn.lock \
   pyproject.toml uv.lock poetry.lock requirements*.txt setup.py \
   go.mod Cargo.toml Gemfile pom.xml build.gradle* *.csproj *.sln mix.exs composer.json \
   2>/dev/null
# House tooling that names the real commands:
ls Makefile Justfile Taskfile* 2>/dev/null
ls .github/workflows/ 2>/dev/null
# Monorepo orchestrators:
ls turbo.json nx.json moon.yml WORKSPACE* pnpm-workspace.yaml 2>/dev/null
```

Also read existing CI, `CONTRIBUTING*`, and the manifest's script section — they
usually name the exact tools the maintainers already use.

## 3. Derive the toolchain (research if needed)

For each detected stack, determine the idiomatic **format / lint / typecheck /
test / build / install+cache / setup-action** using `stacks.md`. Then:

- **Reconcile with the repo.** If the repo already uses specific tools (its
  scripts, existing CI, Makefile targets), match those — don't impose an opinion.
- **Research when the stack isn't in `stacks.md`, or the reference looks dated,
  or you're unsure.** Use WebSearch/WebFetch to confirm the *current* idiomatic
  tooling and a *maintained* setup action for that ecosystem (verify currency —
  actions get deprecated). Prefer official docs / the action's README; note the
  version you'll pin.
- **Monorepo:** prefer per-area path buckets + gated jobs; if a monorepo tool
  (turbo/nx/moon/bazel) is present, wire CI around its affected-detection.

**State the derivation before writing anything**, e.g.:
> Detected: Python (uv) + a `web/` Node area. Chose ruff (format+lint), pyright
> (typecheck), pytest, `astral-sh/setup-uv`; web uses pnpm + biome. Basis: repo
> already has `ruff` in pyproject and a `web/package.json`.

Let the user correct it.

## 4. Apply the CI patterns

Instantiate `spec.md` with the derived toolchain into `.github/workflows/`.
Generate, don't blind-copy — the bun example is a shape reference:

- **`ci.yml`** (pattern 1) — PR validation with concurrency, least-privilege
  `permissions`, the always-runs path gate, and one gated job per check the
  stack supports. Drop checks the repo lacks. Apply `--self-hosted` to heavy
  jobs. Tune path buckets to the repo's layout (per `spec.md`).
- **`ci-pass` aggregator** (pattern 2) — **always add this job.** It `needs:`
  the path gate job *and* every validation job — omitting the gate job from
  `needs:` means a gate failure makes everything downstream report `skipped`,
  which `ci-pass` treats as OK, silently disabling enforcement. Runs
  `if: always()`, and fails if any dependency failed/was cancelled (treating
  `skipped` as OK so path-gated jobs don't hang a docs-only PR). This is the
  single status check that branch protection will require — see step 6. Copy
  the shape from `spec.md` pattern 2.
- **reusable setup workflow** (pattern 3) — the stack's setup action + dependency
  cache + self-hosted hygiene.

Universal pieces (copy as-is unless `--minimal`):
- `${CLAUDE_PLUGIN_ROOT}/templates/workflows/secret-scan.yml` → `.github/workflows/secret-scan.yml`.
- `${CLAUDE_PLUGIN_ROOT}/templates/dependabot.yml` → `.github/dependabot.yml`
  (add the stack's package ecosystem alongside `github-actions` if the user wants dep PRs).

When a target file already exists, Read it and present a diff before overwriting
anything hand-tuned.

## 5. Local dev setup (pattern 6 — feedback only)

Local hooks are *echoes* of CI for fast signal, never the enforcement point.
Keep them cheap and changed-files-scoped.

- **Formatter/linter:** if the repo has none, set up the stack's idiomatic one
  (biome for Node, ruff for Python, gofmt+golangci-lint for Go, etc.) with its
  config + `lint`/`format` scripts, then run it once and show the user the scope
  of changes. If one already exists, leave it.
- Copy `${CLAUDE_PLUGIN_ROOT}/templates/editorconfig` → `.editorconfig` if absent.
- **Ignore the agent scratch directories.** These commands write per-branch
  artifacts and gate state under `.claude/`; none of it belongs in git. Append
  whichever are missing from `.gitignore` (create it if absent):

  ```
  # Agent scratch — gate state, plans, specs, review reports
  .claude/data/
  .claude/sdlc/
  .claude/grumpy/
  .claude/worktrees/
  ```

  Copy that block **verbatim**. `.gitignore` has no trailing comments — `#`
  starts a comment only at the beginning of a line, so `.claude/data/  # state`
  is a literal pattern that matches nothing and silently ignores nothing.

  First check for an entry that already covers these (a blanket `.claude/`, or
  the individual lines) and skip what's present — appending on every run leaves
  a file full of duplicates. If the repo deliberately *tracks* `.claude/` for
  shared commands or `settings.json`, add only the four lines above, never a
  blanket ignore.

  **If you replace an existing agent-scratch ignore entry with these**, delete the
  directory it covered in the same step, after telling the user what is in it.
  The moment an ignore entry is gone its directory becomes untracked files that
  the next `git add -A` commits. Do not leave that window open by only
  mentioning it.
- **Git hooks — pre-commit by default, pre-push only if needed:**
  - `pre-commit`: block direct commits to the default branch + **auto-format the
    staged/changed files only** with the stack's formatter (sub-second). Apply
    the **auto-fix safety rule** from `spec.md`: only deterministic safe
    transforms — **never wire opinionated reformatting that fights an intentional
    style** (e.g. `ansible-lint --fix`). The bun example in
    `examples/node-bun/githooks/pre-commit` shows the shape.
  - `pre-push`: **scaffold this only if the repo has a genuinely slow check**
    (e.g. a full test suite) worth catching before push. If every check is fast
    (lint/typecheck/quick tests all sub-second), **omit pre-push** — the
    squeezed-middle tier is empty, and CI is the authoritative gate anyway. State
    which you chose and why.
  - Wire via the stack's idiomatic path (a `prepare` script setting
    `core.hooksPath`, or the repo's existing husky/lefthook/pre-commit framework
    — don't fight an existing setup).

> If `sdlc-guardrails` is installed, its hook already guards main commits.
> Mention it and offer to drop the `pre-commit` main-guard to avoid double-guarding.

## 6. Enforcement: require `ci-pass` on the default branch (pattern 2)

This is the step that makes CI authoritative — skipping it reproduces the exact
gap this tooling exists to close (CI green/red not blocking merge). Unless
`--no-protect` was passed, set the default branch to require **only** the
`ci-pass` status check:

```bash
OWNER_REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)
DEFAULT_BRANCH=$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name)
gh api -X PUT "repos/$OWNER_REPO/branches/$DEFAULT_BRANCH/protection" \
  --input - <<'JSON'
{
  "required_status_checks": { "strict": true, "checks": [ { "context": "ci-pass" } ] },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null
}
JSON
```

If the call fails (no admin on the repo, or the org restricts protection), **do
not silently continue** — print the exact command above and tell the user to run
it (or have an admin do it), and note that until `ci-pass` is required, CI is
advisory. `/sdlc:audit` will report this as a FAIL.

For a repo with **no GitHub remote yet** or a **brand-new repo with no default
branch**, `OWNER_REPO`/`DEFAULT_BRANCH` resolve empty and the `gh api` call is
meaningless — pass `--no-protect` and wire branch protection later once the repo
is pushed to GitHub.

## 7. Optional hardening

Offer to pin actions to full commit SHAs (`gh api repos/<o>/<r>/git/refs/tags/<tag> --jq .object.sha`).
Dependabot keeps them current. Opt-in — SHAs make diffs noisier.

## 8. Secrets

Tell the user to run **`/sdlc:secrets`** for the full three-tier setup
(dev / CI / prod), each with a tier-scoped service account. Never put plaintext
secrets in a file.

Two surfaces this repo likely needs:

- **Dev workstation** — for `mise run setup` / `workon <repo>`. Single shared
  `dev` vault, one item per repo (`op://dev/<repo>/<field>`), kebab-case
  fields, resolved via `~/.config/op/dev-token` (placed by whatever provisions
  your workstations). If the repo doesn't already have a `mise.toml` with a
  `setup` task, the dev-tier section of `/sdlc:secrets` shows the canonical
  shape.
- **CI** — Environment `<repo>-ci`, read-only SA, GitHub secret
  `OP_SERVICE_ACCOUNT_TOKEN`, injected via `1password/load-secrets-action`.

See the **Naming convention** table at the top of `/sdlc:secrets` — same shape
across all tiers (env → vault/Environment, repo → item, secret → field; env
never in the field name).

## 9. Deploy workflow (pattern 8 — only if the repo ships something)

Skip this step entirely under `--minimal`, and for libraries and repos with no
runtime — say which applies. If `--deploy <target>` was passed, that is
authoritative: use it and skip detection (a brand-new repo has no manifest to
detect). Otherwise **derive the runtime-ownership model** from the repo — do not
assume a host:

```bash
# Self-managed runtime? (process manager, unit file, or a self-hosted CI runner)
ls ecosystem.config.js 2>/dev/null
find . -name '*.service' -not -path './.git/*' 2>/dev/null | head
grep -rIl "pm2\|ecosystem\.config\|systemctl\|supervisor" \
  package.json Makefile Justfile .github deploy scripts 2>/dev/null
grep -l "self-hosted" .github/workflows/*.yml 2>/dev/null
# Platform-managed runtime? (a platform manifest)
ls fly.toml render.yaml railway.json heroku.yml .do/app.yaml Procfile \
   vercel.json netlify.toml serverless.yml app.yaml 2>/dev/null
# Container/orchestrator runtime? (neither column cleanly — see below)
ls Dockerfile docker-compose.yml compose.yaml Chart.yaml 2>/dev/null
ls -d k8s helm .helm deploy/k8s 2>/dev/null
# Already has a deploy workflow?
ls .github/workflows/deploy*.yml 2>/dev/null
```

**A container image is not a runtime-ownership answer.** A `Dockerfile` says how
the app is packaged, not who owns the process — the same image runs on a box you
manage and on a platform that manages it for you. When containers or an
orchestrator are the only signal, **ask** which applies rather than guessing; the
choice selects a different set of invariants, and picking wrong produces port
hygiene and config-file restarts for a runtime that has neither. Orchestrated
deploys (Kubernetes, ECS, Nomad) satisfy pattern 8 through the orchestrator's own
rollout and rollback primitives — map each invariant onto those rather than
inventing shell steps, and say which primitive covers which invariant.

**If nothing matches at all, say so and stop** — do not quietly conclude "no
runtime." State what you looked for and ask whether the repo deploys. A service
that ships to production and gets told pattern 8 doesn't apply is the expensive
failure here, and it is silent.

Announce the detected model and the evidence **before writing anything**, and let
the user correct it:

> Detected: **self-managed runtime** — `ecosystem.config.js` present, no platform
> manifest, `ci.yml` names a self-hosted runner. Service `<name>`, port `<n>`
> (from `.env.example`). Deploying via the process manager's config file.

Then resolve what the workflow needs, deriving each from the repo and asking only
when it genuinely can't be found: service name, listening port, health endpoint,
previous-release rollback command, and the notification channel.

For a platform you don't already know the current CLI for, **research it against
the platform's official docs** (WebSearch/WebFetch) before generating — state
which doc you used. A guessed deploy or rollback flag is a broken deploy.

Generate `.github/workflows/deploy.yml` satisfying **every invariant in `spec.md`
pattern 8** — read them there rather than working from memory; the spec carries
the rationale for each, and self-managed runtimes have two extra.

Never write a secret value into the generated file. Never omit the smoke check or
the rollback step — those two are what make the deploy reversible, which is the
whole point of the pattern.

When a deploy workflow already exists, Read it, diff it against the invariants,
and offer to merge in only what's missing. Do not overwrite hand-tuned steps.

Finally, state the rollback **floor**: how many previous releases the mechanism
retains (usually one), and that the recovery path past that is deploying a
known-good ref.

## 10. Wire AGENTS.md (and CLAUDE.md)

Everything this command derived — the toolchain commands, tier rules, PR
conventions, artifact paths, grumpy availability — has to live where every
agent host reads it. That is `AGENTS.md`: Codex, Gemini, Cursor and Copilot
read it directly, and Claude Code reads it through a one-line `CLAUDE.md`
that says `@AGENTS.md`. Do not hand-write the block; the plugin's script
renders it from `templates/agents-sdlc.md` and upserts it between
`<!-- sdlc:begin -->` / `<!-- sdlc:end -->` markers, so a re-run replaces
exactly its own block and nothing a human wrote:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agents_md.py" --root . \
  --test-cmd "<test command from step 3>" \
  --lint-cmd "<lint command>" \
  --format-cmd "<format command>" \
  --typecheck-cmd "<typecheck command, or omit>" \
  --default-branch "<default branch>" \
  --grumpy   # only when step 0b found the grumpy skills
  # --deploy "<one line on how the repo deploys>"   # only if step 9 generated a deploy workflow
```

Pass every command exactly as CI runs it (step 4), and omit a flag rather
than inventing a command — the block renders "not configured" for it, which
is honest. The script prints one `path | action` line per file:

- `AGENTS.md` — created (with a `# <repo>` heading) or updated in place;
  content outside the markers is preserved byte for byte.
- `CLAUDE.md` — created as exactly `@AGENTS.md`, or given that line at the
  top if it lacked it. If it still carries the reminder an older init
  appended (`## Run gates before every PR`), the script removes that section
  and says so — AGENTS.md carries it now. Anything else in CLAUDE.md is left
  alone; if the repo keeps hand-written rules there, tell the user they
  belong in AGENTS.md so every host sees them.

`--check` reports what would change without writing and exits 1 on drift —
run it by hand, or from CI, when the toolchain changes. If the script is missing (`${CLAUDE_PLUGIN_ROOT}`
unset on this host), say so and stop here rather than writing the block by
hand — a hand-written block drifts from the template on the next run.

## 11. Verify and report

- Confirm each generated workflow is valid YAML (parse each file).
- Print a table of files created/changed/skipped, the derived toolchain, whether
  branch protection on `ci-pass` was applied (or the command to run), and the
  next steps (run `/sdlc:secrets`).
- Confirm `AGENTS.md` contains the managed SDLC block (between the
  `sdlc:begin`/`sdlc:end` markers) and `CLAUDE.md` contains `@AGENTS.md`, by
  reading both back — not by trusting the script's table alone.
- **Parse every generated workflow with a real parser** — a YAML file that does
  not parse is a workflow GitHub silently never runs, and "I read it and it
  looked fine" is not a parse. Use whatever the repo's stack already provides
  (`python3 -c 'import yaml,sys;yaml.safe_load(open(sys.argv[1]))'`, `yq`,
  `actionlint` if available). If no parser is available, say the workflows were
  **not** verified rather than implying they were.
- **If a deploy workflow was generated (step 9), state five things explicitly:**
  the runtime-ownership model and the evidence for it; the health endpoint the
  smoke check polls and how to change it; the rollback mechanism — or that the
  runtime has none — and its retention floor; where the prod-tier token is read
  from, and that the `production` environment may not exist yet (`/sdlc:secrets`
  creates it); and the runner the deploy job targets. These are the facts whoever
  is paged will need, and the generated YAML alone does not make them obvious.
- **If step 9 was skipped, say so and why** — `--minimal`, no runtime, or
  detection found nothing — as its own line in the summary. A deploy pipeline
  that was never generated leaves no file to notice missing, so silence here
  reads as success.
- State grumpy status in the summary table: installed (full gate quality) or
  missing (self-review fallback — remind to install).
- Remind: open a PR; the new `ci.yml` runs on it, and `ci-pass` is now the gate.
