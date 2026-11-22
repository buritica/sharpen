#!/usr/bin/env python3
"""Run the pi extension smoke test (scripts/tests/pi/extensions.test.mjs)
from scripts/run-tests.py, so the local suite covers the TypeScript adapters
too.

Needs Node 22.6+ (for --experimental-strip-types). Without it this prints a
visible SKIP and exits 0: the python suite must stay runnable on any box with
python3. CI does not depend on this wrapper; its `pi` job installs Node and
runs the test directly, so a missing Node can't turn into a silent pass there.
"""

import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
MIN_NODE = (22, 6)

# ponytail: this invocation is also spelled out in ci.yml's `pi` job and in
# docs/pi.md (CI runs it directly so a missing Node can't skip there). Move
# all three behind one script the next time any copy of it changes.
PI_TEST_CMD = [
    "node",
    "--experimental-strip-types",
    "--no-warnings",
    "--import",
    "./scripts/tests/pi/register.mjs",
    "--test",
    "scripts/tests/pi/extensions.test.mjs",
]


def node_version(node):
    try:
        out = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.match(r"v(\d+)\.(\d+)", out.strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def main():
    node = shutil.which("node")
    version = node_version(node) if node else None
    if version is None or version < MIN_NODE:
        found = "not found" if version is None else "v%d.%d" % version
        print(
            "SKIP pi extension smoke test: needs Node %d.%d+ (%s)"
            % (MIN_NODE + (found,))
        )
        return 0
    return subprocess.run(PI_TEST_CMD, cwd=ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())
