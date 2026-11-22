# Portable core protocol

Sharpen currently ships as a Claude Code marketplace. This protocol identifies the parts that can work with any coding agent, model, CI provider, or human workflow without making the Claude adapter less capable.

## Boundary

The portable core owns policy and durable data:

- gate tiers and their required capabilities;
- review artifacts and change provenance;
- capability discovery;
- artifact validation; and
- the decision a host adapter uses to allow opening a pull request.

An adapter owns host mechanics:

- command syntax and user interaction;
- tool invocation, subagent delegation, and lifecycle hooks;
- model routing; and
- forge APIs such as GitHub, GitLab, or a manual fallback.

Claude commands, `Skill` lifecycle events, `Task` dispatch, Claude Code's `CLAUDE_PLUGIN_ROOT` substitution, and `PreToolUse`/`PostToolUse` hooks are therefore adapter features, not portable-core requirements.

## Protocol documents

The v1 schemas are:

- [`capability-manifest.v1.schema.json`](../schemas/capability-manifest.v1.schema.json): what outcomes an adapter can provide.
- [`review-report.v1.schema.json`](../schemas/review-report.v1.schema.json): a structured, actionable review result.

Every document declares `protocol_version: "1"`. Schema `$id` values are versioned too. Consumer implementations must ignore unknown fields; adapters should put host-specific additions under `x-` keys.

The checked-in examples are normative minimal examples, not a claim that the repository has implemented a generic runtime yet.

## Skill-completion adapter contract

A host that has a trustworthy post-skill lifecycle callback may automatically record a skill-gated gate by calling `plugins/sdlc/scripts/skill_gate_completion.py`:

```python
record_skill_completion(
    skill="grumpy:review",
    cwd="/canonical/repo/worktree",
    succeeded=True,
    host="example-host",
    host_version="1.2.3",  # optional
    invocation_id="host-event-7",  # optional
)
```

The event fields are a canonical skill ID, canonical invocation cwd, boolean success result, host name/version, and optional invocation ID. The shared recorder validates the event, resolves same- and cross-repo routes, preserves gate ordering and invalidation, and performs the authorized atomic store write. Adapter metadata is returned for diagnostics, not treated as stored authority.

On a receipt-backed cycle (every cycle `--init` or auto-init creates; see `gate.md`, "How skill gates record") the event alone proves nothing. The recorder stamps a gate only when the grumpy pass's receipt (`schemas/grumpy-receipt.v1.schema.json`, under the gitignored `.sharpen/grumpy/<branch>/receipts/`, or `.claude/grumpy/` for a receipt an older grumpy wrote) names a commit on the branch (HEAD, or an ancestor once a later fix has committed), was written after the cycle started, and, for a fix gate, after the receipt of the pass it follows. Enforce then requires the last skill gate to sit on HEAD. That makes every host equal:

- `record-gate.py --verify <pass>` is the host-neutral path. A gate chain runs it after each pass on any host that can run a shell.
- Claude Code's `PostToolUse` `Skill` hook and `Stop` hook, and pi's `agent_end`, call the same recorder sooner. They are conveniences; the receipt is the evidence.
- `record-gate.py --attest` stays the visible, reason-required fallback when a receipt can't be produced.

Receipts record that the work ran. They are not a security boundary, and are not meant to be.

Legacy cycles (no `evidence` key) keep event-only recording until their next `--init`, and the receipt-only paths (`--verify`, the sweeps) never stamp them.

## Capability manifest

Each host's session-start adapter (`plugins/sdlc/scripts/session-start.py`) writes `.sharpen/data/capabilities.<host>.json`, naming the host honestly: `SHARPEN_HOST` when the adapter sets it (pi does), `claude-code` when Claude Code's `CLAUDECODE=1` is present, otherwise `unknown`. One file per host, so hosts sharing a repo don't overwrite each other. Next to the v1 fields it records two extensions auto-init reads:

- `x-recording`: `automatic` when a host event sweeps receipts at the end of a turn (Claude Code, pi), `manual` otherwise (run `--verify`). Auto-init takes this from the host table in `hosts.py`, not from the manifest; the field is a declaration for other readers.
- `x-receipts`: whether the installed grumpy writes receipts. When it doesn't, auto-init on Claude Code arms an event-only cycle (its `Skill` event can stamp one; a receipt-backed one could never complete). Other hosts have no such event, so they keep the receipt-backed cycle and are told gates 2-6 can't record until grumpy is upgraded to 2.9.0+ (`--attest` per gate is the last resort).

[`hosts.md`](hosts.md) has the per-host support matrix, generated from the same table.

## Capability profiles

A profile summarizes a manifest's capabilities:

| Profile | Required outcomes | Use case |
|---|---|---|
| `baseline` | `test`, `lint`, `typecheck` | Any agent |
| `review` | baseline + `review` | A structured independent review is available |
| `adversarial` | review + `imagine`, `fix` | A capable delegated-agent workflow |

A profile is a declaration, recorded on the cycle for diagnostics. It does not decide which gates a cycle requires: the tier alone does (`gate_store.required_gates`), on every host. Auto-init never arms a lighter tier because a profile is incomplete. Since receipts, every host that can run a shell can record gates 2 to 6, so a lighter tier would only quietly drop the review gates. Instead, the arming message names the grumpy passes the manifest lacks, and a person decides whether to install grumpy or re-init as `tiny`. An explicit `record-gate.py --init --profile <name> --capabilities-file <path>` still fails when the declared capabilities can't satisfy the requested profile.

## Review evidence

A normal portable review uses `provenance.kind: "git-range"` and records both `base` and `head`. Every finding has a severity, summary, location, and concrete consequence. An empty `findings` list is valid for a passing clean review.

