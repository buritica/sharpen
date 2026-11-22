/**
 * grumpy under pi — exports `GRUMPY_PLUGIN_ROOT`, nothing else.
 *
 * grumpy has no hooks to port. Its passes do need files of their own
 * (`scripts/receipt.py`, `scripts/simplify_policy.py`, `models.yaml`). pi runs
 * every plugin in one process, so each plugin exports a DEDICATED root var
 * (`SDLC_PLUGIN_ROOT`, `GUARDRAILS_PLUGIN_ROOT`, this one) and none sets the
 * shared `CLAUDE_PLUGIN_ROOT`. The passes resolve
 * `${GRUMPY_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT}}`; on Claude Code the braced
 * inner name is filled in when the skill loads. See docs/hosts.md.
 *
 * Node stdlib only.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

/** `plugins/grumpy`, from this file at extensions/. */
process.env.GRUMPY_PLUGIN_ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

export default function (_pi: ExtensionAPI): void {}
