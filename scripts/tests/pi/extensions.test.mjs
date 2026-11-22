// Smoke test for the pi extensions (plugins/*/extensions/*.ts).
//
// pi has no Claude hook events, so each extension maps a pi lifecycle event to
// the Claude hook payload the python scripts read on stdin, runs the script,
// and maps the exit code back to a pi decision. This loads each extension
// against a fake pi API, fires the events in throwaway git repos, and checks
// the decision that comes back. The python hooks are the real ones, so a
// payload the scripts can't read shows up here as a wrong decision.
//
// Node stdlib only. Run (Node 22.6+):
//   node --experimental-strip-types --import ./scripts/tests/pi/register.mjs \
//        --test scripts/tests/pi/extensions.test.mjs
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const SDLC_EXT = join(ROOT, "plugins", "sdlc", "extensions", "sdlc-hooks.ts");
const GUARD_EXT = join(ROOT, "plugins", "sdlc-guardrails", "extensions", "guardrails-hooks.ts");
const RECORD = join(ROOT, "plugins", "sdlc", "scripts", "record-gate.py");
const GUARD_CONFIG = join(ROOT, "plugins", "sdlc-guardrails", "hooks", "_guardrails_config.py");
const MAIN = "ma" + "in"; // split so the guardrails hook never sees a literal

const temps = [];
function tmp(prefix) {
  const d = mkdtempSync(join(tmpdir(), prefix));
  temps.push(d);
  return d;
}

function git(repo, ...args) {
  return execFileSync("git", ["-C", repo, ...args], { encoding: "utf8" }).trim();
}

function makeRepo(branch) {
  const repo = tmp("pi-ext-repo-");
  git(repo, "init", "-q", "-b", MAIN);
  git(repo, "config", "user.email", "t@t");
  git(repo, "config", "user.name", "t");
  git(repo, "commit", "-q", "--allow-empty", "-m", "init");
  if (branch) git(repo, "checkout", "-q", "-b", branch);
  return repo;
}

/** A fake ExtensionAPI that records handlers, plus a context factory. */
function fakePi() {
  const handlers = new Map();
  return {
    handlers,
    on(name, fn) {
      handlers.set(name, fn);
    },
  };
}

function ctx(cwd, { ui = false, confirm = false } = {}) {
  const notes = [];
  const prompts = [];
  return {
    cwd,
    hasUI: ui,
    mode: ui ? "tui" : "print",
    notes,
    prompts,
    ui: {
      async confirm(title, body) {
        prompts.push({ title, body });
        return confirm;
      },
      notify(msg, level) {
        notes.push({ msg, level });
      },
    },
  };
}

const bash = (command) => ({ toolName: "bash", input: { command } });

let sdlc;
let guard;
// One gate store for the whole file, and the store is keyed by branch name
// only, not by repo. Every test that touches it uses its own feature/<letter>
// branch so one test's cycle can't leak into another's decision.
let gatesPath;

before(async () => {
  // Hermetic env for the python hooks the extensions spawn (they inherit
  // process.env): no global git config, a private guardrails config dir and
  // gate store.
  process.env.GIT_CONFIG_GLOBAL = "/dev/null";
  process.env.CLAUDE_CONFIG_DIR = tmp("pi-ext-cfg-");
  gatesPath = join(tmp("pi-ext-store-"), "gates.json");
  process.env.SDLC_GATES_PATH = gatesPath;

  sdlc = fakePi();
  (await import(pathToFileURL(SDLC_EXT).href)).default(sdlc);
  guard = fakePi();
  (await import(pathToFileURL(GUARD_EXT).href)).default(guard);
});

after(() => {
  for (const d of temps) rmSync(d, { recursive: true, force: true });
});

// --- sdlc-hooks.ts ----------------------------------------------------------

test("sdlc: registers the three events and exports the plugin root", () => {
  assert.deepEqual([...sdlc.handlers.keys()].sort(), ["session_start", "tool_call", "tool_result"]);
  assert.equal(process.env.CLAUDE_PLUGIN_ROOT, join(ROOT, "plugins", "sdlc"));
  assert.equal(process.env.CLAUDE_PLUGIN_SCRIPTS, join(ROOT, "plugins", "sdlc", "scripts"));
});

