# Stack → toolchain reference

Maps a detected ecosystem to its *idiomatic* tooling and the GitHub building
blocks `/sdlc:init` needs to instantiate the patterns in `spec.md`. This is a
**starting reference, not a closed set** — toolchains move (uv displaced
pip/poetry; biome displaced prettier+eslint for many). Always reconcile against
what the repo already does, and **research when the stack isn't here or the table
looks dated** (see the protocol at the bottom).

Per stack, you need: **detect** (manifest), **install + cache**, **format**,
**lint**, **typecheck/static-analysis**, **test**, **build**, **setup action**.

## Node / TypeScript
- **Detect:** `package.json`. PM from lockfile — `bun.lock*`→bun, `pnpm-lock.yaml`→pnpm, `yarn.lock`→yarn, `package-lock.json`→npm.
- **Format/lint:** biome (modern default) or eslint + prettier if already present.
- **Typecheck:** `tsc --noEmit` when `tsconfig.json` exists.
- **Test:** repo's `test` script (vitest/jest/bun test/node --test).
- **Setup/cache:** `oven-sh/setup-bun` or `actions/setup-node` with `cache:`; cache key on the lockfile.
- **Reference:** `examples/node-bun/`.

## Python
- **Detect:** `pyproject.toml`, `requirements*.txt`, `setup.py`. PM — `uv.lock`→uv, `poetry.lock`→poetry, else pip.
- **Format/lint:** ruff (`ruff format` + `ruff check`) is the modern default; black+flake8/isort if already configured.
- **Typecheck:** mypy or pyright/ty when configured.
- **Test:** pytest (or `python -m unittest`).
- **Setup/cache:** `astral-sh/setup-uv` (cache built in) or `actions/setup-python` with `cache: pip`.

## Go
- **Detect:** `go.mod`.
- **Format:** `gofmt -l` / `go fmt` (fail if diff).
- **Lint:** `golangci-lint` (`golangci/golangci-lint-action`).
- **Static:** `go vet`.
- **Build:** `go build ./...` (required — see "Compiled languages" below).
- **Test:** `go test ./...` (`-race` where sensible).
- **Setup/cache:** `actions/setup-go` with `cache: true`.

## Rust
- **Detect:** `Cargo.toml`.
- **Format:** `cargo fmt --check`.
- **Lint:** `cargo clippy -- -D warnings`.
- **Build:** `cargo build` (required — see "Compiled languages" below).
- **Test:** `cargo test`.
- **Setup/cache:** `dtolnay/rust-toolchain` + `Swatinem/rust-cache`.

## Ruby
- **Detect:** `Gemfile`. **Format/lint:** rubocop. **Test:** rspec/minitest. **Setup:** `ruby/setup-ruby` (bundler cache).

## Java / Kotlin
- **Detect:** `pom.xml`→maven, `build.gradle*`→gradle. **Lint/format:** spotless/ktlint/checkstyle. **Build:** `mvn -B package -DskipTests` / `gradle assemble` (required — see "Compiled languages"). **Test:** `mvn test` / `gradle test`. **Setup:** `actions/setup-java` with build-tool cache.

## .NET
- **Detect:** `*.csproj`/`*.sln`. **Format:** `dotnet format --verify-no-changes`. **Build:** `dotnet build` (required — see "Compiled languages"). **Test:** `dotnet test`. **Setup:** `actions/setup-dotnet`.

## Elixir
- **Detect:** `mix.exs`. **Format:** `mix format --check-formatted`. **Lint:** credo. **Test:** `mix test`. **Setup:** `erlef/setup-beam` + deps/_build cache.

## PHP
- **Detect:** `composer.json`. **Format/lint:** php-cs-fixer/phpcs. **Static:** phpstan/psalm. **Test:** phpunit/pest. **Setup:** `shivammathur/setup-php`.

---

## Compiled languages — always emit a build job

For compiled stacks (Go, Rust, JVM, .NET, and anything that produces a binary),
a **build job is a core check, not optional** — a repo can be fmt/lint/test-clean
and still fail to compile on a path the tests don't exercise. Always generate a
gated build job (`go build ./...`, `cargo build`, `gradle assemble`,
`dotnet build`, …). Interpreted stacks (Node without a build step, Python, Ruby)
skip this unless the repo has its own build/bundle step.

## Polyglot / monorepos

Detect **all** manifests, not just the first. For a monorepo, prefer one path
bucket + gated job per package/area (e.g. `services/api/**`, `web/**`) so each
language's checks run only when that area changes. If the repo uses a monorepo
tool (turbo, nx, moon, bazel), wire CI around *its* affected-detection rather
than reinventing path filters.

## Research protocol — when the stack isn't listed or the table looks dated

Do **not** guess or ship a stale default. Instead:

1. **Read the repo first.** Existing CI, Makefile/Justfile/Taskfile, contributing
   docs, and `package.json`/`pyproject.toml` scripts usually name the exact
   tools the maintainers use. Match those before importing an opinion.
2. **Research current practice.** Use WebSearch/WebFetch to confirm the *current*
   idiomatic formatter/linter/test runner and the maintained GitHub setup action
   for that ecosystem (toolchains and actions get deprecated — verify currency).
   Prefer official docs and the action's own README; note the version.
3. **State your derivation.** Before writing files, tell the user: detected
   stack(s), the toolchain you chose, and *why* (repo evidence vs. researched
   default). Let them correct it — they may have house preferences.
4. **Then instantiate** the `spec.md` patterns with that toolchain.
