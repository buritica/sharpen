---
description: "Rerun the test suite N times locally to surface intermittent failures; optionally file a GitHub issue for each flake found."
argument-hint: "[--runs N] [--file-issues] [--worktree <path>]"
allowed-tools: ["Bash", "Read", "Grep", "Glob", "Write", "TaskCreate", "TaskUpdate"]
---

# /sdlc:test-flake

Hunt for intermittent (flaky) tests by running the suite multiple times and
collecting failures that are not 100% reproducible. A test that fails in some
runs but not others is a flake — it erodes trust in CI and masks real regressions.

## 0. Parse arguments

From `$ARGUMENTS`:
- `--runs N` → repeat the suite `N` times (default: **5**).
- `--file-issues` → after the hunt, open a GitHub issue for each unique flake
  found (requires `gh` and a remote).
- `--worktree <path>` (alias `--path <path>`) → run against that worktree
  instead of cwd. Set `WT` to that path; otherwise `WT` is the current
  directory. Use `git -C "$WT"` for all git operations.

## 1. Detect the stack and test runner

Read manifests in `$WT` — do not assume bun. Follow the same detection order
used by `/sdlc:gate`:

```
bun.lockb / bunfig.toml                 → bun test --rerun-each <N>
package-lock.json / yarn.lock            → npm test   (no native rerun; wrap in a shell loop)
pnpm-lock.yaml                           → pnpm test  (no native rerun; wrap in a shell loop)
pyproject.toml / setup.py / setup.cfg   → pytest      (use pytest-repeat or shell loop)
Cargo.toml                               → cargo test  (shell loop)
go.mod                                   → go test ./... (shell loop)
Makefile with "test" target             → make test   (shell loop)
```

Read `${CLAUDE_PLUGIN_ROOT}/templates/stacks.md` for the full
toolchain reference, including how to detect the package manager from the
lockfile. For stacks not listed there, use WebSearch/WebFetch to confirm the
current idiomatic test runner before proceeding.

Announce the detected runner before running anything, e.g.:
> Detected: bun (bun.lockb present). Will use `bun test --rerun-each <N>`.

## 2. Choose the rerun strategy

### Bun (native rerun-each)

Bun's `--rerun-each N` flag re-executes **each individual test** `N` times in
the same process — it catches timing-sensitive flakes more reliably than running
the whole suite once per iteration because the entire suite runs `N` times per
test.

```bash
cd "$WT"
bun test --rerun-each "$RUNS" 2>&1 | tee /tmp/flake-run.log
```

### Node (npm / pnpm / yarn) — shell loop

These runners lack a built-in rerun flag. Run the full suite `N` times and
diff the results:

```bash
cd "$WT"
RUNS=5   # or the user-supplied value
FAIL_LOG=/tmp/flake-failures.log
: > "$FAIL_LOG"
for i in $(seq 1 "$RUNS"); do
  echo "=== Run $i ===" >> "$FAIL_LOG"
  npm test -- --reporter=verbose >> "$FAIL_LOG" 2>&1 || true
done
```

Adapt the runner (`npm` → `pnpm` / `yarn`) and the reporter flag to what the
repo's `package.json` `test` script accepts. If the script doesn't support
`--reporter`, omit it.

### pytest — shell loop or pytest-repeat

Check whether `pytest-repeat` is installed:

```bash
cd "$WT"
python -m pytest --co -q 2>/dev/null | head -5
pip show pytest-repeat 2>/dev/null && echo "pytest-repeat available" || echo "shell loop fallback"
```

With pytest-repeat:
```bash
python -m pytest --count="$RUNS" -v 2>&1 | tee /tmp/flake-run.log
```

Without it, shell loop:
```bash
FAIL_LOG=/tmp/flake-failures.log
: > "$FAIL_LOG"
for i in $(seq 1 "$RUNS"); do
  echo "=== Run $i ===" >> "$FAIL_LOG"
  python -m pytest -v >> "$FAIL_LOG" 2>&1 || true
done
```

