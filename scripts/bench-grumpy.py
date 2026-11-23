#!/usr/bin/env python3
"""Local benchmark for grumpy's review and imagine passes.

Runs the real pass headlessly on a host (Claude Code, Codex, pi, or grumpy's
no-agent `oneshot.py` against a chat API) against fixture repos (small planted
cases and real-size replays), scores the report the pass writes, records what
each run spent, and compares runs so
a model, host or prompt change shows up as a number. Local only: nothing here
runs in CI, and results land in the gitignored .sharpen/bench/.

    bench-grumpy.py run --host claude [--model ID[,ID...]] [--pass review|imagine|all]
                        [--mode auto|inline|fanout[,...]]
                        [--provider openai|compatible]
                        [--cases a,b] [--repeat N] [--invoke native|inline]
                        [--analysis haiku|sonnet|opus]
                        [--cmd TEMPLATE] [--timeout S] [--out DIR] [--keep]
    bench-grumpy.py score REPORT --case NAME
    bench-grumpy.py rescore RESULTS.json
    bench-grumpy.py compare BASELINE.json CANDIDATE.json [--cost-rise F]
    bench-grumpy.py commands --host codex [--model ID[,ID...]]
                        [--mode auto|inline|fanout[,...]] [--pass review]
    bench-grumpy.py cases

Fixtures live in plugins/grumpy/tests/bench/<case>/: a `before/` tree, an
`after/` tree and a `key.json` (see that directory's README). Line numbers in
a key refer to the `after/` files.

Scoring is deterministic: a finding matches a planted issue when it names the
same file, a line within WINDOW lines of the issue's range, and one of its
keywords (matched at a word start, case-insensitively). Matching is one to one, so a single finding can't claim two issues
and two findings can't both claim one. Findings that match an `acceptable`
entry (real, but not planted) don't count as noise unless they exceed that
entry's `max_severity`.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# How each host is invoked lives in bench_hosts.py; these names are part of
# this script's surface (the tests reach them as bench.<name>).
from bench_hosts import (  # noqa: E402,F401
    CLAUDE_ALIASES,
    HOSTS,
    MODES,
    USAGE_HOSTS,
    build_command,
    cli_version,
    observed_mode,
    oneshot_setup,
    oneshot_version,
    output_text,
    parse_usage,
    prompt_hash,
)

# Summarizing a run and comparing two live in bench_summary.py; the same
# goes for these names.
from bench_summary import compare, group_name, summarize  # noqa: E402,F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRUMPY = os.path.join(ROOT, "plugins", "grumpy")
CASES_DIR = os.path.join(GRUMPY, "tests", "bench")
RESULTS_DIR = os.path.join(ROOT, ".sharpen", "bench")
PASSES = ("review", "imagine")
WINDOW = 3
RANK = {"NOTE": 1, "WARN": 2, "CRIT": 3}
BLOCKING = {"CRIT", "WARN"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def list_cases(cases_dir=CASES_DIR):
    if not os.path.isdir(cases_dir):
        return []
    return sorted(
        d
        for d in os.listdir(cases_dir)
        if os.path.isfile(os.path.join(cases_dir, d, "key.json"))
    )


def load_key(case, cases_dir=CASES_DIR):
    with open(os.path.join(cases_dir, case, "key.json"), encoding="utf-8") as f:
        key = json.load(f)
    key.setdefault("planted", [])
    key.setdefault("acceptable", [])
    key.setdefault("expect_absent", [])
    key.setdefault("passes", list(PASSES))
    validate_key(key, case)
    return key


def validate_key(key, case="key"):
    """Raise ValueError on a key whose typo would silently skew scores."""
    ids = [t["id"] for t in key["planted"] + key["acceptable"]]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"{case}: duplicate ids {dupes}")
    for t in key["planted"]:
        if t.get("severity") not in RANK:
            raise ValueError(f"{case}: {t['id']} severity {t.get('severity')!r}")
    for t in key["acceptable"]:
        if "max_severity" in t and t["max_severity"] not in RANK:
            raise ValueError(f"{case}: {t['id']} max_severity {t['max_severity']!r}")
    planted_ids = {t["id"] for t in key["planted"]}
    unknown = sorted(set(key["expect_absent"]) - planted_ids)
    if unknown:
        raise ValueError(f"{case}: expect_absent names no planted issue: {unknown}")
    bad = sorted(set(key["passes"]) - set(PASSES))
    if bad:
        raise ValueError(f"{case}: unknown passes {bad}")


def _tree_files(root):
    out = {}
    for dirpath, _, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            out[os.path.relpath(full, root).replace(os.sep, "/")] = full
    return out


def build_repo(case, dest, cases_dir=CASES_DIR):
    """Make a git repo at dest: `before/` on main, `after/` committed on a branch.

    Returns the branch name. A real consumer repo ignores sharpen's artifacts,
    so the base commit does too.
    """
    base = os.path.join(cases_dir, case)
    key = load_key(case, cases_dir)
    branch = f"bench/{case}"

    # Isolated from the user's global and system git config (hooksPath,
    # templates, signing), so a personal pre-commit hook never runs here.
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

    def git(*args):
        proc = subprocess.run(
            ["git", "-C", dest, *args], capture_output=True, text=True, env=env
        )
        if proc.returncode:
            raise RuntimeError(
                f"git {' '.join(args)} failed in {dest}: {proc.stderr.strip()}"
            )

    os.makedirs(dest, exist_ok=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "bench@example.invalid")
    git("config", "user.name", "grumpy bench")
    git("config", "commit.gpgsign", "false")
    shutil.copytree(os.path.join(base, "before"), dest, dirs_exist_ok=True)
    with open(os.path.join(dest, ".gitignore"), "w", encoding="utf-8") as f:
        f.write(".claude/\n.sharpen/\n__pycache__/\n")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", branch)
    for rel in _tree_files(os.path.join(base, "before")):
        if not os.path.exists(os.path.join(base, "after", rel)):
            os.remove(os.path.join(dest, rel))
    shutil.copytree(os.path.join(base, "after"), dest, dirs_exist_ok=True)
    git("add", "-A")
    git("commit", "-q", "-m", key.get("description", case)[:72])
    # Prior pass output the case starts from (e.g. a fix report whose settled
    # list review must honor) goes where grumpy reads it. .sharpen/ is ignored,
    # so it leaves the worktree clean.
    art_dir = os.path.join(dest, ".sharpen", "grumpy", *branch.split("/"))
    for name, src in key.get("artifacts", {}).items():
        os.makedirs(art_dir, exist_ok=True)
        shutil.copyfile(os.path.join(base, src), os.path.join(art_dir, name))
    return branch


# ---------------------------------------------------------------------------
# Parsing findings
# ---------------------------------------------------------------------------
_LOC = re.compile(
    r"`?([A-Za-z0-9_./-]*[A-Za-z0-9_-]\.[A-Za-z0-9]+)`?(?::(\d+)(?:-(\d+))?)?"
)
_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
# Lists a report appends to the last severity section without a heading of
# their own: findings held back as settled or already deferred. They are not
# raised findings, so they end the section.
_HELD_BACK = re.compile(
    r"^\W*(settled findings held back|deferred findings honored)", re.IGNORECASE
)
_BULLET = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$")
# A placeholder bullet: "None.", "N/A", "Nothing to report", "No critical
# issues found." It must be the whole bullet, so "None is returned after
# retries" or "Nothing logs the failure" stay findings.
_EMPTY_BULLET = re.compile(
    r"^(none( found)?|n/?a|nothing( (to report|found|here|significant))?"
    r"|no( \w+)? (findings|issues|concerns|problems)( found)?( in this pass)?)\W*$",
    re.I,
)


def section_severity(heading):
    """Map a rendered report heading to a severity, or None to ignore it.

    Only the top-level severity sections count. review.md and imagine.md both
    aggregate every finding into them; the detail sections (simulations,
    strengths, verdict) repeat or explain, and would double count.
    """
    h = heading.lower()
    if "critical issues" in h or "bugs found" in h:
        return "CRIT"
    if "serious concerns" in h:
        return "WARN"
    if "questionable decisions" in h or "simplify this" in h:
        return "NOTE"
    return None


def parse_locations(text):
    """Every file reference in text, as {file, line, end}. line may be None."""
    locs = []
    for m in _LOC.finditer(text):
        name = m.group(1)
        if "/" not in name and not re.search(
            r"\.(py|ts|js|go|rs|rb|sql|md|json|ya?ml|toml|sh)$", name
        ):
            continue  # "e.g." / "v1.2" style false hits
        line = int(m.group(2)) if m.group(2) else None
        end = int(m.group(3)) if m.group(3) else line
        locs.append({"file": name, "line": line, "end": end})
    return locs


def has_severity_sections(markdown):
    """True when the report has at least one severity heading the scorer reads."""
    in_fence = False
    for raw in markdown.splitlines():
        if raw.strip().startswith("```"):
            in_fence = not in_fence
            continue
        heading = _HEADING.match(raw)
        if not in_fence and heading and len(heading.group(1)) <= 2:
            if section_severity(heading.group(2)):
                return True
    return False


def section_body_lines(markdown):
    """Lines inside severity sections that look like findings the parser
    skips: table rows, indented bullets, or prose citing a file:line.

    With zero parsed findings, a positive count means the parser dropped
    content: the run is 'unparsed', not quiet. Plain prose ("None worth
    raising: the rename is complete") is commentary, not a dropped finding.
    """
    severity, in_fence, count = None, False, 0
    for raw in markdown.splitlines():
        if raw.strip().startswith("```"):
            in_fence = not in_fence
            continue
        heading = _HEADING.match(raw)
        if heading and not in_fence:
            if len(heading.group(1)) <= 2:
                severity = section_severity(heading.group(2))
            continue
        if severity is None or not raw.strip():
            continue
        if _HELD_BACK.match(raw):
            severity = None
            continue
        bullet = _BULLET.match(raw)
        text = bullet.group(2).strip() if bullet else raw.strip()
        if _EMPTY_BULLET.match(text):
            continue
        if (
            raw.lstrip().startswith("|")
            or (bullet and len(bullet.group(1)) >= 2)
            or any(loc["line"] for loc in parse_locations(text))
        ):
            count += 1
    return count


def parse_report(markdown):
    """Findings from a rendered review.md or imagine.md report."""
    findings = []
    severity = None
    current = None
    in_fence = False
    for raw in markdown.splitlines():
        if raw.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        heading = _HEADING.match(raw)
        if heading:
            level = len(heading.group(1))
            if level <= 2:
                severity = section_severity(heading.group(2))
            current = None
            continue
        if severity is None:
            continue
        if _HELD_BACK.match(raw):
            severity = current = None
            continue
        bullet = _BULLET.match(raw)
        if bullet and len(bullet.group(1)) < 2:
            text = bullet.group(2).strip()
            if _EMPTY_BULLET.match(text):
                current = None
                continue
            current = {"severity": severity, "text": text}
            findings.append(current)
        elif current is not None and raw.strip():
            current["text"] += " " + raw.strip()
        elif not raw.strip():
            current = None
    for f in findings:
        f["locations"] = parse_locations(f["text"])
    return findings


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _same_file(a, b):
    a, b = a.strip("`./"), b.strip("`./")
    if not a or not b:
        return False
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def _keyword_in(keyword, text):
    """keyword occurs in text at a word start ("log" in "logging", not in
    "catalog"), ignoring case. Identifier parts count as word starts, so
    "factory" is in "FormatterFactory" and "smoke" in "test_smoke". A keyword
    that starts with punctuation, such as "`pass`", matches anywhere."""
    start = (
        r"(?:\b|(?<=_)|(?-i:(?<=[a-z])(?=[A-Z])))"
        if keyword[:1].isalnum() or keyword[:1] == "_"
        else ""
    )
    return re.search(start + re.escape(keyword), text, re.IGNORECASE) is not None


def matches(target, finding, window=WINDOW):
    """Same file, a line inside the target's range +/- window, and a keyword.

    A finding that names the file but no line matches on keyword alone, so
    keys must use keywords specific to the issue (a word start must match:
    "log" matches "logging" but not "catalog"). A keyword is required either
    way: planted issues sit a few lines apart, and a line window alone would
    let one finding claim its neighbour.
    """
    text = finding.get("text", "")
    keywords = target.get("keywords") or []
    if keywords and not any(_keyword_in(k, text) for k in keywords):
        return False
    lo, hi = target["lines"]
    lo, hi = lo - window, hi + window
    for loc in finding.get("locations", []):
        if not _same_file(loc.get("file", ""), target["file"]):
            continue
        line = loc.get("line")
        if line is None:
            return True
        end = loc.get("end") or line
        if line <= hi and end >= lo:
            return True
    return False


def _max_matching(edges, n_left, owner=None):
    """Kuhn's algorithm. edges[i] = right indices for left i. Returns {right: left}.

    `owner` seeds the matching; augmenting keeps every seeded right matched,
    though it may re-route a seeded right to another left to fit one more.
    """
    owner = dict(owner or {})

    def augment(i, seen):
        for j in edges[i]:
            if j in seen:
                continue
            seen.add(j)
            if j not in owner or augment(owner[j], seen):
                owner[j] = i
                return True
        return False

    matched_left = set(owner.values())
    for i in range(n_left):
        if i not in matched_left:
            augment(i, set())
    return owner


def _pair_findings(findings, planted, acceptable, window):
    """Pair findings with planted issues first, then leftovers with acceptable
    ones. Returns ({finding index: (kind, target index)}, planted edge lists)."""
    n = len(findings)
    p_edges = [
        [j for j, t in enumerate(planted) if matches(t, f, window)] for f in findings
    ]
    # Prefer pairings where the finding is rated at least as high as the
    # planted issue, so a CRIT and a NOTE that both describe two neighbouring
    # issues are paired the right way round; then fill in the rest. Filling in
    # can re-route a preferred pair when that matches one more issue: recall
    # comes first, severity agreement second.
    rated_ok = [
        [
            j
            for j in p_edges[i]
            if RANK.get(findings[i].get("severity"), 1)
            >= RANK.get(planted[j].get("severity", "NOTE"), 1)
        ]
        for i in range(n)
    ]
    p_owner = _max_matching(p_edges, n, owner=_max_matching(rated_ok, n))
    matched = {i: ("planted", j) for j, i in p_owner.items()}

    free = [i for i in range(n) if i not in matched]
    a_edges = [
        [j for j, t in enumerate(acceptable) if matches(t, findings[i], window)]
        for i in free
    ]
    a_owner = _max_matching(a_edges, len(free))
    for j, k in a_owner.items():
        matched[free[k]] = ("acceptable", j)
    acceptable_hit = {free[k] for k, edges in enumerate(a_edges) if edges}
    return matched, p_edges, acceptable_hit


def _rate_matches(matched, findings, planted, acceptable, absent=()):
    """(planted ids hit, planted ids underrated, acceptable ids overrated,
    [(expect_absent id, finding index)] that showed up anyway)."""
    hits, underrated, overrated, present = [], [], [], []
    for i, (kind, j) in matched.items():
        sev = findings[i].get("severity", "NOTE")
        if kind == "planted" and planted[j]["id"] in absent:
            present.append((planted[j]["id"], i))
        elif kind == "planted":
            hits.append(planted[j]["id"])
            if RANK.get(sev, 1) < RANK.get(planted[j].get("severity", "NOTE"), 1):
                underrated.append(planted[j]["id"])
        else:
            cap = acceptable[j].get("max_severity")
            if cap and RANK.get(sev, 1) > RANK[cap]:
                overrated.append(acceptable[j]["id"])
    return hits, underrated, overrated, present


def _classify_unmatched(findings, matched, p_edges, acceptable_hit):
    """Split the unmatched findings into (duplicates, unlocated, noise)."""
    unmatched = [i for i in range(len(findings)) if i not in matched]
    # An unmatched finding that describes an issue another finding already
    # claimed is a duplicate: a dedup miss, reported separately, not noise.
    duplicates = [i for i in unmatched if p_edges[i] or i in acceptable_hit]
    # A finding with no file reference can't be scored or acted on (the
    # report contract asks for [file:line]); it's a format miss, kept apart
    # from noise and out of the precision denominator.
    unlocated = [
        i
        for i in unmatched
        if i not in duplicates
        and not any(loc.get("file") for loc in findings[i].get("locations", []))
    ]
    noise = [i for i in unmatched if i not in duplicates and i not in unlocated]
    return duplicates, unlocated, noise


def score(key, findings, window=WINDOW):
    """Score findings against a case key. Planted issues are matched first.

    A planted id listed in `expect_absent` is one the pass must hold back
    (e.g. settled by an earlier fix): it is left out of recall, and a finding
    that raises it anyway fails the case.
    """
    planted = key.get("planted", [])
    acceptable = key.get("acceptable", [])
    absent = set(key.get("expect_absent", []))
    scored = [t for t in planted if t["id"] not in absent]
    n = len(findings)

    matched, p_edges, acceptable_hit = _pair_findings(
        findings, planted, acceptable, window
    )
    hits, underrated, overrated, present = _rate_matches(
        matched, findings, planted, acceptable, absent
    )
    duplicates, unlocated, noise = _classify_unmatched(
        findings, matched, p_edges, acceptable_hit
    )
    # Precision is over distinct, locatable blocking findings: a duplicate
    # (a dedup miss) or an unlocated finding (a format miss) is counted on its
    # own and compared on its own, never as a true positive.
    blocking = [
        i
        for i in range(n)
        if findings[i].get("severity") in BLOCKING
        and i not in unlocated
        and i not in duplicates
    ]
    noise_blocking = [i for i in noise if i in blocking]
    present_blocking = [i for _, i in present if i in blocking]
    false_blocking = len(noise_blocking) + len(overrated) + len(present_blocking)
    hit_set = set(hits)
    return {
        "recall": (len(hit_set) / len(scored)) if scored else None,
        "precision": ((len(blocking) - false_blocking) / len(blocking))
        if blocking
        else None,
        "findings": n,
        "blocking": len(blocking),
        "noise": len(noise),
        "duplicates": len(duplicates),
        "unlocated": len(unlocated),
        "noise_blocking": len(noise_blocking),
        "overrated": sorted(overrated),
        "underrated": sorted(underrated),
        "hits": sorted(hit_set),
        "misses": [t["id"] for t in scored if t["id"] not in hit_set],
        "present": sorted(pid for pid, _ in present),
        "noise_text": [findings[i]["text"][:160] for i in noise_blocking],
        "assigned": [
            _assignment(i, matched, planted, acceptable, duplicates, unlocated)
            for i in range(n)
        ],
        "clean": false_blocking == 0 and not present,
    }


def _assignment(i, matched, planted, acceptable, duplicates, unlocated):
    """How finding i was classified, for reading a run after the fact."""
    if i in matched:
        kind, j = matched[i]
        return f"{kind}:{(planted if kind == 'planted' else acceptable)[j]['id']}"
    if i in duplicates:
        return "duplicate"
    return "unlocated" if i in unlocated else "noise"


# ---------------------------------------------------------------------------
# Running a host
# ---------------------------------------------------------------------------
REPORT_TITLES = {"review": "# Code Review", "imagine": "# Production Imagination"}


def report_in_output(output, pass_name):
    """True when output contains the pass's rendered report: its title, or
    (under another title) the severity sections the scorer reads."""
    return any(
        line.startswith(REPORT_TITLES[pass_name]) for line in output.splitlines()
    ) or has_severity_sections(output)


def find_report(repo, branch, pass_name):
    expected = os.path.join(
        repo, ".sharpen", "grumpy", *branch.split("/"), f"{pass_name}.md"
    )
    # Only the branch's own folder: a report written anywhere else is a
    # contract miss, and a case can seed other files there via `artifacts`.
    return expected if os.path.isfile(expected) else None


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def _drain(proc):
    """What a killed host printed. Bounded: a grandchild that left the
    process group can hold the pipes open."""
    try:
        return proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        return "", ""


# ponytail: cyclomatic 21 against a bar of 22; split the report-scoring tail into its own function when radon cc reports run_one above 22
def run_one(
    case,
    pass_name,
    argv,
    stdin_text,
    out_dir,
    label,
    timeout,
    keep,
    cases_dir=CASES_DIR,
    grumpy=GRUMPY,
    analysis=None,
    host=None,
):
    key = load_key(case, cases_dir)
    repo = tempfile.mkdtemp(prefix=f"grumpy-bench-{case}-")
    result = {"case": case, "pass": pass_name, "label": label}
    try:
        branch = build_repo(case, repo, cases_dir)
        if analysis:
            # Review's passes follow models.yaml's `analysis` role, not the
            # session's --model; pin it through the repo override the passes
            # read. .sharpen/ is ignored in the fixture, so the tree stays clean.
            os.makedirs(os.path.join(repo, ".sharpen"), exist_ok=True)
            with open(
                os.path.join(repo, ".sharpen", "models.yaml"), "w", encoding="utf-8"
            ) as f:
                f.write(f"roles:\n  analysis:\n    native: {analysis}\n")
        # GRUMPY_PLUGIN_ROOT is what pi sets; Claude Code fills the braced
        # name into the prompt itself and never exports CLAUDE_PLUGIN_ROOT, so
        # neither does the bench. The parent's own host markers are dropped so
        # a pass doesn't detect the shell that launched the bench as its host.
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("CLAUDECODE", "CLAUDE_PLUGIN_ROOT", "SHARPEN_HOST")
            and not k.startswith("CLAUDE_CODE_")
        }
        env["GRUMPY_PLUGIN_ROOT"] = grumpy
        started = datetime.datetime.now(datetime.timezone.utc)
        try:
            # Its own session, so a timeout can kill the host's children
            # (Agent, Bash) too, not just the host, before the repo is removed.
            proc = subprocess.Popen(
                argv,
                cwd=repo,
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                start_new_session=True,
            )
        except FileNotFoundError:
            result["status"] = "cli-missing"
            result["error"] = f"{argv[0]} not found on PATH"
            return result
        try:
            stdout, stderr = proc.communicate(stdin_text, timeout=timeout)
            result["exit_code"] = proc.returncode
        except subprocess.TimeoutExpired:
            # A timed-out run is a failed run even if it left a report: a
            # half-written report would otherwise score as a clean one.
            _kill_group(proc)
            stdout, stderr = _drain(proc)
            result["exit_code"] = None
            result["status"] = "timeout"
        except KeyboardInterrupt:
            # The host runs in its own session, so the terminal's ^C never
            # reaches it: kill it here or it keeps billing in a deleted repo.
            _kill_group(proc)
            raise
        stdout, stderr = stdout or "", stderr or ""
        result["duration_s"] = round(
            (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds(), 1
        )
        with open(os.path.join(out_dir, f"{label}.log"), "w", encoding="utf-8") as f:
            f.write(stdout + "\n----- stderr -----\n" + stderr)
        # What the run spent and how its passes ran, recorded even for a
        # failed run. A oneshot timeout keeps the tokens it logged; a Claude
        # timeout records none (its JSON prints only at the end), and the
        # summary counts such runs as `unpriced` instead of hiding them.
        try:
            usage = parse_usage(host, stdout, stderr)
        except Exception as e:  # a shape change must not fail a paid, scorable run
            usage = None
            result["usage_error"] = f"{type(e).__name__}: {e}"
        if usage is not None:
            result["usage"] = usage
        mode = observed_mode(host, usage)
        if mode:
            result["observed_mode"] = mode
        printed = output_text(host, stdout)
        if result.get("status") == "timeout":
            return result
        if result["exit_code"]:
            # A rate limit, max turns or a crash after a partial report: a
            # failed run, not a scored one that reads as a prompt regression.
            result["status"] = "exit-nonzero"
            return result
        report = find_report(repo, branch, pass_name)
        saved = os.path.join(out_dir, f"{label}.md")
        if report:
            shutil.copyfile(report, saved)
            with open(report, encoding="utf-8", errors="replace") as f:
                text = f.read()
            findings = parse_report(text)
            result["status"] = "scored"
        elif report_in_output(printed, pass_name):
            # The host printed the report but couldn't save it (headless Claude
            # Code refused writes under .claude/ before grumpy 2.15). Score what
            # it printed, and say so: the pass's own write step failed.
            with open(saved, "w", encoding="utf-8") as f:
                f.write(printed)
            text = printed
            findings = parse_report(printed)
            result["status"] = "scored-from-output"
        else:
            result["status"] = "no-report"
            return result
        result["report"] = (
            os.path.relpath(saved, ROOT) if saved.startswith(ROOT) else saved
        )
        if not has_severity_sections(text) or (
            not findings and section_body_lines(text)
        ):
            # A renamed heading, a report in a fence, or bullets the parser
            # can't read: scoring that as a quiet, clean run would hide it.
            result["status"] = "unparsed"
            return result
        result["score"] = score(key, findings)
        return result
    finally:
        if keep:
            result["repo"] = repo
        else:
            shutil.rmtree(repo, ignore_errors=True)


def _fmt(v, pct=True):
    if v is None:
        return "-"
    return f"{v:.0%}" if pct else f"{v:.1f}"


def _money(v):
    return "-" if v is None else f"${v:.2f}"


def _ktok(v):
    return "-" if v is None else f"{v / 1000:,.0f}k"


def _secs(v):
    return "-" if v is None else f"{v:.0f}s"


def print_summary(summary, stream=sys.stdout, host=None):
    width = max([34, *(len(n) + 1 for n in summary)])
    print(
        f"{'case/pass':{width}} {'runs':>4} {'fail':>4} {'recall':>7} {'spread':>7} {'prec':>6} {'noise':>6} {'over':>5} {'unloc':>6} {'clean':>6}"
        f" {'cost':>7} {'tokens':>8} {'time':>6} {'agents':>6}",
        file=stream,
    )
    for name, s in summary.items():
        print(
            f"{name:{width}} {s['runs']:>4} {s['failed_runs']:>4} {_fmt(s['recall']):>7} {_fmt(s['recall_spread']):>7} "
            f"{_fmt(s['precision']):>6} {_fmt(s['noise_blocking'], False):>6} {_fmt(s['overrated'], False):>5} {_fmt(s['unlocated'], False):>6} {_fmt(s['clean_rate']):>6}"
            f" {_money(s.get('cost_usd')):>7} {_ktok(s.get('tokens')):>8} {_secs(s.get('duration_s')):>6} {_fmt(s.get('subagents'), False):>6}",
            file=stream,
        )
    for name, s in summary.items():
        for note in _row_notes(s, host):
            print(f"note: {name}: {note}", file=stream)


def _row_notes(s, host):
    """What the table can't show about a row's measurements."""
    notes = []
    priced = s.get("cost_usd") is not None or s.get("tokens") is not None
    if s.get("unpriced") and priced:
        notes.append(
            f"{s['unpriced']}/{s['runs']} run(s) recorded no usage"
            " (a Claude run that times out records none); cost and tokens"
            " are the mean of the rest"
        )
    elif s.get("unpriced") == s.get("runs") and host in USAGE_HOSTS:
        notes.append(
            f"no run recorded usage, though {host} reports it; read a run's .log"
            " (a --cmd without --output-format json records none)"
        )
    if s.get("partial_usage"):
        notes.append(
            f"{s['partial_usage']}/{s['runs']} run(s) reported usage for only some"
            " calls; their tokens are low"
        )
    if s.get("mode_unknown"):
        notes.append(
            f"{s['mode_unknown']} run(s) recorded usage but not how the passes ran"
            " (no subagent_stats in Claude's JSON)"
        )
    if s.get("mode_missed"):
        notes.append(
            f"{s['mode_missed']} run(s) ignored the pinned --mode; their row's"
            " mode label is wrong for them"
        )
    return notes


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _passes(arg):
    return list(PASSES) if arg == "all" else [arg]


def _selected_cases(arg):
    available = list_cases()
    if not arg:
        return available
    wanted = [c.strip() for c in arg.split(",") if c.strip()]
    unknown = [c for c in wanted if c not in available]
    if unknown:
        raise SystemExit(
            f"error: unknown case(s): {', '.join(unknown)} (have: {', '.join(available)})"
        )
    return wanted


def _run_labelled(
    case, pass_name, argv, stdin_text, out_dir, label, args, variant=None
):
    """One run_one call, bracketed by progress lines on stderr. `variant`
    is (model, mode) for a matrix run, None otherwise."""
    print(f"[bench] {label} ...", file=sys.stderr, flush=True)
    model, mode = variant or (
        _list(getattr(args, "model", None))[0],
        _list(getattr(args, "mode", None))[0] or "auto",
    )
    analysis = _analysis_pin(args, model)
    try:
        r = run_one(
            case,
            pass_name,
            argv,
            stdin_text,
            out_dir,
            label,
            args.timeout,
            args.keep,
            analysis=analysis,
            host=getattr(args, "host", None),
        )
    except Exception as e:  # one broken run must not discard the paid-for rest
        r = {"case": case, "pass": pass_name, "label": label, "status": "error"}
        r["error"] = f"{type(e).__name__}: {e}"
    if variant:
        r["model"], r["mode"] = variant
        r["variant"] = variant_name(*variant)
    if analysis:
        r["analysis"] = analysis
    if mode in ("inline", "fanout"):
        r["pinned_mode"] = mode
    s = r.get("score")
    u = r.get("usage") or {}
    spent = "".join(
        [
            f" cost={_money(u['cost_usd'])}" if u.get("cost_usd") is not None else "",
            f" tokens={_ktok(u['tokens'])}" if u.get("tokens") is not None else "",
            f" ran={r['observed_mode']}" if r.get("observed_mode") else "",
        ]
    )
    if s:
        detail = f" recall={_fmt(s['recall'])} noise={s['noise_blocking']}{spent}"
    else:
        detail = f" exit={r.get('exit_code')}{spent}" + (
            f" {r['error']}" if r.get("error") else f" (see {label}.log)"
        )
    print(f"[bench] {label}: {r.get('status')}{detail}", file=sys.stderr, flush=True)
    return r


def _list(arg):
    """'a,b' -> ['a', 'b']; None or '' -> [None] (the host default). A
    repeated item is dropped: two identical variants would share log names."""
    items = [x.strip() for x in (arg or "").split(",") if x.strip()]
    return list(dict.fromkeys(items)) or [None]


def _analysis_pin(args, model):
    """The alias to pin models.yaml's analysis role to, or None. --analysis
    wins; otherwise a Claude run on an alias pins analysis to it, so under
    fanout review's passes run on the model the row names, as they do inline
    (where the session runs them itself)."""
    if getattr(args, "analysis", None):
        return args.analysis
    if getattr(args, "host", None) == "claude" and model in CLAUDE_ALIASES:
        return model
    return None


def variant_name(model, mode):
    return f"{model or 'default'}/{mode or 'auto'}"


def _variants(args):
    """[(model, mode)]: every --model with every --mode."""
    modes = _list(getattr(args, "mode", None))
    bad = sorted({m for m in modes if m and m not in MODES})
    if bad:
        raise ValueError(
            f"unknown mode(s) {', '.join(bad)} (one of: {', '.join(MODES)})"
        )
    models = _list(args.model)
    cmd = getattr(args, "cmd", None)
    if cmd and len(models) > 1 and "{model}" not in cmd:
        raise ValueError(
            "--cmd has no {model}, so every model in --model would run the same command"
        )
    variants = [(m, mode or "auto") for m in models for mode in modes]
    # Labels fold punctuation to `_` and may land on a case-insensitive disk:
    # two variants with one label would overwrite each other's logs.
    seen = {}
    for v in variants:
        slug = _variant_tags(v)[0].lower()
        if slug in seen:
            raise ValueError(
                f"variants {variant_name(*seen[slug])} and {variant_name(*v)}"
                " would share log names; rename one"
            )
        seen[slug] = v
    return variants


def _plan(args, invoke, provider):
    """[(case, pass, argv, stdin, variant)] for every run, or ValueError for
    an invocation the host doesn't have. `variant` is (model, mode) when the
    run compares several, None for a plain run."""
    plan = []
    variants = _variants(args)
    matrix = len(variants) > 1
    for case in _selected_cases(args.cases):
        key = load_key(case)
        for pass_name in _passes(args.pass_):
            if pass_name not in key["passes"]:
                continue
            for model, mode in variants:
                argv, stdin_text = build_command(
                    args.host,
                    pass_name,
                    model,
                    invoke,
                    args.cmd,
                    provider=provider,
                    mode=mode,
                )
                plan.append(
                    (
                        case,
                        pass_name,
                        argv,
                        stdin_text,
                        (model, mode) if matrix else None,
                    )
                )
    return plan


def _run_id(args, provider, stamp):
    """The results directory name: stamp, host, model(s) and any mode(s)."""
    host_id = f"{args.host}-{provider}" if provider else args.host
    models = "+".join(m or "default" for m in _list(args.model)).replace("/", "_")
    modes = [m or "auto" for m in _list(args.mode)]
    modes = "" if modes == ["auto"] else "-" + "+".join(modes)
    return f"{stamp}-{host_id}-{models}{modes}"


def _variant_tags(variant):
    """(label slug, command tag) for a matrix variant; ('', '') otherwise."""
    if not variant:
        return "", ""
    name = variant_name(*variant)
    return "-" + re.sub(r"[^A-Za-z0-9.]+", "_", name), f" @{name}"


def cmd_run(args):
    profile = HOSTS[args.host]
    provider, problem = oneshot_setup(args)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    invoke = args.invoke or profile["default_invoke"]
    # Everything that can fail before a paid session does, before the first,
    # and a bad flag is named before a missing binary.
    try:
        plan = _plan(args, invoke, provider)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    binary = shlex.split(args.cmd)[0] if args.cmd else profile["binary"]
    if not shutil.which(binary):
        print(f"error: {binary} is not on PATH", file=sys.stderr)
        return 2
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = _new_dir(
        os.path.join(args.out or RESULTS_DIR, _run_id(args, provider, stamp))
    )
    print(f"[bench] results: {os.path.relpath(out_dir)}", file=sys.stderr)
    meta = {
        "protocol_version": "1",
        "host": args.host,
        "provider": provider,
        "model": args.model,
        "mode": args.mode or "auto",
        "variants": [variant_name(*v) for v in _variants(args)],
        "analysis": args.analysis,
        "invoke": invoke,
        "cli_version": (
            oneshot_version() if args.host == "oneshot" else cli_version(binary)
        ),
        "flags_verified": profile["verified"] and not args.cmd,
        "started_at": stamp,
        "prompt_hashes": {p: prompt_hash(p, host=args.host) for p in PASSES},
        "key_hashes": {c: key_hash(c) for c in sorted({p[0] for p in plan})},
        "commands": {},
        "runs": [],
    }
    path = os.path.join(out_dir, "results.json")
    try:
        # ponytail: no early stop, so a usage cap fails every remaining run; stop after 3 consecutive exit-nonzero runs once an eval's results.json shows such a streak
        # ponytail: repeats of one variant run back to back; interleave variants per repeat if a provider slowdown shows as one variant's duration_s over 2x the others' on a case
        for case, pass_name, argv, stdin_text, variant in plan:
            slug, tag = _variant_tags(variant)
            meta["commands"][pass_name + tag] = shlex.join(argv) + (
                " < prompt" if stdin_text else ""
            )
            for i in range(1, args.repeat + 1):
                label = f"{case}-{pass_name}{slug}-{i}"
                meta["runs"].append(
                    _run_labelled(
                        case, pass_name, argv, stdin_text, out_dir, label, args, variant
                    )
                )
                # After every run, so a crash or kill loses one run, not all.
                meta["summary"] = summarize(meta["runs"])
                write_json(path, meta)
    except KeyboardInterrupt:
        meta["interrupted"] = True
        print("[bench] interrupted; writing the runs so far", file=sys.stderr)
    meta["summary"] = summarize(meta["runs"])
    write_json(path, meta)
    print_summary(meta["summary"], host=args.host)
    print(f"\nresults: {os.path.relpath(path)}")
    if not meta["flags_verified"]:
        print(
            f"note: the {args.host} command line is unverified; check commands in results.json",
            file=sys.stderr,
        )
    if not meta["runs"]:
        print("error: no runs (check --cases, --pass and --repeat)", file=sys.stderr)
        return 1
    ok = all("score" in r for r in meta["runs"]) and not meta.get("interrupted")
    return 0 if ok else 1


def write_json(path, data):
    """Write via a temp file and rename, so a kill never truncates it."""
    # ponytail: fixed tmp name races a rescore run against a live run; use a per-process name if a results.json is ever found truncated or mixed
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _new_dir(path):
    """path, or path-2, path-3... if a run started the same second has it."""
    candidate, n = path, 1
    while True:
        try:
            os.makedirs(candidate)
            return candidate
        except FileExistsError:
            n += 1
            candidate = f"{path}-{n}"


def key_hash(case, cases_dir=CASES_DIR):
    with open(os.path.join(cases_dir, case, "key.json"), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def cmd_score(args):
    key = load_key(args.case)
    with open(args.report, encoding="utf-8") as f:
        raw = f.read()
    if not has_severity_sections(raw):
        print(f"error: {args.report} has no severity sections", file=sys.stderr)
        return 1
    result = score(key, parse_report(raw))
    print(json.dumps(result, indent=2))
    return 0


def cmd_rescore(args):
    """Re-score a run's saved reports with the current scorer and keys."""
    with open(args.results, encoding="utf-8") as f:
        meta = json.load(f)
    run_dir = os.path.dirname(os.path.abspath(args.results))
    for r in meta["runs"]:
        saved = os.path.join(run_dir, f"{r['label']}.md")
        if not os.path.exists(saved):
            if "score" in r:  # nothing to re-score from: don't mix old and new
                del r["score"]
                r["status"] = "no-saved-report"
            continue
        with open(saved, encoding="utf-8") as f:
            text = f.read()
        try:
            key = load_key(r["case"])
        except (OSError, ValueError) as e:
            raise SystemExit(f"error: {r['label']}: can't load its case key: {e}")
        findings = parse_report(text)
        if has_severity_sections(text) and (findings or not section_body_lines(text)):
            r["score"] = score(key, findings)
            if r.get("status") not in ("scored", "scored-from-output"):
                r["status"] = "scored"
        else:
            r.pop("score", None)
            r["status"] = "unparsed"
    meta["summary"] = summarize(meta["runs"])
    meta["key_hashes"] = {
        c: key_hash(c) for c in sorted({r["case"] for r in meta["runs"]})
    }
    meta["rescored_at"] = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    if not os.path.exists(args.results + ".orig"):
        shutil.copyfile(args.results, args.results + ".orig")  # first scores kept
    write_json(args.results, meta)
    print_summary(meta["summary"], host=meta.get("host"))
    return 0


def cmd_compare(args):
    with open(args.baseline, encoding="utf-8") as f:
        base = json.load(f)
    with open(args.candidate, encoding="utf-8") as f:
        cand = json.load(f)
    for side, meta in (("baseline: ", base), ("candidate:", cand)):
        print(
            f"{side} {meta.get('host')} {meta.get('model') or 'default'}"
            f" mode {meta.get('mode') or 'auto'} ({meta.get('started_at')})"
        )
    if base.get("prompt_hashes") != cand.get("prompt_hashes"):
        print("prompts differ between runs")
    problems, notes = compare(
        base,
        cand,
        args.recall_drop,
        args.noise_rise,
        args.precision_drop,
        args.dup_rise,
        args.cost_rise,
    )
    for n in notes:
        print(f"  note: {n}")
    for p in problems:
        print(f"  REGRESSION {p}")
    if not problems:
        print("no regressions")
    return 1 if problems else 0


def cmd_commands(args):
    profile = HOSTS[args.host]
    if args.provider and args.host != "oneshot":
        # The same refusal `run` gives (oneshot_setup), not a dropped flag.
        print("error: --provider is for --host oneshot", file=sys.stderr)
        return 2
    try:
        variants = _variants(args)
        lines = []
        for pass_name in _passes(args.pass_):
            for model, mode in variants:
                argv, stdin_text = build_command(
                    args.host,
                    pass_name,
                    model,
                    args.invoke,
                    args.cmd,
                    provider=args.provider if args.host == "oneshot" else None,
                    mode=mode,
                )
                suffix = (
                    f"  < (grumpy {pass_name} SKILL.md as the prompt)"
                    if stdin_text
                    else ""
                )
                tag = f" @{variant_name(model, mode)}" if len(variants) > 1 else ""
                lines.append(f"{pass_name}{tag}: {shlex.join(argv)}{suffix}")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    if not profile["verified"]:
        print(f"(the {args.host} flags are unverified until a live run)")
    return 0


def cmd_cases(_args):
    for case in list_cases():
        key = load_key(case)
        print(
            f"{case:24} {','.join(key['passes']):14} planted={len(key['planted'])} acceptable={len(key['acceptable'])}"
        )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def host_args(p):
        p.add_argument("--host", required=True, choices=sorted(HOSTS))
        p.add_argument(
            "--model",
            default=None,
            help="model id or alias, or several comma-separated (run tries each); omit for the host default",
        )
        p.add_argument(
            "--mode",
            default=None,
            help="how the pass runs its passes: auto (the host decides), inline, fanout, or several comma-separated; inline and fanout are claude only",
        )
        p.add_argument("--pass", dest="pass_", default="all", choices=[*PASSES, "all"])
        p.add_argument("--invoke", choices=["native", "inline"], default=None)
        p.add_argument(
            "--provider",
            choices=["openai", "compatible"],
            default=None,
            help="oneshot only: the chat API (default SHARPEN_LLM_PROVIDER, else openai)",
        )
        p.add_argument(
            "--cmd",
            default=None,
            help="override the host command line ({model}, {provider}, {grumpy}, {python}, {pass}, {claude_tools}, {prompt}; {model} and {provider} must be their own tokens)",
        )

    p = sub.add_parser("run", help="run passes on a host and score them")
    host_args(p)
    p.add_argument(
        "--cases", default=None, help="comma-separated case names (default: all)"
    )
    p.add_argument("--repeat", type=int, default=3, help="runs per case (default 3)")
    p.add_argument(
        "--analysis",
        choices=["haiku", "sonnet", "opus"],
        default=None,
        help="pin models.yaml's analysis role (review's fanned-out passes on Claude Code; not oneshot); default: --model when it is a Claude Code alias",
    )
    p.add_argument(
        "--timeout", type=int, default=1200, help="seconds per run (default 1200)"
    )
    p.add_argument(
        "--out",
        default=None,
        help=f"results root (default {os.path.relpath(RESULTS_DIR, ROOT)})",
    )
    p.add_argument(
        "--keep", action="store_true", help="keep the temp repos for inspection"
    )
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("score", help="score one report against a case")
    p.add_argument("report")
    p.add_argument("--case", required=True)
    p.set_defaults(func=cmd_score)

    p = sub.add_parser(
        "rescore",
        help="re-score a results.json's saved reports after a scorer or key change",
    )
    p.add_argument("results")
    p.set_defaults(func=cmd_rescore)

    p = sub.add_parser("compare", help="compare two results.json files")
    p.add_argument("baseline")
    p.add_argument("candidate")
    p.add_argument("--recall-drop", type=float, default=0.0)
    p.add_argument("--noise-rise", type=float, default=0.0)
    p.add_argument("--precision-drop", type=float, default=0.1)
    p.add_argument("--dup-rise", type=float, default=1.0)
    p.add_argument(
        "--cost-rise",
        type=float,
        default=0.5,
        help="flag mean cost (or tokens) rising by more than this fraction (default 0.5)",
    )
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("commands", help="print the exact command a run would use")
    host_args(p)
    p.set_defaults(func=cmd_commands)

    p = sub.add_parser("cases", help="list fixture cases")
    p.set_defaults(func=cmd_cases)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
