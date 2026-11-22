# Plan: fix duplicate merge losing the body

## Scope
`merge_duplicate()` archives the duplicate without copying its body into the
survivor. Copy the body before archiving. Files: `src/notes/merge.py`,
`src/notes/test_merge.py`.

## Approach
Append the duplicate's body to the survivor's, separated by a rule, then
archive. One test for the copy, one for an empty body.
