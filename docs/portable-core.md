# Portable core protocol

Sharpen currently ships as a Claude Code marketplace. This protocol identifies the parts that can work with any coding agent, model, CI provider, or human workflow without making the Claude adapter less capable.

## Boundary

The portable core owns policy and durable data:

- gate tiers and their required capabilities;
- review artifacts and change provenance;
- capability discovery;
- artifact validation; and
- the decision a CI or forge adapter uses to allow a merge.

An adapter owns host mechanics:

- command syntax and user interaction;
- tool invocation, subagent delegation, and lifecycle hooks;
- model routing;
- forge APIs such as GitHub, GitLab, or a manual fallback; and
- CI-specific publication of a required check.

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

On a receipt-backed cycle (every cycle `--init` or auto-init creates; see `gate.md`, "How skill gates record") the event alone proves nothing. The recorder stamps a gate only when the grumpy pass's receipt (`schemas/grumpy-receipt.v1.schema.json`, under the gitignored `.claude/grumpy/<branch>/receipts/`) names a commit on the branch (HEAD, or an ancestor once a later fix has committed), was written after the cycle started, and, for a fix gate, after the receipt of the pass it follows. Enforce then requires the last skill gate to sit on HEAD. That makes every host equal:

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
| `baseline` | `test`, `lint`, `typecheck` | Any agent or CI environment |
| `review` | baseline + `review` | A structured independent review is available |
| `adversarial` | review + `imagine`, `fix` | A capable delegated-agent workflow |

A profile is a declaration, recorded on the cycle for diagnostics. It does not decide which gates a cycle requires: the tier alone does (`gate_store.required_gates`), on every host. Auto-init never arms a lighter tier because a profile is incomplete. Since receipts, every host that can run a shell can record gates 2 to 6, so a lighter tier would only quietly drop the review gates. Instead, the arming message names the grumpy passes the manifest lacks, and a person decides whether to install grumpy or re-init as `tiny`. An explicit `record-gate.py --init --profile <name> --capabilities-file <path>` still fails when the declared capabilities can't satisfy the requested profile.

## Review evidence

A normal portable review uses `provenance.kind: "git-range"` and records both `base` and `head`. Every finding has a severity, summary, location, and concrete consequence. An empty `findings` list is valid for a passing clean review.

`provenance.kind: "legacy"` is allowed during migration for adapters that cannot recover a range. It is explicitly lower fidelity and should not be used by new CI enforcement.

The report records the executing agent/model only when the host can supply it. Identity metadata helps auditability but is not needed to make a report portable.

## State root migration

Portable state uses `.sharpen/` for new shared data:

```text
.sharpen/
  data/gates.json
  data/capabilities.<host>.json
  sdlc/<branch>/plan.md
  reviews/<branch>/review.json
  simplify.json
```

`simplify.json` is the one committed file under `.sharpen/` (the
`/grumpy:simplify` policy config), unlike `data/`, which is gitignored
per-run state.

The gate store and capability-manifest adapter now resolve shared data in this order: an explicit environment override, `.sharpen/data/`, then existing `.claude/data/` as a compatibility fallback. Existing installs therefore keep reading and writing active state until `.sharpen/data/` exists; fresh state starts in the neutral root.

`.claude/sdlc/` and `.claude/grumpy/` remain adapter-local scratch/artifact locations and are not moved by this step. A full migration must still read the old locations, write neutral locations atomically, and preserve a documented rollback path.

## Enforcement model

Local hooks are useful feedback, but they cannot be the cross-clone authority: forked pull requests and agents without equivalent hooks do not share local state. The portable CI adapter is [`plugins/sdlc/scripts/ci-validate.py`](../plugins/sdlc/scripts/ci-validate.py): it reads the gate store for the PR head branch, verifies all required gates are complete, and checks that any attached review report's provenance matches the PR head SHA. It runs as a `gates` job in the repository's CI workflow and is part of the one required check (`ci-pass`). Forked PRs and agents without local hooks are held to the same standard here.
