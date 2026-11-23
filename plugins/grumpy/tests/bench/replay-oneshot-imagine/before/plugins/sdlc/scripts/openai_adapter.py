#!/usr/bin/env python3
"""Gate commands, then grumpy's review on the OpenAI API.

Usage: openai_adapter.py <capabilities.json> [--grumpy <grumpy plugin root>]

Runs the manifest's test/lint/typecheck commands like generic_adapter.py, then
grumpy's no-agent review (`oneshot.py review --provider openai`): grumpy's own
prompts, a report in .sharpen/grumpy/<branch>/review.md and a receipt that
`record-gate.py --verify review` records gate 3 from. The diff leaves this
machine for OpenAI; local_llm_adapter.py keeps it local.

Requires OPENAI_API_KEY and grumpy (GRUMPY_PLUGIN_ROOT or --grumpy). The model
is --model's job in oneshot.py: SHARPEN_LLM_MODEL, the manifest's
provider.model, else grumpy's models.yaml. OPENAI_MODEL still works and warns.
Pure stdlib.
"""

import sys

import generic_adapter as ga


def main(argv):
    return ga.run_with_oneshot(
        argv,
        "openai-adapter",
        "openai",
        legacy_env={"OPENAI_MODEL": "SHARPEN_LLM_MODEL"},
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
