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

Claude commands, `Skill` lifecycle events, `Task` dispatch, `${CLAUDE_PLUGIN_ROOT}`, and `PreToolUse`/`PostToolUse` hooks are therefore adapter features, not portable-core requirements.

## Protocol documents

The v1 schemas are:

- [`capability-manifest.v1.schema.json`](../schemas/capability-manifest.v1.schema.json): what outcomes an adapter can provide.
- [`review-report.v1.schema.json`](../schemas/review-report.v1.schema.json): a structured, actionable review result.

Every document declares `protocol_version: "1"`. Schema `$id` values are versioned too. Consumer implementations must ignore unknown fields; adapters should put host-specific additions under `x-` keys.

The checked-in examples are normative minimal examples, not a claim that the repository has implemented a generic runtime yet.

## Capability profiles

Resolve a profile against the active capability manifest *before* creating a gate cycle:

| Profile | Required outcomes | Use case |
|---|---|---|
| `baseline` | `test`, `lint`, `typecheck` | Any agent or CI environment |
| `review` | baseline + `review` | A structured independent review is available |
| `adversarial` | review + `imagine`, `fix` | A capable delegated-agent workflow |
| `claude-enforced` | adversarial + Claude hook support | Current Claude Code installation |

A host must not auto-arm a profile it cannot complete. This avoids treating a missing host-specific skill as a workflow failure.

## Review evidence

A normal portable review uses `provenance.kind: "git-range"` and records both `base` and `head`. Every finding has a severity, summary, location, and concrete consequence. An empty `findings` list is valid for a passing clean review.

`provenance.kind: "legacy"` is allowed during migration for adapters that cannot recover a range. It is explicitly lower fidelity and should not be used by new CI enforcement.

The report records the executing agent/model only when the host can supply it. Identity metadata helps auditability but is not needed to make a report portable.

## State root migration

Portable state uses `.sharpen/` for new shared data:

```text
.sharpen/
  data/gates.json
  data/capabilities.claude.json
  sdlc/<branch>/plan.md
  reviews/<branch>/review.json
```

The gate store and capability-manifest adapter now resolve shared data in this order: an explicit environment override, `.sharpen/data/`, then existing `.claude/data/` as a compatibility fallback. Existing installs therefore keep reading and writing active state until `.sharpen/data/` exists; fresh state starts in the neutral root.

`.claude/sdlc/` and `.claude/grumpy/` remain adapter-local scratch/artifact locations and are not moved by this step. A full migration must still read the old locations, write neutral locations atomically, and preserve a documented rollback path.

## Enforcement model

Local hooks are useful feedback, but they cannot be the cross-clone authority: forked pull requests and agents without equivalent hooks do not share local state. A future CI adapter should validate these artifacts against the PR head commit and make that result part of the repository's one required check. The existing GitHub `ci-pass` pattern remains the intended enforcement shape; only its evidence source becomes portable.
