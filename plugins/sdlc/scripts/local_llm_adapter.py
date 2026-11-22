#!/usr/bin/env python3
"""Gate commands, then grumpy's review on a local OpenAI-compatible server.

Usage: local_llm_adapter.py <capabilities.json> [--grumpy <grumpy plugin root>]

Runs the manifest's test/lint/typecheck commands like generic_adapter.py, then
grumpy's no-agent review (`oneshot.py review --provider compatible`) against
any server that speaks OpenAI's chat-completions format (Ollama, llama.cpp,
vLLM, LM Studio): grumpy's own prompts, a report in
.sharpen/grumpy/<branch>/review.md and a receipt that
`record-gate.py --verify review` records gate 3 from.

Requires SHARPEN_LLM_URL (e.g. http://localhost:11434/v1), a model
(SHARPEN_LLM_MODEL or the manifest's provider.model: there is no default) and
grumpy (GRUMPY_PLUGIN_ROOT or --grumpy). SHARPEN_LLM_API_KEY if the server
wants one. LOCAL_LLM_URL, LOCAL_LLM_MODEL and LOCAL_LLM_API_KEY still work and
warn. Pure stdlib.
"""

import sys

import generic_adapter as ga

LEGACY_ENV = {
    "LOCAL_LLM_URL": "SHARPEN_LLM_URL",
    "LOCAL_LLM_MODEL": "SHARPEN_LLM_MODEL",
    "LOCAL_LLM_API_KEY": "SHARPEN_LLM_API_KEY",
}


def main(argv):
    return ga.run_with_oneshot(
        argv, "local-llm-adapter", "compatible", legacy_env=LEGACY_ENV
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
