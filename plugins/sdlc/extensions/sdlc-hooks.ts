/**
 * sdlc under pi — replicates the Claude Code hook layer and its interactive
 * gate confirmations.
 *
 * pi has no PreToolUse/PostToolUse/SessionStart hook events and no `Skill`
 * tool. Every gate decision still lives in the pure-stdlib python scripts in
 * `plugins/sdlc/scripts/`; this adapter only translates pi lifecycle events
 * into the Claude hook payloads those scripts read on stdin, and turns their
 * exit codes back into pi tool-call decisions.
 *
 * Event mapping (see docs/pi.md for the full contract):
 *   SessionStart     -> pi `session_start`          -> claude-session-start.py
 *   PreToolUse Bash  -> pi `tool_call` (bash)       -> enforce-sdlc-gates.py
 *                                                    -> block-direct-gate-record.py
 *   PostToolUse Bash -> pi `tool_result` (bash)     -> auto-init-gate-cycle.py
 *   PostToolUse Skill -> no pi equivalent (no Skill tool). Skill-gated gates
 *                        record via the documented `record-gate.py --attest`
 *                        fallback the SKILL.md bodies already carry.
 *
 * The gate confirmation: when a PreToolUse hook denies, Claude shows an
 * interactive modal with the reason and lets a human confirm. pi replicates
 * this with `ctx.ui.confirm`. Only an interactive human in the TUI can
 * override — in headless (print/rpc) mode there is no human to ask, so the
 * gate FAILS CLOSED. The model can never self-bypass; that matches the
 * fail-closed posture the scripts enforce.
 *
 * Also exports `CLAUDE_PLUGIN_ROOT` (and `CLAUDE_PLUGIN_SCRIPTS`) into the
 * process env so the existing cross-host SKILL.md instructions that reference
 * `$CLAUDE_PLUGIN_ROOT/scripts/...` resolve unchanged under pi's bash tool,
 * whose spawned shells inherit `process.env`.
 *
 * Node stdlib only — the repo's "pure stdlib" constraint, in TS.
 */

import type {
  ExtensionAPI,
  ExtensionContext,
  ToolCallEventResult,
} from "@earendil-works/pi-coding-agent";
import { isBashToolResult, isToolCallEventType } from "@earendil-works/pi-coding-agent";
import { PLUGIN_ROOT, SCRIPTS_DIR, runHook } from "./lib/run-hook";

// Let existing SKILL.md `$CLAUDE_PLUGIN_ROOT/scripts/...` references run under
// pi's bash tool (its spawned shells inherit process.env).
process.env.CLAUDE_PLUGIN_ROOT = PLUGIN_ROOT;
process.env.CLAUDE_PLUGIN_SCRIPTS = SCRIPTS_DIR;

/**
 * Replicate a Claude PreToolUse denial's interactive confirmation.
 *
 * Returns a `tool_call` result: `{ block: true, reason }` to deny, or
 * `undefined` to allow (human override). Headless -> always deny.
 */
async function gateConfirmation(
  reason: string,
  title: string,
  fixHint: string,
  ctx: ExtensionContext,
): Promise<ToolCallEventResult | undefined> {
  const clean = reason.trim() || title;
  if (ctx.hasUI && ctx.mode === "tui") {
    const proceed = await ctx.ui.confirm(
      title,
      `${clean}\n\n${fixHint}\n\nProceed anyway? Only an interactive human can override this gate.`,
    );
    if (proceed) {
      ctx.ui.notify(`SDLC gate overridden by human — continuing.\n\n${clean}`, "warning");
      return undefined;
    }
  }
  return { block: true, reason: clean, terminate: true };
}

export default function (pi: ExtensionAPI) {
  // SessionStart: write the capability manifest the gate store reads.
  pi.on("session_start", async (_event, ctx) => {
    await runHook("claude-session-start.py", {}, ctx.cwd);
  });

  // PreToolUse Bash: block `gh pr create` until gates pass, and block direct
  // `record-gate.py --record` for skill-gated gates.
  pi.on("tool_call", async (event, ctx) => {
    if (!isToolCallEventType("bash", event)) return;
    const payload = {
      hook_event_name: "PreToolUse",
      tool_name: "Bash",
      tool_input: { command: event.input.command },
      cwd: ctx.cwd,
    };

    // enforce-sdlc-gates.py: block ungated `gh pr create`.
    const enforce = await runHook("enforce-sdlc-gates.py", payload, ctx.cwd);
    if (enforce.exitCode !== 0) {
      return gateConfirmation(
        enforce.stderr,
        "SDLC gate incomplete — block PR?",
        "Run /sdlc:gate to finish the chain, then retry.",
        ctx,
      );
    }

    // block-direct-gate-record.py: block manual stamping of skill-gated gates.
    // Mirror its own cheap pre-filter so we don't spawn python on every Bash call.
    const command = event.input.command;
    if (/record-gate|auto-record-skill-gate/.test(command)) {
      const direct = await runHook("block-direct-gate-record.py", payload, ctx.cwd);
      if (direct.exitCode !== 0) {
        return gateConfirmation(
          direct.stderr,
          "Direct gate recording blocked",
          "Run the skill itself; the gate records when it finishes.",
          ctx,
        );
      }
    }

    if (enforce.systemMessage) ctx.ui.notify(enforce.systemMessage, "info");
    return;
  });

  // PostToolUse Bash: auto-init a gate cycle on the first commit to a branch.
  pi.on("tool_result", async (event, ctx) => {
    if (!isBashToolResult(event)) return;
    const command = event.input.command as string;
    // Conservative pre-filter: never skip a real commit. `git -C <wt> commit`,
    // `git commit`, aliases (`ci`), all match; over-matching only costs a
    // wasted python spawn on a near-commit, under-matching would ship an
    // ungated branch.
    if (!/\bgit\b/.test(command) || !/commit|\bci\b/.test(command)) return;
    const out = await runHook(
      "auto-init-gate-cycle.py",
      {
        hook_event_name: "PostToolUse",
        tool_name: "Bash",
        tool_input: { command },
        tool_response: { is_error: event.isError },
        cwd: ctx.cwd,
      },
      ctx.cwd,
    );
    if (out.stderr.trim()) ctx.ui.notify(out.stderr.trim(), "warning");
  });
}
