#!/usr/bin/env python3
"""Discover and run all test_*.py suites in each plugin's tests/ subdirectory,
plus this repo's own top-level scripts/tests/ (for scripts/*.py that aren't
part of any plugin, like check-marketplace.py)."""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMEOUT = 120  # seconds per suite


def _test_files(d):
    if not os.path.isdir(d):
        return []
    return [
        os.path.join(d, f)
        for f in sorted(os.listdir(d))
        if f.startswith("test_") and f.endswith(".py")
    ]


def find_tests():
    tests = _test_files(os.path.join(ROOT, "scripts", "tests"))
    plugins_dir = os.path.join(ROOT, "plugins")
    if not os.path.isdir(plugins_dir):
        print(f"plugins/ not found at {plugins_dir}", file=sys.stderr)
        return tests
    for plugin in sorted(os.listdir(plugins_dir)):
        tests += _test_files(os.path.join(plugins_dir, plugin, "tests"))
    return tests


def main():
    tests = find_tests()
    if not tests:
        print("no test files found — nothing to run", file=sys.stderr)
        return 0
    failed = []
    for t in tests:
        rel = os.path.relpath(t, ROOT)
        print(f"\n=== {rel} ===")
        proc = subprocess.Popen([sys.executable, t], cwd=ROOT)
        try:
            proc.wait(timeout=TIMEOUT)
            if proc.returncode != 0:
                failed.append(rel)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            print(f"TIMEOUT after {TIMEOUT}s")
            failed.append(f"{rel} (timeout)")
    if failed:
        print(f"\n{len(failed)}/{len(tests)} test suite(s) failed:")
        for f in failed:
            print(f"  ✗ {f}")
        return 1
    print(f"\nall {len(tests)} test suite(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
