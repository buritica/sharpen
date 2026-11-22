/**
 * grumpy under pi — exports `GRUMPY_PLUGIN_ROOT`, nothing else.
 *
 * grumpy has no hooks to port. Its passes do need one script of their own,
 * `scripts/receipt.py`, which each pass runs last to leave the receipt the
 * sdlc gate chain records from. The shared `CLAUDE_PLUGIN_ROOT` belongs to
 * sdlc's extension (it points at the sdlc root), so grumpy gets a DEDICATED
 * var, the same way sdlc-guardrails does with `GUARDRAILS_PLUGIN_ROOT`. The
 * passes resolve `${GRUMPY_PLUGIN_ROOT:-$CLAUDE_PLUGIN_ROOT}`, which still
 * works under Claude Code and Codex, which set `CLAUDE_PLUGIN_ROOT` per plugin.
 *
 * Node stdlib only.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

/** `plugins/grumpy`, from this file at extensions/. */
process.env.GRUMPY_PLUGIN_ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

export default function (_pi: ExtensionAPI): void {}
