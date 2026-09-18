/**
 * sdlc-guardrails under pi — replicates the PreToolUse main-branch protection
 * hook and its interactive confirmation.
 *
 * The deny logic lives entirely in `hooks/_block-main-commits.py` (opt-in per
 * repo via `_guardrails_config.py` / `/guard on`, bypassable per command with
 * `SDLC_ALLOW_MAIN=1`). This adapter translates pi's `tool_call` (bash) event
 * into the Claude PreToolUse payload that script reads, and turns a deny into
 * an interactive confirmation — the pi analog of Claude's PreToolUse modal.
 *
 * Only an interactive human in the TUI can override (equivalent to the
 * `SDLC_ALLOW_MAIN=1` escape hatch surfacing as a prompt). Headless
 * (print/rpc) mode has no human to ask, so it fails closed and blocks.
 * `SDLC_ALLOW_MAIN=1` on the command itself still allows, exactly as in
 * Claude Code — the script already handles it.
 *
 * Also exports `CLAUDE_PLUGIN_ROOT` into the process env so existing
 * SKILL.md instructions that reference it resolve under pi's bash tool.
 *
 * Node stdlib only.
 */

import type {
  ExtensionAPI,
  ExtensionContext,
  ToolCallEventResult,
} from "@earendil-works/pi-coding-agent";
import { isToolCallEventType } from "@earendil-works/pi-coding-agent";
import { PLUGIN_ROOT, runGuard } from "./lib/run-hook";

process.env.CLAUDE_PLUGIN_ROOT = PLUGIN_ROOT;

async function guardConfirmation(
  reason: string,
  ctx: ExtensionContext,
): Promise<ToolCallEventResult | undefined> {
  const clean = reason.trim() || "Protected-branch commit blocked.";
  if (ctx.hasUI && ctx.mode === "tui") {
    const proceed = await ctx.ui.confirm(
      "Protected-branch commit blocked",
      `${clean}\n\nProceed anyway? This is the interactive override for the SDLC_ALLOW_MAIN=1 escape hatch — only a human can grant it.`,
    );
    if (proceed) {
      ctx.ui.notify(`Protected-branch guard overridden by human — continuing.`, "warning");
      return undefined;
    }
  }
  return { block: true, reason: clean, terminate: true };
}

export default function (pi: ExtensionAPI) {
  pi.on("tool_call", async (event, ctx) => {
    if (!isToolCallEventType("bash", event)) return;
    // Cheap pre-filter before spawning python: only git commit/push can touch
    // a protected branch. Over-matching only costs a spawn; never skip a real one.
    const command = event.input.command;
    if (!/\bgit\b/.test(command) || !/(commit|push)\b/.test(command)) return;
    const { exitCode, reason } = await runGuard(command, ctx.cwd);
    if (exitCode !== 0) {
      return guardConfirmation(reason, ctx);
    }
    return;
  });
}