`provenance.kind: "legacy"` is allowed during migration for adapters that cannot recover a range. It is explicitly lower fidelity; new adapters should record a range.

The report records the executing agent/model only when the host can supply it. Identity metadata helps auditability but is not needed to make a report portable.

An attached report is informational. `record-gate.py --status` shows it, and nothing blocks on its `status` or provenance: attaching one never completes a gate, and a `fail` report doesn't stop a PR. The gates themselves do that.

## State root migration

Portable state uses `.sharpen/` for new shared data:

```text
.sharpen/
  data/gates.json
  data/capabilities.<host>.json
  sdlc/<branch>/plan.md
  reviews/<branch>/review.json
  simplify.json
  models.yaml
```

`simplify.json` (the `/grumpy:simplify` policy config) and `models.yaml` (a
repo's override of grumpy's role→model map, when present) are the committed
files under `.sharpen/`, unlike `data/`, which is gitignored per-run state.

Artifact folders ignore themselves, so keeping them out of git needs no host
support and no `/sdlc:init`: whatever creates `.sharpen/data/`,
`.sharpen/sdlc/`, `.sharpen/grumpy/` or `.claude/worktrees/` also writes a
`.gitignore` holding `*` inside it (the stdlib writers through sdlc's
`artifact_dirs.py` and grumpy's `receipt.py`, and one shell line beside each
command's `mkdir -p`). `.sharpen/` itself never gets one, because it holds
the committed `simplify.json` and `models.yaml`. `/sdlc:init` still lists the same folders in
the repo's own `.gitignore` as a second layer.

The gate store and capability-manifest adapter now resolve shared data in this order: an explicit environment override, `.sharpen/data/`, then existing `.claude/data/` as a compatibility fallback. Existing installs therefore keep reading and writing active state until `.sharpen/data/` exists; fresh state starts in the neutral root.

Per-branch plans, specs, reports and receipts moved the same way in sdlc 4.21 and grumpy 2.15: they are written to `.sharpen/sdlc/<branch>/` and `.sharpen/grumpy/<branch>/`, and nothing is written to the old `.claude/sdlc/` and `.claude/grumpy/` folders any more. How the old folders are still read: a receipt in both places counts by whichever copy is newer; a plan or a `fix.md` that only exists in the old folder is copied into the new one the first time a pass appends to it, so nothing is ever written under `.claude/`; every other file (reports, specs, `base.json`) is read from the new folder first and the old one only when the new copy is missing. The reason is a host one, not tidiness. Claude Code treats `.claude/` as its own configuration: its file tools put a write there through a safety check that no permission rule or classifier can approve, so a headless session refuses it and a pass that writes its report with the Write tool prints the report instead of saving it. No such rule was found in Codex or pi; that is unverified, and `docs/hosts.md` lists the checks that would settle it. The two plugins can't import each other, so each names the grumpy root on its own (`artifact_dirs.GRUMPY_ROOT` in sdlc, `GRUMPY_ROOT` in grumpy's `receipt.py`), and `plugins/sdlc/tests/test_artifact_root.py` fails if they drift. Rolling back means installing sdlc 4.20 and grumpy 2.14 together. They read only `.claude/`, so anything written after the upgrade (plans, `base.json`, reports, receipts) is invisible to them until it is copied back, in each worktree that holds work in progress (`/sdlc:plan` worktrees included): `mkdir -p .claude/sdlc .claude/grumpy && cp -pR .sharpen/sdlc/. .claude/sdlc/ && cp -pR .sharpen/grumpy/. .claude/grumpy/`. `-p` keeps the reports' mtimes, so grumpy 2.14 doesn't count a copied report as freshly saved. The copy is safe to repeat, and the `.gitignore` files it carries keep both folders out of git. `/sdlc:plan` worktrees still live under `.claude/worktrees/`, because `git worktree add` creates them from a shell, which the file-tool check doesn't cover.

## Enforcement model

The gate is local, and it is the product. Everything runs on the developer's machine, inside the agent's session, before a pull request exists: the hooks and the CLI read and write a gitignored gate store, and opening a PR stays blocked until the branch's cycle is complete. Two layers do the blocking:

- **Host hooks** stop every PR path `pr_paths.py` recognizes (`gh pr create`, `gh api` POSTs, `glab`, `hub`, `git push -o merge_request.create`, MCP PR tools) on hosts that run them: Claude Code and Codex through `hooks/hooks.json`, pi through its extension.
- **A git pre-push hook** (`pre-push.py`, installed by `/sdlc:gate`) is the floor under them: host-agnostic, it runs the same missing/stale check for whatever a script, alias or the web UI opens after the push. It warns by default and refuses only when the repo opts in with `git config sharpen.prePush block`.

Nothing is sent to CI, and no CI job checks gate evidence.

Its limits, stated plainly:

- **Opt-out, per branch.** A branch with no cycle is not gated (auto-init arms one on the first commit where the host supports it). `--attest` stamps a gate on a human's word, with a required reason, and shows as unverified.
- **Not adversarial.** It stops an agent talking itself past its own process. It does not stop someone who edits the store, sets `SDLC_ALLOW_PUSH=1`, skips the hooks, or pushes from another clone. The people and agents using it are coworkers, not attackers.
- **One machine.** A fork's pull request, or an agent running without the host hooks, has no cycle in this checkout's store and is not covered. A repository that needs to hold outside contributions to a review standard needs its own review policy for them; sharpen does not supply a CI-side substitute.

CI still matters, for a different job. `/sdlc:init` scaffolds a consumer repository's pipeline with one required check, `ci-pass`, that verifies the code itself (tests, lint, typecheck, build). That check never reads gate state. Earlier versions shipped `ci-validate.py` and a `gates` job that did; because the store is gitignored, the job could only ever pass, and it was removed in sdlc 4.20.0.
