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

Create a private scratch directory for this run's logs and segment files —
`FLAKE_TMPDIR=$(mktemp -d)`. Every path below that would otherwise be a fixed
`/tmp/flake-*` name lives under `$FLAKE_TMPDIR` instead: two concurrent
`/sdlc:test-flake` invocations (two sessions, or a retry before a prior run's
process tree exited) sharing a fixed path would truncate/overwrite each
other's logs and segment files mid-run, silently corrupting each other's
flake counts rather than erroring.

## 1. Detect the stack and test runner

Read manifests in `$WT` — do not assume bun. Follow the same detection order
used by `/sdlc:gate`:

```
bun.lock* / bunfig.toml                 → bun test --rerun-each <N>
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
> Detected: bun (bun.lock* present). Will use `bun test --rerun-each <N>`.

## 2. Choose the rerun strategy

### Bun (native rerun-each)

Bun's `--rerun-each N` flag re-executes **each individual test** `N` times in
the same process — it catches timing-sensitive flakes more reliably than running
the whole suite once per iteration because the entire suite runs `N` times per
test.

```bash
cd "$WT"
bun test --rerun-each "$RUNS" 2>&1 | tee "$FLAKE_TMPDIR/flake-run.log"
```

### Node (npm / pnpm / yarn) — shell loop

These runners lack a built-in rerun flag. Run the full suite `N` times and
diff the results:

```bash
cd "$WT"
RUNS=5   # or the user-supplied value
FAIL_LOG="$FLAKE_TMPDIR/flake-failures.log"
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
python -m pytest --count="$RUNS" -v 2>&1 | tee "$FLAKE_TMPDIR/flake-run.log"
```

Without it, shell loop:
```bash
FAIL_LOG="$FLAKE_TMPDIR/flake-failures.log"
: > "$FAIL_LOG"
for i in $(seq 1 "$RUNS"); do
  echo "=== Run $i ===" >> "$FAIL_LOG"
  python -m pytest -v >> "$FAIL_LOG" 2>&1 || true
done
```

### Go — shell loop

```bash
cd "$WT"
FAIL_LOG="$FLAKE_TMPDIR/flake-failures.log"
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
FAIL_LOG="$FLAKE_TMPDIR/flake-failures.log"
: > "$FAIL_LOG"
for i in $(seq 1 "$RUNS"); do
  echo "=== Run $i ===" >> "$FAIL_LOG"
  cargo test -- --nocapture >> "$FAIL_LOG" 2>&1 || true
done
```

### make test — shell loop

```bash
cd "$WT"
FAIL_LOG="$FLAKE_TMPDIR/flake-failures.log"
: > "$FAIL_LOG"
for i in $(seq 1 "$RUNS"); do
  echo "=== Run $i ===" >> "$FAIL_LOG"
  make test >> "$FAIL_LOG" 2>&1 || true
done
```

## 3. Collect and deduplicate failures

The `failed k/N runs` figure in Step 4 means *distinct runs*, not *failure
lines* — a naive grep-and-count over the whole log overcounts any test that
logs more than one failure line within a single run. How to count correctly
depends on which strategy Step 2 used:

**Shell-loop runners (Node/pytest-fallback/Go/Rust/make):** the loop wrote
`=== Run N ===` markers into `$FAIL_LOG`. Split on them first, dedupe failing
test names *within* each run's segment, then aggregate — that way a test
contributes at most once per run it actually failed in:

```bash
rm -f "$FLAKE_TMPDIR"/flake-run-*.seg
awk -v dir="$FLAKE_TMPDIR" \
  '/^=== Run [0-9]+ ===$/ { n++; next } n { print > (dir "/flake-run-" n ".seg") }' \
  "$FAIL_LOG"
shopt -s nullglob
segs=("$FLAKE_TMPDIR"/flake-run-*.seg)
shopt -u nullglob
if [ "${#segs[@]}" -eq 0 ]; then
  echo "No '=== Run N ===' segments found in $FAIL_LOG — the loop in Step 2 may not have run, or the log is empty. STOP: do not report 'no flakes detected' from this state — no run data was actually collected." >&2
  exit 1
fi
for seg in "${segs[@]}"; do
  # Example pattern for jest/bun-style output — adapt to the runner's format
  grep -oE "✗ .*|FAIL .*|FAILED .*|× .*" "$seg" | sort -u
done | sort | uniq -c | sort -rn
```

Passing `$FLAKE_TMPDIR` to `awk` via `-v` rather than interpolating it into
the single-quoted script is required — a shell variable reference inside
single quotes is not expanded by the shell, so `awk` would receive the
literal string `$FLAKE_TMPDIR` as part of the path. Without the
`nullglob`/array guard, a bare `for seg in "$FLAKE_TMPDIR"/flake-run-*.seg`
errors on "No such file or directory" when zero segments exist (e.g. the
wrong branch of Step 2 ran, or `$FAIL_LOG` is empty) — the unmatched glob
pattern is passed to `grep` literally instead of the loop simply not
iterating. The `exit 1` on zero segments matters as much as the glob fix
itself: a silent fall-through here would let a tired agent report "no flakes
detected" when the counting step actually failed to find any run data.

The count from `uniq -c` here is exactly "number of distinct runs this test
failed in" — each `seg` file contributes a given test name at most once,
because of the `sort -u` inside the loop.

**Bun's native `--rerun-each`:** there are no `=== Run N ===` markers — bun
re-executes each test N times inside one process and reports a pass/fail tally
per test in its own summary output. Read that tally directly instead of
imposing the run-marker model on it; bun already gives you the k/N count.

**pytest-repeat (`--count=N`):** also no `=== Run N ===` markers — the loop in
Step 2 only wraps the *shell-loop fallback*, not the pytest-repeat path.
Unlike bun/jest-style output, pytest emits exactly one `PASSED`/`FAILED`
summary line per test *execution* (not multiple failure-detail lines per
run), so counting occurrences directly from the `-v` log is already accurate
— no run-marker splitting needed:

```bash
grep -E '(PASSED|FAILED)' "$FLAKE_TMPDIR/flake-run.log" \
  | awk '{print $NF, $1}' \
  | sort | uniq -c | sort -rn
```

Adapt the column parsing to the pytest version's actual verbose-output
format (e.g. `test_foo.py::test_bar PASSED` vs `PASSED test_foo.py::test_bar`
depending on pytest version and plugins) before trusting the extracted counts.

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
