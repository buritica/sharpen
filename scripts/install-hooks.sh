#!/usr/bin/env bash
# Wire the repo's git hooks. Run once after cloning.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
git -C "$ROOT" config core.hooksPath .githooks
find "$ROOT/.githooks" -maxdepth 1 -type f -exec chmod +x {} +
echo "hooks installed → .githooks/ (core.hooksPath set)"
