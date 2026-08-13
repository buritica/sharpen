---
description: "Wire 1Password-based secrets across three tiers (dev workstations, PR CI, prod) with tier-scoped service accounts, the op:// template, and the right delivery for each tier. Blast-radius isolation throughout. Pass --scaffold-prod to generate a repo-specific bootstrap script for the prod tier."
argument-hint: "[--repo <name>] [--tiers dev,ci,prod] [--scaffold-prod]"
allowed-tools: ["Bash", "Read", "Grep", "Glob", "Edit", "Write"]
---

# /sdlc:secrets

Set up secrets injection across three tiers with tier-scoped service accounts,
so a leaked token can never reach a tier it wasn't granted. The injection itself
relies on stable `op://` secret-reference syntax — works the same in every tier.

> CI/prod uses **1Password Environments** (beta) as the scoping unit; dev uses a
> shared `dev` vault. Confirm `op --version` is recent.

## The model

| Tier | Vault / Environment | Service account | Stored where |
|---|---|---|---|
| **Dev workstation** | shared `dev` vault | one `dev` SA (read+write), distributed to every dev machine | `~/.config/op/dev-token`, placed by whatever configuration management provisions your workstations |
| PR CI | `<repo>-ci` Environment | read-only → `<repo>-ci` only | GitHub repo secret `OP_SERVICE_ACCOUNT_TOKEN` |
| Deploy/prod | `<repo>-prod` Environment | read-only → `<repo>-prod` only | on the deploy box, or a GitHub `production` Environment secret |

Service-account environment scope is **chosen at creation and immutable** — so
create one SA per tier. The `.env.op` template uses `op://$APP_ENV/...` for
CI/prod (where `APP_ENV` swaps per context); the dev tier uses a literal
`op://dev/<repo>/<field>` (one vault, no parameterization).

## Naming convention (all tiers, kebab-case throughout)

| Layer | Encodes | Example |
|---|---|---|
| vault / Environment | the **env** | `dev`, `<repo>-ci`, `<repo>-prod` |
| item title | the **repo** (dev) or a **logical group** (CI/prod) | `<repo>` (in `dev`), `openrouter` (in `<repo>-ci`) |
| field name | the **secret descriptor** (env var lowercased + hyphenated) — never the env | `GEMINI_API_KEY` → `gemini-api-key` |
| reference | full `op://...` path | `op://dev/<repo>/gemini-api-key` · `op://$APP_ENV/openrouter/key` |

**Env lives in the vault, never in the field name.** Suffixing fields with `-dev`
or `-prod` would mean one item exposes multiple envs to a single SA — the exact
isolation we're paying for.

## 1. Preconditions

```bash
command -v op >/dev/null && op --version || echo "Install 1Password CLI first"
command -v gh >/dev/null && gh auth status || echo "gh not authenticated"
```

Determine `<repo>` from `--repo` or `basename "$(git rev-parse --show-toplevel)"`.
Determine tiers from `--tiers` (default `dev,ci,prod`).

## 2. Dev workstation tier (shared `dev` vault)

The dev tier serves `mise run setup` / `workon <repo>` on developer machines.
One vault, one SA, distributed once per machine — never interactive.

**One-time per fleet (admin):**

```bash
op service-account create dev --vault dev:read_items,write_items
# Stash the token where every machine's primary SA can read it:
op item create --vault <provisioning-vault> --category "API Credential" \
  --title dev-service-account-token "credential[password]=<paste-token>"
```

Write access lets the dev SA *create* items as new repos onboard (matches the
"stub secrets on the user's behalf" working rule); the audit log records each
write. The token sits scoped to one vault — it cannot reach `<repo>-ci` /
`<repo>-prod` Environments.

**Per dev machine:** the token file has to land on each workstation without a
human pasting it. Provision it through whatever already configures those machines
(Ansible, a dotfiles bootstrap, an MDM) by reading it from a vault the workstation
can already reach. An Ansible role handling it might look like:

```yaml
# host_vars/<machine>.yml
op_extra_tokens:
  - name: dev
    ref: "op://<provisioning-vault>/dev-service-account-token/credential"
```

The mechanism matters less than the property: the token arrives from a vault, not
from a chat message, and is scoped so it cannot reach the CI or prod tiers.

Re-running the `op` role drops `~/.config/op/dev-token` (0600). The role is
resilient — a not-yet-created ref warns and skips, never breaks provision.

**Per repo:** put a `.env.op` in the repo root with `op://dev/<repo>/<field>`
references (commit it; gitignore the resolved `.env`, allow `!.env.op`). Wire
resolution into the repo's `mise.toml` setup task with this precedence:

```toml
# mise.toml — setup task that prefers the scoped dev SA, falls back gracefully
[tasks.setup]
run = """
tok=""
for t in "$HOME/.config/op/dev-token" "$HOME/.config/op/service-account-token"; do
  [ -f "$t" ] && { tok="$t"; break; }
done
if [ -n "$tok" ] && [ -f .env.op ]; then
  OP_SERVICE_ACCOUNT_TOKEN="$(cat "$tok")" op inject -f -i .env.op -o .env \\
    && echo "✓ secrets → .env (op service account: $(basename "$tok"))"
elif [ -f .env ]; then
  echo '✓ using existing .env (no SA token — hardcoded fallback)'
else
  echo '⚠ no SA token and no .env; fill .env manually'
fi
# … then your stack-specific install (npm ci / bun install / uv sync / etc.)
"""
```

Verify: `mise run setup` logs `op service account: dev-token` when the dev SA is
in place.

## 3. CI/prod tiers — create Environments + scoped service accounts

Guide the user through (these are account-level operations — show the commands,
let the user run the ones that mint credentials):

- Create an Environment per tier in the 1Password Developer section
  (`<repo>-ci`, `<repo>-prod`), or via the documented CLI/SDK path.
- Create one **read-only** service account per tier, scoped to *only* that
  tier's Environment. Capture each token once — it is shown once.

Link the docs:
- https://www.1password.dev/environments/
- https://developer.1password.com/docs/service-accounts/get-started/
- https://developer.1password.com/docs/ci-cd/github-actions/

## 4. Emit the template

If `.env.op` doesn't exist, copy `${CLAUDE_PLUGIN_ROOT}/templates/env.op.example`
to `.env.op` and fill it from the repo's current `.env` / `.env.example`,
rewriting each value to `op://$APP_ENV/<item>/<field>` (for CI/prod) or
`op://dev/<repo>/<field>` (for dev). Never write a real secret value into the
template. Ensure `.env` and `.env.op` are gitignored; `.env.op.example` (no
secrets) may be committed.

## 5. Wire CI injection

In `ci.yml` (or wherever CI needs secrets), inject before the step that needs
them:

```yaml
      - uses: 1password/load-secrets-action@v2
        with:
          export-env: true
        env:
          OP_SERVICE_ACCOUNT_TOKEN: ${{ secrets.OP_SERVICE_ACCOUNT_TOKEN }}
          # Each secret your job needs, by op:// reference. APP_ENV is the
          # Environment this token is scoped to.
          EXAMPLE_TOKEN: op://${{ env.APP_ENV }}/example-item/token
```
Set `env.APP_ENV: <repo>-ci` at the job or workflow level for CI.

## 6. Set the GitHub secret

```bash
gh secret set OP_SERVICE_ACCOUNT_TOKEN --repo <owner>/<repo>   # paste the CI-tier token
```

For a hosted deploy that needs prod secrets, store the prod-tier token in a
GitHub **Environment** named `production` (with branch protection + optional
required reviewer) instead of a plain repo secret:
```bash
gh secret set OP_SERVICE_ACCOUNT_TOKEN --repo <owner>/<repo> --env production
```
For a **self-hosted deploy box**, keep the prod token on the box
(`~/.config/op/service-account-token`) and `op inject` at deploy time — nothing
prod-scoped enters GitHub at all.

## 6b. Scaffold the prod-tier bootstrap script (`--scaffold-prod`)

The prod tier's boilerplate — vault create, item existence check,
90-day-capped SA mint with `--raw`, token archive in a vault you nominate, push
to the rotation box, three-way isolation verify — is identical across repos.
Only the item structure and the seed source differ. So when the user
passes `--scaffold-prod`, generate a repo-specific bootstrap script from
the shared template and stop; the user fills in the ITEM SEED block.

**Template**: `${CLAUDE_PLUGIN_ROOT}/templates/bootstrap-prod-tier.sh` —
parameterized on three placeholders. Copy to
`scripts/bootstrap-<repo>-prod-tier.sh` in the target repo, substitute all
three, and `chmod +x` the result:

| Placeholder | Value |
|---|---|
| `{{repo}}` | from `--repo`, else `basename $(git rev-parse --show-toplevel)` |
| `{{rotation_host}}` | the deploy box that holds the prod token — **ask**; there is no sensible default, and a wrong one SSHes somewhere real |
| `{{archive_vault}}` | the 1Password vault where the SA token is archived — **ask** |

The script refuses to run while any placeholder is unsubstituted or empty, so a
half-filled copy fails loudly instead of acting on the wrong host.

Instruct the user on what they still need to do:

1. **Fill `EXPECTED_ITEMS`** with the logical-group titles they want (per
   §Naming convention). Guidance: kebab-case groups like `google-oauth`,
   `auth`, `cloudflare`, `<external-system>`. Group by "these fields
   rotate together" or "these belong to one external system."
2. **Fill the source reads** in the seed block — where does the initial
   seed data come from? Common patterns are inline as commented examples
   in the template (existing 1P vault, Stripe Projects env pull, running
   process env). The awkward part is usually reading *out* of wherever the
   secrets live today. Filled in, the three sites look like this — they are
   **three separate places in the template**, not one pasteable block
   (`EXPECTED_ITEMS` near the top, the reads and `upsert` calls inside §3, after
   `upsert()` is defined):

   ```bash
   # (a) EXPECTED_ITEMS — the logical groups this repo will create
   EXPECTED_ITEMS=(google-oauth database)

   # (b) source reads, inside the seed block
   #     from the pre-migration vault this repo used before the tier model:
   GOOGLE_ID=$($OP read "op://<legacy-vault>/${REPO}_group/google-client-id")
   GOOGLE_SECRET=$($OP read "op://<legacy-vault>/${REPO}_group/google-client-secret")
   #     from a running process, for values never stored anywhere:
   DATABASE_URL=$(ssh "$ROTATION_HOST" 'cat /etc/myapp/env' | sed -n 's/^DATABASE_URL=//p')

   # (c) upserts — one per group, all of that group's fields on one call
   upsert google-oauth "client-id[text]=$GOOGLE_ID" "client-secret[password]=$GOOGLE_SECRET"
   upsert database     "url[password]=$DATABASE_URL"
   ```

   **Type every field, and mark every secret `[password]`.** An untyped field is
   created as plain text — visible and unmasked in the 1Password UI. Use
   `[text]` only for genuinely non-secret values (a client ID, a hostname), so
   that an untyped field always reads as an oversight rather than a decision.

   Every variable an `upsert` references must be assigned above it: the script
   runs under `set -u`, so a group you listed but never sourced aborts the run
   partway through the seed.
3. **Fill the `upsert` calls** — one per item, all fields on a single
   invocation.
4. **Flip `SEED_CONFIGURED=1`** at the top of the file when 1–3 are done.
   The template dies with a clear error until this is set — no silent
   half-seeded states.
5. **Write `docs/SECRETS.md` and `docs/DEPLOY.md`** covering, at minimum: which
   vault or Environment each tier reads, where each token lives, how to rotate
   one, and what to do when a rotation is missed.
6. **Run the script.** It authorizes via one biometric prompt then
   proceeds unattended. Guards: placeholder-not-replaced check aborts if
   any of the three placeholders wasn't substituted (or is blank); the
   archive vault is checked for existence *before* the SA is minted, since
   1Password shows that token only once; empty `EXPECTED_ITEMS` aborts before
   §7 so we never report "Done" without proving isolation.
7. **Schedule the 90-day rotation reminder** — 1P caps SA lifetime at
   2160h. Set a reminder for ~48h before the SA's expiry, somewhere that
   will actually reach a human. An expired prod SA fails deploys closed,
   which is the safe direction but an opaque one to debug.

**Gotchas baked into the template (don't second-guess these):**

- `op service-account create --expires-in` maxes at 90 days (2160h). CLI
  hard cap; longer is rejected.
- 1P has no `op service-account list`. Existence detection is
  try-create-and-parse-the-error.
- `op item create` and `op item edit` read stdin for a JSON template when
  unfed — in `ssh bash -s` heredoc pipelines that leaks and errors with
  `invalid JSON in piped input`. The template appends `</dev/null` to
  every mutating op call.
- 1Password "Environments" are UI-only in op 2.34.1 — no `op environment`
  subcommand. Plain vaults with a vault-scoped SA achieve identical
  isolation, and the `op://<name>/<item>/<field>` reference syntax is
  the same.

## 7. Verify and report

- **Dev:** `mise run setup` prints `op service account: dev-token`; `.env`
  resolves. Prove scope isolation: `OP_SERVICE_ACCOUNT_TOKEN=$(cat ~/.config/op/dev-token) op read 'op://<provisioning-vault>/...'`
  errors with *"isn't a vault in this account"*.
- **CI:** `gh secret list --repo <owner>/<repo>` shows `OP_SERVICE_ACCOUNT_TOKEN`;
  `APP_ENV=<repo>-ci op inject -i .env.op -o /tmp/.env.check && head /tmp/.env.check && rm /tmp/.env.check`
  resolves (requires a session with access).
- **Summarize:** which vault/Environments + SAs were created, where each token
  lives, and the isolation guarantees (dev token cannot read CI/prod
  Environments; CI token cannot read `<repo>-prod`).
