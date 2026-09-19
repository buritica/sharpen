/**
 * Run a sdlc-guardrails hook script under pi.
 *
 * pi has no Claude `PreToolUse` event; the guard logic lives entirely in the
 * pure-stdlib `_block-main-commits.py` script (single source of truth). This
 * adapter feeds it the Claude PreToolUse payload on stdin, runs it, and turns
 * its exit code / stderr back into a pi decision. Denial = exit 2 + a
 * `{"decision":"deny","reason":...}` envelope on stderr; anything else allows.
 *
 * Node stdlib only — the repo's "pure stdlib" constraint, in TS.
 */

import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

/** `plugins/sdlc-guardrails` — the plugin root, from this file at extensions/lib/. */
export const PLUGIN_ROOT = dirname(dirname(dirname(fileURLToPath(import.meta.url))));

/** The guard script lives directly under the plugin's `hooks/` dir. */
export const HOOKS_DIR = join(PLUGIN_ROOT, "hooks");

export interface GuardResult {
  /** 2 = denied (exit 2 is the deny contract); 0 = allow. */
  exitCode: number;
  reason: string;
}

/** Extract the `reason` from the deny envelope, falling back to raw stderr. */
function parseReason(stderr: string): string {
  const trimmed = stderr.trim();
  if (!trimmed) return trimmed;
  try {
    const parsed = JSON.parse(trimmed);
    if (parsed && typeof parsed.reason === "string") return parsed.reason;
  } catch {
    /* not the JSON envelope — use raw stderr */
  }
  return trimmed;
}

/** Run `_block-main-commits.py` with a PreToolUse payload on stdin. */
export function runGuard(command: string, cwd: string): Promise<GuardResult> {
  return new Promise((resolve) => {
    const child = spawn("python3", [join(HOOKS_DIR, "_block-main-commits.py")], {
      cwd,
      env: process.env,
    });
    let stderr = "";
    child.stderr.on("data", (d: Buffer) => (stderr += d.toString()));
    child.on("error", (err) =>
      resolve({ exitCode: 1, reason: String(err) }),
    );
    child.on("close", (code) => {
      resolve({ exitCode: code ?? 0, reason: parseReason(stderr) });
    });
    child.stdin.on("error", () => {
      /* ignore */
    });
    child.stdin.write(
      JSON.stringify({
        hook_event_name: "PreToolUse",
        tool_name: "Bash",
        tool_input: { command },
        cwd,
      }),
    );
    child.stdin.end();
  });
}
