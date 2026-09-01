---
name: test-gaps
description: "Find coverage holes — changed source files with no covering tests in the current diff."
---

# /sdlc:test-gaps

Find changed source files that have no covering tests. This is not about
coverage percentage — it is about identifying code that was added or modified in
the current branch but has no test file that exercises it at all. A 100% green
CI with untested changed code is still a liability.

## 0. Parse arguments

From `$ARGUMENTS`:
- `--base <branch>` → diff against this branch instead of the resolved default
  (see step 2 — a bare `origin/main` guess breaks on `master`-default repos).
- `--worktree <path>` (alias `--path <path>`) → operate in that worktree.
  Set `WT` to that path; otherwise `WT` is cwd. Use `git -C "$WT"` for all
  git operations.

## 1. Detect the stack

Read manifests in `$WT` to determine the language(s) and test file conventions.
Consult `${CLAUDE_PLUGIN_ROOT}/templates/stacks.md` for the full
reference. Do not hardcode assumptions — test-file naming varies by stack:

| Stack | Source extensions | Test file conventions |
|---|---|---|
| Node / TS | `.ts`, `.tsx`, `.js`, `.jsx` | `*.test.ts`, `*.spec.ts`, `__tests__/` |
| Python | `.py` | `test_*.py`, `*_test.py`, `tests/` directory |
| Go | `.go` | `*_test.go` (same package) |
| Rust | `.rs` | `#[cfg(test)]` blocks in same file, or `tests/` directory |
| Ruby | `.rb` | `*_spec.rb`, `test_*.rb` |
| Elixir | `.ex`, `.exs` | `*_test.exs` |
| PHP | `.php` | `*Test.php`, `tests/` directory |
| Java / Kotlin | `.java`, `.kt` | `*Test.java`, `*Test.kt`, `test/` source set |

For polyglot repos, detect all stacks and apply the appropriate conventions per
file extension.

Announce the detected stack(s) before proceeding.

## 2. Get the changed source files

```bash
WT="${WT:-.}"
if [ -z "$BASE" ]; then
  # Resolve the default branch dynamically — mirrors the same fallback chain
  # auto-init-gate-cycle.py uses. A bare "origin/main" guess breaks with
  # "fatal: ambiguous argument" on a master-default repo, and nothing below
  # checks that exit code, so a hardcoded guess here silently produces "no
  # source files changed" on a repo that has plenty.
  BASE=$(git -C "$WT" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)
  if [ -z "$BASE" ]; then
    for candidate in origin/main origin/master main master; do
      git -C "$WT" rev-parse --verify -q "$candidate" >/dev/null 2>&1 && { BASE="$candidate"; break; }
    done
  fi
fi
if [ -z "$BASE" ]; then
  echo "Could not resolve a base branch (no origin/HEAD symref, no origin/main," \
       "origin/master, main, or master). Pass --base <branch> explicitly." >&2
  exit 1
fi
git -C "$WT" diff --name-only "$BASE"...HEAD | grep -v '^$'
```

Unlike the diff-scoped grumpy commands (which have staged/unstaged/`HEAD~1`
fallback priorities to fall through to), this command has no other diff
source — an unresolved `$BASE` here means stop and say so, not silently run
`git diff --name-only ...HEAD` (which bash would collapse to `HEAD...HEAD`,
exit 0, empty output — indistinguishable from "no changed source files" and
reported as a clean bill of health on a repo that may have plenty).

