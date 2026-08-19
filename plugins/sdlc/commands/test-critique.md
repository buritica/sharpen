---
description: "Adversarial review of test quality — assertions that can't fail, over-mocking, and skipped tests without justification."
argument-hint: "[--base <branch>] [--scope all|changed] [--worktree <path>]"
allowed-tools: ["Bash", "Read", "Grep", "Glob", "TaskCreate", "TaskUpdate"]
---

# /sdlc:test-critique

Review the quality of tests with the same skepticism a grumpy principal engineer
applies to production code. Green tests that don't actually verify behavior are
worse than no tests — they create false confidence. This command finds the
patterns that let bad code slip through.

You are reviewing tests with earned skepticism. You've seen mocks that mock the
thing being tested, assertions that pass on empty results, and `.skip` blocks
that have been "temporary" for two years. Find them.

## 0. Parse arguments

From `$ARGUMENTS`:
- `--base <branch>` → diff against this branch instead of the resolved
  default (see step 2 — a bare `origin/main` guess breaks on `master`-default
  repos). Only used when `--scope changed`.
- `--scope all|changed` → `changed` (default) reviews only test files touched in
  the current branch diff; `all` reviews every test file in the repo. Use `all`
  sparingly on large repos.
- `--worktree <path>` (alias `--path <path>`) → operate in that worktree.
  Set `WT` to that path; otherwise `WT` is cwd.

## 1. Detect the stack and test file conventions

Read manifests in `$WT`. Use the same detection logic as the other `/sdlc:test-*`
commands. Consult `${CLAUDE_PLUGIN_ROOT}/templates/stacks.md` for the
full reference.

Test file patterns by stack:
- **Node/TS:** `*.test.ts`, `*.spec.ts`, `__tests__/**`
- **Python:** `test_*.py`, `*_test.py`, `tests/**/*.py`
- **Go:** `*_test.go`
- **Rust:** `#[cfg(test)]` in source files, `tests/**/*.rs`
- **Ruby:** `*_spec.rb`, `test_*.rb`
- **Elixir:** `*_test.exs`
- **Java/Kotlin:** `*Test.java`, `*Test.kt`, `*Spec.kt`
- **PHP:** `*Test.php`

Announce the detected stack and scope before proceeding.

## 2. Collect the test files to review

**Scope: changed (default)**

```bash
WT="${WT:-.}"
if [ -z "$BASE" ]; then
  # Resolve the default branch dynamically — mirrors the same fallback chain
  # auto-init-gate-cycle.py uses. A bare "origin/main" guess breaks with
  # "fatal: ambiguous argument" on a master-default repo.
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
git -C "$WT" diff --name-only "$BASE"...HEAD \
  | grep -E '\.(test|spec)\.(ts|tsx|js|jsx)$|test_.*\.py$|.*_test\.(py|go|rb)$|.*Test\.(java|kt|php)$|.*_test\.exs$'
```

Adapt the grep pattern to the detected stack.

**Scope: all**

```bash
find "$WT" -type f \( \
  -name "*.test.ts" -o -name "*.test.tsx" \
  -o -name "*.spec.ts" -o -name "*.spec.tsx" \
  -o -name "*.test.js" -o -name "*.spec.js" \
  -o -name "test_*.py" -o -name "*_test.py" \
  -o -name "*_test.go" \
  -o -name "*_spec.rb" -o -name "test_*.rb" \
  -o -name "*Test.java" -o -name "*Test.kt" \
  -o -name "*_test.exs" \
  -o -name "*Test.php" \
\) 2>/dev/null \
  | grep -vE '(node_modules|dist|build|coverage|vendor|\.git)/'
```

If no test files are in scope, report it and exit.

## 3. Read and critique each test file

For each file in scope, Read it and apply the full critique checklist below.

### 3a. Assertions that can't fail

These are the most insidious — they look like tests but prove nothing.

Hunt for:

**Vacuous equality**
```
expect(result).toBeDefined()          # passes on any truthy value
expect(result).not.toBeNull()         # passes even if result is {}
expect(result).toBeTruthy()           # passes on 0, "", false — is that right?
assert result is not None             # python: only rules out None
```

Flag these when they are the ONLY assertion in a test, or when a meaningful
value assertion (e.g. `toBe(42)`, `toEqual({id: 1})`) would be possible but
wasn't written.

**Asserting the mock's return value**
```js
const mockFn = jest.fn().mockReturnValue(42);
// ...
expect(result).toBe(42);  // of course it's 42 — you set that
```

This proves the mock works, not the code under test.

**Empty collection passes**
```js
expect(result.items).toHaveLength(0);  // did the function actually run?
expect(errors).toEqual([]);            // was the happy path ever reached?
```

Flag empty-array/empty-object assertions unless the test explicitly set up a
scenario that should produce an empty result.