### Go — shell loop

```bash
cd "$WT"
FAIL_LOG=/tmp/flake-failures.log
: > "$FAIL_LOG"
for i in $(seq 1 "$RUNS"); do
  echo "=== Run $i ===" >> "$FAIL_LOG"
  go test ./... -v -race -count=1 >> "$FAIL_LOG" 2>&1 || true
done
```

Note: `go test` caches results by default. Pass `-count=1` to bypass the cache
so each run actually executes. Consider `-race` to catch data races that
manifest as flakes.

### Rust — shell loop

```bash
cd "$WT"
FAIL_LOG=/tmp/flake-failures.log
: > "$FAIL_LOG"
for i in $(seq 1 "$RUNS"); do
  echo "=== Run $i ===" >> "$FAIL_LOG"
  cargo test -- --nocapture >> "$FAIL_LOG" 2>&1 || true
done
```

### make test — shell loop

```bash
cd "$WT"
FAIL_LOG=/tmp/flake-failures.log
: > "$FAIL_LOG"
for i in $(seq 1 "$RUNS"); do
  echo "=== Run $i ===" >> "$FAIL_LOG"
  make test >> "$FAIL_LOG" 2>&1 || true
done
```

## 3. Collect and deduplicate failures

After all runs complete, parse the log(s) to extract test names that failed in
**at least one run but not all runs**. Tests that fail in every run are
regressions, not flakes — call them out separately.

```bash
# Example extraction for bun / jest-style output — adapt to the runner's format
grep -E "✗|FAIL|FAILED|× " /tmp/flake-run.log | sort | uniq -c | sort -rn
```

Build two lists:
- **Flakes** — appeared in 1 to (N-1) runs.
- **Consistent failures** — appeared in all N runs (a real bug, not a flake).

If no failures appear, report "No flakes detected in $RUNS runs" and exit
cleanly.

## 4. Report

Print a summary table:

```
Flake hunt: <runner>, <N> runs
─────────────────────────────────────────────────
FLAKY  (intermittent)
  <test name>   failed <k>/<N> runs
  ...

CONSISTENTLY FAILING (real regression — fix first)
  <test name>   failed <N>/<N> runs
  ...

PASSED always: <count> tests
─────────────────────────────────────────────────
```

For each flake, add a brief diagnosis hint if you can infer it from the log
(timing dependency, shared state, missing teardown, random seed, port conflict,
etc.).

## 5. Optionally file GitHub issues

If `--file-issues` was passed AND `gh` is available AND flakes were found:

```bash
command -v gh >/dev/null 2>&1 || { echo "gh not found — skipping issue creation"; exit 0; }
```

For each unique flake:

```bash
gh issue create \
  --title "Flaky test: <test name>" \
  --body "$(cat <<'EOF'
## Flaky test detected by /sdlc:test-flake

**Test:** <test name>
**Runner:** <runner>
**Failure rate:** <k>/<N> runs
**Detected on branch:** <branch>
**Date:** <date>

### Suspected cause
<diagnosis hint or "Unknown — needs investigation">

### Reproduction
Run \`/sdlc:test-flake --runs 10\` locally and look for this test in the output.

### Next steps
- [ ] Identify the source of non-determinism (shared state, time, network, port, randomness)
- [ ] Add isolation (beforeEach teardown, fixed seed, retry once with a comment)
- [ ] Remove the flake or mark it explicitly with a tracking issue link
EOF
)" \
  --label "flaky-test,test-quality"
```

Print each created issue URL.

## 6. Exit guidance

- If flakes were found: suggest running with `--runs 10` or `--runs 20` to
  measure frequency more accurately before spending time fixing low-rate flakes.
- Remind the user that flakes with shared mutable state or timing dependencies
  are highest priority — they can cause silent data corruption or masking of
  real failures in CI.
- If consistent failures exist: fix those first; a broken suite drowns out flakes.