Filter to source files only — exclude test files, config, documentation, and
generated files from the gap analysis (they are not "production code that needs
tests"):

```bash
# Exclude known test and non-source paths — adapt to the detected stack.
# Every test pattern is anchored (directory boundary, filename prefix, or
# filename suffix before the extension) rather than a bare substring match —
# a bare "test" would also drop real source like attestation.py, contest.ts,
# or latest.go. Likewise `scripts/` is anchored to the repo root, not any
# directory named scripts/ anywhere in the tree (a nested plugins/*/scripts/
# holding real production code should not be excluded).
git -C "$WT" diff --name-only "$BASE"...HEAD \
  | grep -vE '\.(md|json|yaml|yml|toml|lock|txt|env|svg|png|jpg|gif|ico|woff|css|scss)$' \
  | grep -vE '(^|/)(tests?|__tests__|specs?|fixtures?|mocks?|fakes?|stubs?|snapshots?)/' \
  | grep -vE '(^|/)test_[^/]*$' \
  | grep -vE '[._-](test|spec)\.[a-zA-Z0-9]+$' \
  | grep -vE '(Test|Spec)\.(php|java|kt)$' \
  | grep -vE '(^|/)(\.github|\.husky|docs|dist|build|coverage|node_modules)(/|$)' \
  | grep -vE '^scripts/'
```

If the filtered list is empty, report "No source files changed" and exit.

## 3. Map each changed file to its expected test location(s)

For each changed source file, derive one or more canonical test file paths using
the stack's conventions. Examples:

- `src/auth/login.ts` → `src/auth/login.test.ts`, `src/auth/__tests__/login.ts`,
  `tests/auth/login.test.ts`
- `internal/store/cache.go` → `internal/store/cache_test.go`
- `app/models/user.py` → `tests/models/test_user.py`, `app/models/test_user.py`

For Rust, also search for `#[cfg(test)]` blocks within the source file itself.

## 4. Check whether tests exist

For each changed source file:

**Step A — Look for a dedicated test file:**

```bash
# Example for TypeScript (adapt per stack)
SOURCE_FILE="src/auth/login.ts"
STEM=$(basename "$SOURCE_FILE" | sed 's/\.[^.]*$//')
DIR=$(dirname "$SOURCE_FILE")

# Check canonical locations
find "$WT" -type f \( \
  -path "*/${STEM}.test.*" \
  -o -path "*/${STEM}.spec.*" \
  -o -path "*/__tests__/${STEM}.*" \
  -o -path "*/tests/*${STEM}*" \
\) 2>/dev/null | grep -v node_modules
```

**Step B — Search for the module name or key exports in any test file:**

```bash
# Grep for imports or references to this module across all test files
grep -r --include="*.test.*" --include="*.spec.*" \
  -l "$(basename "$SOURCE_FILE" .ts)" "$WT" 2>/dev/null | grep -v node_modules
```

A source file is considered **covered** if either step finds at least one hit.
A source file with no hits in either step is a **gap**.

## 5. Assess gap severity

Not all gaps are equal. For each gap, add a severity label:

| Severity | Condition |
|---|---|
| **HIGH** | New file — this code was never tested |
| **MEDIUM** | Existing file with significant additions (>10 new lines per `git diff`) |
| **LOW** | Existing file with minor changes (cosmetic, rename, reorder) |

Determine new vs. existing files:

```bash
git -C "$WT" diff --name-only --diff-filter=A "$BASE"...HEAD  # Added files
git -C "$WT" diff --name-only --diff-filter=M "$BASE"...HEAD  # Modified files
```

## 6. Report

Print a summary grouped by severity:

```
Coverage gaps in changed code (vs <BASE>)
─────────────────────────────────────────────────────
HIGH — new files with no tests
  src/payments/stripe.ts           (new, 0 test files found)
  src/auth/mfa.ts                  (new, 0 test files found)

MEDIUM — modified files, significant additions, no covering test found
  src/users/profile.ts             (+47 lines, no test file)

LOW — modified files, minor changes, no covering test found
  src/utils/format.ts              (+3 lines, cosmetic)

COVERED — changed source files with tests found
  src/api/routes.ts                → src/api/routes.test.ts ✓
─────────────────────────────────────────────────────
Gaps: <N high> HIGH, <N med> MEDIUM, <N low> LOW
```

## 7. Suggest next steps

For each HIGH or MEDIUM gap, suggest a minimal test approach:

- State what the file exports or does (read the first 30 lines to infer intent).
- Suggest 1–3 test cases that would meaningfully cover the change (happy path,
  error branch, edge input).
- Do NOT write the tests — only suggest. The developer knows the domain.

Example suggestion:
> `src/payments/stripe.ts` — exports `chargeCard(amount, token)`.
> Suggest testing: (1) successful charge returns receipt id, (2) network error
> throws `PaymentError`, (3) amount ≤ 0 throws `ValidationError`.

## 8. Exit

- If no gaps: "All changed source files have covering tests. Good."
- If gaps exist, remind the user that `/sdlc:test-critique` reviews the quality of
  the tests that do exist — coverage gaps and low-quality tests are separate
  problems.
