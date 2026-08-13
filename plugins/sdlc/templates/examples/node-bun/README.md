# Reference example: Node + bun + biome

This is **one concrete instantiation** of `../../spec.md` — not the canonical
output of `/sdlc:init`. It shows what the patterns look like for a bun/biome/tsc
repo. For any other stack, `/sdlc:init` derives the equivalent from `spec.md`
+ `stacks.md` (researching when needed), not by copying these files.

- `reusable-setup.yml` — pattern 3 (reusable setup) with `oven-sh/setup-bun` + bun cache.
- `pr-validate.yml` — pattern 1 (PR validation) with the path gate, concurrency, and bun commands.
- `biome.json` — pattern 6 formatter/linter config.
- `githooks/{pre-commit,pre-push}` — pattern 6 hooks; `pre-commit` auto-formats staged files with biome.

Read these to understand the *shape*; generate the stack-correct version rather
than assuming bun.
