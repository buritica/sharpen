#!/usr/bin/env python3
"""OpenAI portable-core adapter.

Extends the generic adapter with a real delegated review via OpenAI's API.
The review capability runs the model on the diff and produces findings;
other capabilities fall back to the generic shell-command behavior.

Requires: OPENAI_API_KEY. Optional: OPENAI_MODEL (default gpt-4o-mini).
Pure stdlib — no openai package needed.
"""

import json
import os
import sys
import urllib.request

import capabilities
import gate_store as gs
import generic_adapter as ga
import review_report


def _openai_chat(messages, model=None):
    """Minimal OpenAI chat completion via raw HTTP. Returns the content string."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("OPENAI_API_KEY is not set")
    model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "max_tokens": 4096,
            "temperature": 0.1,
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"]["content"]


def _extract_diff(cwd=None):
    """Get the diff for review. Prefers origin/main...HEAD, falls back to HEAD~1."""
    base = ga._detect_base(cwd)
    head = ga._detect_head(cwd)
    try:
        return ga._git("diff", f"{base}...{head}", cwd=cwd)
    except Exception:
        try:
            return ga._git("diff", "HEAD~1", cwd=cwd)
        except Exception:
            return ""


def _generic_build_review_report(manifest, gate_results, cwd=None):
    """Call the generic adapter's review builder."""
    return ga.build_review_report(manifest, gate_results, cwd)


REVIEW_PROMPT = """You are a code reviewer. Review this diff and return a JSON array of findings.

Each finding: {{"severity": "nit"|"suggestion"|"serious", "summary": "...", "location": "file:line or file", "consequence": "why it matters"}}.

Return [] if the diff is clean. Return only the JSON array, no other text.

Diff:
```diff
{diff}
```"""


def _parse_findings(response):
    """Extract a JSON array of findings from a model response.

    The response is the content string from the API, not the raw HTTP body.
    The model may wrap the JSON in prose; we look for the first [ ... ] block
    that parses as a JSON array.
    """
    for i, char in enumerate(response):
        if char == "[":
            for j in range(len(response), i, -1):
                if response[j - 1] == "]":
                    try:
                        parsed = json.loads(response[i:j])
                        if isinstance(parsed, list):
                            return parsed
                    except (json.JSONDecodeError, ValueError):
                        pass
    return []


def build_review_report(manifest, gate_results, cwd=None):
    """Run a real delegated review via OpenAI, falling back to generic on failure."""
    provider = manifest.get("provider", {})
    failures = [r for r in gate_results if r["exit_code"] != 0]

    # If gates already failed, don't waste tokens on review — report the failures
    if failures:
        return _generic_build_review_report(manifest, gate_results, cwd)

    try:
        diff = _extract_diff(cwd)
        response = _openai_chat(
            [{"role": "user", "content": REVIEW_PROMPT.format(diff=diff or "(no diff)")}],
            model=provider.get("model"),
        )
        findings = _parse_findings(response)
        # Normalize severity values
        valid = {"nit", "suggestion", "serious"}
        normalized = []
        for f in findings:
            if not isinstance(f, dict):
                continue
            if f.get("severity") not in valid:
                f["severity"] = "suggestion"
            normalized.append(f)
        findings = normalized
    except Exception as e:
        # Fall back to generic review with a warning finding
        findings = [
            {
                "severity": "serious",
                "summary": "delegated review failed",
                "location": "openai_adapter.py",
                "consequence": str(e),
            }
        ]

    import review_report

    review_failed = any(f.get("summary") == "delegated review failed" for f in findings)
    return review_report.validate_report(
        {
            "protocol_version": "1",
            "status": "fail" if review_failed or any(f.get("severity") == "serious" for f in findings) else "pass",
            "provenance": {
                "kind": "git-range",
                "base": ga._detect_base(cwd),
                "head": ga._detect_head(cwd),
            },
            "executor": {
                "agent": provider.get("agent", "openai-adapter"),
                "model": provider.get("model", os.environ.get("OPENAI_MODEL", "gpt-4o-mini")),
                "adapter": provider.get("name", "openai"),
            },
            "findings": findings,
        }
    )


def main(argv):
    manifest_path = argv[0] if argv else os.environ.get("SHARPEN_CAPABILITIES")
    if not manifest_path:
        ga._log("usage: openai_adapter.py <capabilities.json>")
        ga._log("       or set SHARPEN_CAPABILITIES")
        return 2

    try:
        manifest = capabilities.load_manifest(manifest_path)
    except ValueError as e:
        ga._log(f"[openai-adapter] error: {e}")
        return 2

    decision = capabilities.resolve_profile(manifest["capabilities"])
    if decision["decision"] != "selected":
        ga._log(f"[openai-adapter] error: {decision['reason']}")
        return 2
    profile = decision["resolved_profile"]
    ga._log(f"[openai-adapter] resolved profile: {profile}")

    branch = gs.detect_branch()
    if not branch:
        ga._log("[openai-adapter] error: not on a named branch")
        return 2

    results = ga.run_gates(manifest, profile)
    failures = [r for r in results if r["exit_code"] != 0]

    for r in results:
        status = "PASS" if r["exit_code"] == 0 else "FAIL"
        ga._log(f"[openai-adapter] {status} {r['capability']} ({r['duration_s']}s)")

    report = build_review_report(manifest, results)

    path = gs.default_store_path()
    try:
        gs.update_store(path, lambda d: review_report.attach_report(d, branch, report))
        ga._log(f"[openai-adapter] attached review report for {branch}")
    except ValueError as e:
        ga._log(f"[openai-adapter] warning: could not attach review report: {e}")

    if failures:
        ga._log(f"[openai-adapter] {len(failures)} gate(s) failed")
        return 1
    ga._log("[openai-adapter] all gates passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
