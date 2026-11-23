#!/usr/bin/env python3
"""Gate commands, then grumpy's review and imagine on a local OpenAI-compatible
server.

Usage: local_llm_adapter.py <capabilities.json> [--grumpy <grumpy plugin root>]

Runs the manifest's test/lint/typecheck commands like generic_adapter.py, then
grumpy's no-agent passes the manifest declares (`oneshot.py review` and
`oneshot.py imagine`, `--provider compatible`) against any server that speaks
OpenAI's chat-completions format (Ollama, llama.cpp, vLLM, LM Studio):
grumpy's own prompts, a report in .sharpen/grumpy/<branch>/<pass>.md and a
receipt that `record-gate.py --verify review|imagine` records gate 3 or 5
from.

Requires SHARPEN_LLM_URL (e.g. http://localhost:11434/v1), a model
(SHARPEN_LLM_MODEL or the manifest's provider.model: there is no default) and
grumpy 2.16+, 2.17+ for imagine (GRUMPY_PLUGIN_ROOT or --grumpy). SHARPEN_LLM_API_KEY if the server
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
