# Prior art & inspiration — `grumpy:audit`

Non-binding reference for building `grumpy:audit` (see issue #31). This is the *why* and the *influences*, not the spec. The build is driven by a `/goal` condition stated in the issue; treat everything here as raw material to draw from, not requirements to satisfy.

## What `grumpy:audit` is

The comprehensive, composing audit. It runs **all** the grumpy skills (`review`, `architecture`, `security`, `product`, `edge-cases`, `cleanup`) over a codebase and synthesizes them into one picture: an accurate read of the repo plus a **crisp, prioritized set of improvement steps**, honestly sized from trivial to major. It does not flatten complex work into simple tasks; it names complexity where it exists.

The audit's job is *understanding + direction*, not *cheap execution*. It produces the map and the route. How those steps get executed — and how cheaply — is a separate, downstream concern (see Thrifty below). The audit *feeds* that by labeling each step's effort/complexity honestly; it does not optimize itself for any one execution tier.

## The downstream shape (separate concern)

`ca` (or any orchestrator) tells Claude Code **what to achieve**, not **how**. A strong model audits and plans once; execution of the resulting steps routes by complexity — simple steps to cheap models, complex steps to strong ones; a gate proves each. `grumpy:audit` is the "understand + plan" stage that makes that routing possible.

## Influences

### 1. Thrifty — the model split

harper, https://x.com/harper/status/2064456570251919810

> strong model plans → cheap model executes → gate verifies

The core economic idea, applied **downstream of the audit**. Pay for reasoning once, at the top (the audit). Then execute each step on the cheapest model that can still pass the gate — simple steps cheap, complex steps on strong models — escalating only on failure. The audit enables this by sizing each step honestly; it is not itself shaped for haiku. Tier routing lives in execution (`grumpy:fix`), not in what the audit chooses to find.

### 2. The repo-audit prompt — the 4-phase structure

"Repo Audit & Improvement Plan", attributed to Claude Fable 5, shared by @meta_alchemist, 2026-06-09.
https://x.com/meta_alchemist/status/2064431279383433646 · full text: `prompts/repo-audit-improvement-plan.md` (ca brain)

What's worth taking:
- **Discovery before judgment.** Map the repo and its existing conventions before forming opinions, so recommendations fit the culture instead of fighting it.
- **Evidence-graded findings.** Every finding: what / where (`file:line`) / why it matters (concrete consequence) / severity (Critical/High/Medium/Low). Label facts vs judgments.
- **Signal over volume.** ~15 high-confidence findings beat 50 speculative ones. A healthy dimension gets one sentence.
- **Strengths section.** Name what to preserve, not just what to burn.
- **Milestone-ordered plan.** M0 safety net (tests/CI before refactoring) → M1 critical → M2 high-leverage → M3 polish. Quick wins (high-impact, S-effort) flagged separately.
- **Calibrate to maturity.** No enterprise infra for a weekend prototype.

### 3. Fable 5 system prompt — prompt-engineering technique

Leaked by elder_plinius, 2026-06-09. Study notes: `prompts/claude-fable-5-study-notes.md` (ca brain).

Techniques worth applying to the command prompts themselves:
- **Co-locate "do NOT" with each positive rule** to stop over-application.
- **One worked example per ambiguous rule** — concrete before/after kills misreads.
- **Declarative > imperative framing** — "The reviewer does not…" resists override better than "Do not…".
- **Repeat critical rules at multiple placements.**

### 4. `/goal` — outcome-driven execution

Claude Code native command (v2.1.139+). https://code.claude.com/docs/en/goal

Set a completion condition; a fast validator checks "met?" after each turn and loops until it's provably true. Forces the orchestrator to express work as a verifiable end-state rather than a procedure. The goal should be abstract on implementation, concrete on observable behavior — assert what the feature *does* (provable by running it), not which files exist.

## What we're NOT taking

- The Fable 5 bloat: copyright walls, product catalogs, redundant identity preambles. Keep the grumpy voice and terse house style.
- Rigid adherence to the audit prompt's exact section names or wording — the *discipline* transfers, the boilerplate doesn't.
- Spec-as-goal. The deliverable list in #31 is context, not the `/goal` condition.