**`expect.anything()` / `ANY`**
```js
expect(spy).toHaveBeenCalledWith(expect.anything());
```
This passes even if the function was called with the wrong arguments.

### 3b. Over-mocking

Mocks that replace so much that the test doesn't test the actual code:

**Mocking the module under test**
```js
jest.mock('./auth');  // then testing auth — what exactly is being tested?
```

**Mocking every dependency until the system is hollow**

When the test stubs out 4+ collaborators for a function with 2 lines of logic,
it is testing that the plumbing was wired, not that the logic is correct.

**Mocking at the wrong layer**
```js
// Testing a database query builder but mocking the raw DB connection
// means the query never runs
jest.mock('pg');
```

Flag when a mock removes the thing that the test's title says it's testing.

**Snapshot tests with no context**
```js
expect(render(<Component />)).toMatchSnapshot();
```

Snapshots on complex components catch regressions but not logic errors.
Flag if: (a) the snapshot was committed empty, (b) the snapshot file is
auto-generated and never reviewed, or (c) every test in the file is a snapshot.

### 3c. Skipped tests without justification

```js
test.skip('...', ...)     // jest/vitest
xit('...', ...)           // jasmine / mocha
xtest('...', ...)
it.skip('...', ...)
```

```python
@pytest.mark.skip
@pytest.mark.xfail
unittest.skip(...)
```

```go
t.Skip(...)
```

```ruby
pending '...'
xit '...'
```

Flag every skipped test. A skip without a comment referencing a tracking issue
is a test that will never be un-skipped. Summarize: how many skips, do any have
justification, how long have they been skipped (check `git log -L` if the skip
predates the current diff).

### 3d. Test titles that don't describe behavior

```js
it('works', ...)
it('test 1', ...)
it('should do the thing', ...)
describe('utils', () => { it('runs', ...) })
```

A good title states a behavior: "returns 401 when token is expired",
"trims leading whitespace before saving". Flag titles that are too vague to
know what a failure means.

### 3e. Setup/teardown gaps

- Tests that mutate shared state (module-level variables, DB records) without
  cleanup in `afterEach` / `after` / `defer` / `t.Cleanup`.
- `beforeAll` that seeds data without a matching `afterAll` — these tests are
  ordering-dependent and potential flake seeds.
- Tests that create files, start servers, or open connections without cleanup.

### 3f. Test structure issues

- Multiple unrelated things under one `it`/`test` (these should be split).
- Tests with no `expect`/`assert` at all (the test can never fail).
- Copy-pasted test blocks differing only in a constant — should be parameterized.

## 4. Build the findings report

For each finding, record:
- **File** and line number
- **Category** (from 3a–3f above)
- **Severity**: HIGH (assertion can't fail / skip without issue / no asserts),
  MEDIUM (over-mocking / vague title / shared state), LOW (style / structure)
- **What's wrong** (one sentence, grumpy voice)
- **What it should be** (one sentence, actionable)

## 5. Output the critique

```
Test quality critique — <scope> (<N> files reviewed)
══════════════════════════════════════════════════════

HIGH — these tests cannot catch real regressions

  src/auth/login.test.ts:42
  Category: Assertion that can't fail
  ✗ expect(result).toBeDefined() is the only assertion. The test passes whether
    chargeCard returns a receipt, throws, or returns undefined garbage.
  → Assert the actual receipt shape: expect(result).toEqual({ id: expect.stringMatching(/^ch_/) })

  ...

MEDIUM — tests that erode confidence

  src/payments/stripe.test.ts:15–80
  Category: Over-mocking
  ✗ You mocked stripe.charges.create and then asserted it was called. You are
    testing that jest.fn() works, not that your payment flow works.
  → Use a stripe-mock or a recorded HTTP fixture. Test the behavior, not the wiring.

  ...

LOW — structural issues

  tests/utils.test.ts:3
  Category: Vague title
  ✗ it('works') — works at what? When does it not work? When this fails at 2am
    nobody will know what broke.
  → it('returns empty string when input is null')

  ...

SKIPPED TESTS
  src/auth/login.test.ts:88   test.skip('handles MFA timeout') — no issue reference
  src/payments/stripe.test.ts:120  it.skip('retries on 429')    — no issue reference
  → Each skip needs a link to a tracking issue or it will never be un-skipped.

══════════════════════════════════════════════════════
Summary: <N> HIGH, <N> MEDIUM, <N> LOW findings across <N> files.
         <N> skipped tests, <N> without justification.
```

## 6. Exit guidance

- If HIGH findings exist: these are the priority — tests that can't fail are
  dead weight.
- Suggest running `/sdlc:test-gaps` if the critique revealed that key behaviors
  have no test at all (different from a bad test — a missing test entirely).
- Suggest running `/sdlc:test-flake` if setup/teardown gaps were found — shared
  state is the #1 cause of flaky tests.
- If no findings: "Tests look solid. I'm almost impressed."
