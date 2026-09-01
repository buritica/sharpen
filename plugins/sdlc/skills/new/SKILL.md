---
name: new
description: "Bootstrap a brand-new repo: detect stack, lay down toolchain config, delegate CI + git hooks to /sdlc:init, drop a 1Password .env.op template, scaffold a conventional-commit README + .gitignore, and wire prepare → core.hooksPath. Idempotent."
---

# /sdlc:new

Bootstrap a new repo: the pieces that belong together at repo creation time —
stack detection, toolchain config, CI delegation, and a secrets template.

It **delegates** CI and local hook setup entirely to `/sdlc:init` — it does not
reimplement that logic. The only thing this command adds on top of `/sdlc:init` is
the README, `.gitignore`, and the `.env.op` template.

**Idempotent:** every file write is preceded by an existence check. Existing
files are read and a diff is shown before overwriting. Running `/sdlc:new`
twice on the same repo is safe — it skips what's already there.

---

## 0. Parse arguments

From `$ARGUMENTS`:

- `--stack <name>` — override auto-detected stack (e.g. `--stack python`). Must
  match a stack name from `stacks.md` (node, python, go, rust, ruby, java, dotnet,
  elixir, php). If not recognised, warn and fall back to auto-detection.
- `--deploy <target>` — deployment target hint to embed in the README and `.env.op`
  (e.g. `--deploy fly`, `--deploy railway`, `--deploy vercel`). Optional; if
  absent, the secrets template uses generic placeholders.

Strip `--stack` before forwarding — it is consumed here. **`--deploy` is consumed
here *and* forwarded** to `/sdlc:init` in step 6; see the reasoning there. Forward
any remaining arguments untouched.

---

## 1. Detect the stack

If `--stack` was provided and is recognised, use it directly and skip scanning.

Otherwise read the stack reference:

```bash
cat "${CLAUDE_PLUGIN_ROOT}/templates/stacks.md"
```

Then scan the working directory for manifests. Do not stop at the first hit —
repos are often polyglot:

```bash
ls package.json bun.lock* pnpm-lock.yaml package-lock.json yarn.lock \
   pyproject.toml uv.lock poetry.lock requirements*.txt setup.py \
   go.mod Cargo.toml Gemfile pom.xml build.gradle* *.csproj *.sln mix.exs \
   composer.json 2>/dev/null
ls Makefile Justfile Taskfile* turbo.json nx.json moon.yml 2>/dev/null
ls .github/workflows/ 2>/dev/null
```

Match manifests against the stacks in `stacks.md` to identify the primary stack
and any secondary stacks. For a Node repo, also identify the package manager from
the lockfile (`bun.lock*` → bun, `pnpm-lock.yaml` → pnpm, `yarn.lock` → yarn,
`package-lock.json` → npm).

**State the detected stack(s) and the package manager before writing anything.**
Let the user correct if needed.

---

## 2. Derive the repo name

```bash
basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
```

Use this as `$REPO_NAME` throughout.

---

## 3. Scaffold conventional-commit README

Check whether `README.md` already exists:

```bash
test -f README.md && echo "exists" || echo "missing"
```

**If it exists:** Read it, show the user what's there, and ask whether to
overwrite. If they say no, skip this step.

**If it does not exist (or user confirms overwrite):** Write a `README.md` with:

~~~markdown
# $REPO_NAME

<!-- one-line description — fill in -->

## Prerequisites

<!-- list runtime, env vars, external services needed -->

## Setup

```sh
# clone and install
git clone <repo-url>
cd $REPO_NAME

# inject secrets (requires 1Password CLI)
op inject -i .env.op -o .env

# install dependencies
<stack-specific install command from stacks.md>
```

## Development

```sh
<stack-specific dev/run command>
```

## Commit conventions