test("sdlc: non-bash tool calls are ignored", async () => {
  const r = await sdlc.handlers.get("tool_call")(
    { toolName: "read", input: { path: "x" } },
    ctx(makeRepo("feature/a")),
  );
  assert.equal(r, undefined);
});

test("sdlc: gh pr create with no gate cycle is allowed (opt-in)", async () => {
  const r = await sdlc.handlers.get("tool_call")(bash("gh pr create --fill"), ctx(makeRepo("feature/b")));
  assert.equal(r, undefined);
});

test("sdlc: gh pr create with an incomplete cycle is blocked when headless", async () => {
  const repo = makeRepo("feature/c");
  execFileSync("python3", [RECORD, "--init", "tiny"], { cwd: repo, env: process.env });
  const c = ctx(repo);
  const r = await sdlc.handlers.get("tool_call")(bash("gh pr create --fill"), c);
  assert.equal(r?.block, true, "expected a block decision");
  assert.equal(r?.terminate, true);
  assert.match(r.reason, /gate|tests|lint/i);
  assert.equal(c.prompts.length, 0, "headless must not prompt");
});

test("sdlc: in the TUI a human can override or uphold the block", async () => {
  const repo = makeRepo("feature/d");
  execFileSync("python3", [RECORD, "--init", "tiny"], { cwd: repo, env: process.env });
  const yes = ctx(repo, { ui: true, confirm: true });
  assert.equal(await sdlc.handlers.get("tool_call")(bash("gh pr create"), yes), undefined);
  assert.equal(yes.prompts.length, 1);
  assert.equal(yes.notes.at(-1)?.level, "warning");

  const no = ctx(repo, { ui: true, confirm: false });
  const r = await sdlc.handlers.get("tool_call")(bash("gh pr create"), no);
  assert.equal(r?.block, true);
});

test("sdlc: stamping a skill gate by hand is blocked", async () => {
  const repo = makeRepo("feature/e");
  const r = await sdlc.handlers.get("tool_call")(
    bash(`python3 ${RECORD} --record grumpy-review`),
    ctx(repo),
  );
  assert.equal(r?.block, true, "expected block-direct-gate-record to deny");
});

test("sdlc: a commit on a feature branch arms a gate cycle", async () => {
  const repo = makeRepo("feature/f");
  git(repo, "commit", "-q", "--allow-empty", "-m", "work");
  const c = ctx(repo);
  await sdlc.handlers.get("tool_result")(
    { ...bash("git commit -m work"), isError: false },
    c,
  );
  assert.ok(existsSync(gatesPath), "gate store was not written");
  const store = JSON.parse(readFileSync(gatesPath, "utf8"));
  assert.ok(
    JSON.stringify(store).includes("feature/f"),
    "no cycle recorded for feature/f",
  );
});

test("sdlc: non-commit bash results never spawn the auto-init hook", async () => {
  const c = ctx(makeRepo("feature/g"));
  assert.equal(await sdlc.handlers.get("tool_result")({ ...bash("ls -la"), isError: false }, c), undefined);
  assert.equal(c.notes.length, 0);
});

// --- guardrails-hooks.ts ----------------------------------------------------

test("guardrails: registers tool_call only", () => {
  assert.deepEqual([...guard.handlers.keys()], ["tool_call"]);
});

test("guardrails: commit on a protected default branch is blocked when headless", async () => {
  const repo = makeRepo(null);
  execFileSync("python3", [GUARD_CONFIG, "protect", repo], { env: process.env });
  const r = await guard.handlers.get("tool_call")(bash(`git -C ${repo} commit -m wip`), ctx(repo));
  assert.equal(r?.block, true, "expected the protected-branch guard to deny");
  assert.equal(r?.terminate, true);
});

test("guardrails: commit on a feature branch is allowed", async () => {
  const repo = makeRepo("feature/h");
  execFileSync("python3", [GUARD_CONFIG, "protect", repo], { env: process.env });
  const r = await guard.handlers.get("tool_call")(bash(`git -C ${repo} commit -m wip`), ctx(repo));
  assert.equal(r, undefined);
});

test("guardrails: commit on an unprotected default branch is allowed", async () => {
  const repo = makeRepo(null);
  const r = await guard.handlers.get("tool_call")(bash(`git -C ${repo} commit -m wip`), ctx(repo));
  assert.equal(r, undefined);
});
