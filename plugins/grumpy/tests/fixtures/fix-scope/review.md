# Code Review: fix duplicate merge losing the body

## Critical Issues 🚨

- [errors]: The nightly ingest job that calls merge_duplicate has no run lock, so two runs can archive the same duplicate twice and lose data [src/notes/ingest.py:40]

## Serious Concerns ⚠️

- [code]: `save(survivor)` runs before `archive(duplicate)`; if archive raises, the body is duplicated on the next merge [src/notes/merge.py:8]
- [errors] (judgment): The merge emits no structured event, so there is no way to alert when merges fail; add a `notes.merged` event and a red exit for the monitor [src/notes/merge.py:5]

## Questionable Decisions 🤔

- [tests]: No test for a duplicate whose body is only whitespace [src/notes/test_merge.py:12]

## Verdict

Fix and reship.