This repo uses [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short description>

Types: feat | fix | docs | style | refactor | test | chore | ci
```

Breaking changes: append `!` after type/scope or add `BREAKING CHANGE:` footer.

## CI

Pull-request CI runs on GitHub Actions. See `.github/workflows/ci.yml`.
Secrets are injected via 1Password Environments — see `.env.op` for the template.

## Deployment

<!-- describe how to deploy; update after running /sdlc:secrets -->
~~~

Fill in the stack-specific install/dev commands from `stacks.md` for the
detected stack. Leave `<!-- … -->` placeholders where the user must supply
project-specific content.

---

## 4. Scaffold .gitignore

Check whether `.gitignore` already exists:

```bash
test -f .gitignore && echo "exists" || echo "missing"
```

**If it exists:** show the first 20 lines and ask whether to append or skip.

**If it does not exist:** create `.gitignore` with at minimum:

```
# secrets — never commit
.env
.env.*
!.env.op
!.env.example

# OS
.DS_Store
Thumbs.db

# Editor
.idea/
.vscode/
*.swp
*.swo
```

Don't add the `.claude/` agent-scratch entries here — step 6 delegates to
`/sdlc:init`, which appends them and reconciles any existing entry. One owner
for that list.

Then append stack-specific entries based on the detected stack(s):

- **Node/bun:** `node_modules/`, `dist/`, `.next/`, `.turbo/`, `*.tsbuildinfo`
- **Python:** `__pycache__/`, `*.pyc`, `.venv/`, `dist/`, `*.egg-info/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`
- **Go:** `bin/`, `*.test`, `coverage.out`
- **Rust:** `target/`
- **Ruby:** `.bundle/`, `vendor/bundle/`
- **Java/Kotlin:** `build/`, `.gradle/`, `out/`, `*.class`, `*.jar`
- **.NET:** `bin/`, `obj/`, `*.user`
- **Elixir:** `_build/`, `deps/`, `*.beam`
- **PHP:** `vendor/`

---

## 5. Drop the 1Password .env.op template

Check whether `.env.op` already exists:

```bash
test -f .env.op && echo "exists" || echo "missing"
```

**If it exists:** show it and skip (tell the user to run `/sdlc:secrets` to update it).

**If it does not exist:** read the canonical template from the sdlc plugin:

```bash
cat "${CLAUDE_PLUGIN_ROOT}/templates/env.op.example"
```

Write `.env.op` adapting the template for this repo. The key convention is:

```
# 1Password Environments — one file, all tiers.
# Inject: APP_ENV=<repo-name>-ci op inject -i .env.op -o .env
# See /sdlc:secrets for full setup.

# ── Required ─────────────────────────────────────────────────────────────

# DATABASE_URL=op://$APP_ENV/<repo-name>/database-url

# ── Optional — each enables a feature ──────────────────────────────────

# ANTHROPIC_API_KEY=op://$APP_ENV/anthropic/key
```

If `--deploy <target>` was provided, add a commented section with deploy-target-
specific vars (e.g. for `fly`: `FLY_API_TOKEN=op://$APP_ENV/fly/api-token`; for
`railway`: `RAILWAY_TOKEN=op://$APP_ENV/railway/token`; for `vercel`:
`VERCEL_TOKEN=op://$APP_ENV/vercel/token`).

All secrets use the `op://$APP_ENV/...` reference pattern — never plaintext values.
Remind the user to run `/sdlc:secrets` to create the matching 1Password Environments
and service accounts.

---

## 6. Delegate CI + local hooks to /sdlc:init

This is the core delegation step. **Do not reimplement ci's logic here.**

Tell the user:

> Delegating CI scaffold and local hook wiring to /sdlc:init. This will:
> - Generate .github/workflows/ci.yml tailored to the detected stack
> - Add the ci-pass aggregator + require it as branch protection (default-on)
> - Set up the reusable setup workflow, secret-scan, and dependabot
> - Configure the stack's idiomatic formatter/linter if none exists
> - Create changed-files pre-commit hooks (main-guard + safe auto-format;
>   pre-push only if the repo has a genuinely slow check)
> - Wire prepare → core.hooksPath

Then invoke `/sdlc:init`, forwarding any `--self-hosted` or `--minimal` flags the
user provided (from `$ARGUMENTS`, after stripping the flags this command owns).

**Also pass `--deploy <target>` through if it was given.** On a repo this command
just created there is no `fly.toml`, no `ecosystem.config.js`, and no platform
manifest yet — so `init`'s step-9 runtime detection finds nothing and skips the
deploy workflow entirely. The user's explicit `--deploy` hint is the *only*
evidence of intent that exists at this point in a new repo's life; drop it and
the one case where it matters is the case where it's silently ignored. When
`--deploy` is absent, say that no deploy workflow was scaffolded and that
`/sdlc:init` can add one later once a manifest exists.

```
/sdlc:init [--deploy <target>] [--self-hosted <label>] [--minimal]
```

Wait for `/sdlc:init` to complete before proceeding. Its output is authoritative —
if it detects a different stack than step 1, trust it and note the discrepancy.

---

## 7. Verify prepare → core.hooksPath is wired

After `/sdlc:init` returns, confirm the hook wiring landed:

```bash
# For Node repos: check package.json prepare script
grep -s '"prepare"' package.json

# For all stacks: check git config
git config core.hooksPath
```

If neither is set and the stack is not Node (where `/sdlc:init` handles it via
`prepare`), explicitly set:

```bash
git config core.hooksPath .githooks
```

And inform the user that `git config core.hooksPath .githooks` was set directly
(non-Node stacks don't have a `prepare` lifecycle).

---

## 8. Summary report

Print a table of every file created, skipped, or delegated:

| File / Action | Status | Notes |
|---|---|---|
| `README.md` | created / skipped | — |
| `.gitignore` | created / appended / skipped | — |
| `.env.op` | created / skipped | run `/sdlc:secrets` next |
| CI + hooks | delegated to `/sdlc:init` | see its output |

Then print the **next steps** checklist:

1. Fill in the `<!-- … -->` placeholders in `README.md`.
2. Run `/sdlc:secrets` to create 1Password Environments + service accounts and
   populate `.env.op` with real item references.
3. Push a branch and open a PR — the new `ci.yml` runs on it.
4. Enable required checks in repo settings (Settings → Branches → protection rule).
