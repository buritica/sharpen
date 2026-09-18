/**
 * Run an sdlc hook script under pi.
 *
 * pi has no Claude Code `PreToolUse`/`PostToolUse`/`SessionStart` hook events.
 * This repo keeps ALL gate logic in the pure-stdlib python scripts under
 * `scripts/` (single source of truth — the pi adapter must not fork it). The
 * adapter's only job is to translate a pi lifecycle event into the Claude
 * hook payload shape those scripts already read on stdin, run them, and turn
 * their exit code / stderr / stdout back into a pi decision.
 *
 * The scripts resolve their sibling imports (`gate_store`, `hook_out`,
 * `shell_parse`, ...) from their own directory on sys.path, so cwd does not
 * matter for imports — but cwd IS passed through so any script that wants the
 * session working directory (matching Claude's hooks, which run in the session
 * cwd) gets it.
 *
 * Node stdlib only — no runtime deps, matching the repo's "pure stdlib"
 * constraint (just in TypeScript, since pi extensions are TS).
 */

import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

/** `plugins/<name>` — the plugin root, from this file at extensions/lib/. */
export const PLUGIN_ROOT = dirname(dirname(dirname(fileURLToPath(import.meta.url))));

/** `plugins/<name>/scripts` (sdlc) — the hook scripts live here. */
export const SCRIPTS_DIR = join(PLUGIN_ROOT, "scripts");

export interface HookResult {
  /** Process exit code (0 = allow for PreToolUse hooks; 2 = deny). */
  exitCode: number;
  stdout: string;
  stderr: string;
  /**
   * The `systemMessage` from a Claude hook `warn` envelope on stdout — an
   * allow-time caveat meant for the user, surfaced via `ctx.ui.notify`.
   * Undefined when stdout is not (or does not contain) a warn envelope.
   */
  systemMessage?: string;
}

/**
 * Run one hook script, feeding it the Claude hook payload on stdin.
 *
 * @param script filename inside SCRIPTS_DIR (never a path — no traversal)
 * @param payload the Claude hook JSON payload (tool_name, tool_input, cwd, ...)
 * @param cwd session working directory passed to the child (and into the payload)
 */
export function runHook(
  script: string,
  payload: Record<string, unknown>,
  cwd: string,
): Promise<HookResult> {
  return new Promise((resolve) => {
    const child = spawn("python3", [join(SCRIPTS_DIR, script)], {
      cwd,
      env: process.env,
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d: Buffer) => (stdout += d.toString()));
    child.stderr.on("data", (d: Buffer) => (stderr += d.toString()));
    child.on("error", (err) =>
      resolve({ exitCode: 1, stdout, stderr: String(err) }),
    );
    child.on("close", (code) => {
      let systemMessage: string | undefined;
      try {
        const parsed = JSON.parse(stdout);
        if (parsed && typeof parsed.systemMessage === "string") {
          systemMessage = parsed.systemMessage;
        }
      } catch {
        /* stdout is not the warn envelope; allow-time notice is absent */
      }
      resolve({ exitCode: code ?? 1, stdout, stderr, systemMessage });
    });
    child.stdin.on("error", () => {
      /* ignore — the child may close stdin early */
    });
    child.stdin.write(JSON.stringify(payload));
    child.stdin.end();
  });
}
