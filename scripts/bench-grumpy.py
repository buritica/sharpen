#!/usr/bin/env python3
"""Local benchmark for grumpy's review and imagine passes.

Runs the real pass headlessly on a host (Claude Code, Codex or pi) against
small fixture repos, scores the report the pass writes, and compares runs so
a model, host or prompt change shows up as a number. Local only: nothing here
runs in CI, and results land in the gitignored .sharpen/bench/.

    bench-grumpy.py run --host claude [--model ID] [--pass review|imagine|all]
                        [--cases a,b] [--repeat N] [--invoke native|inline]
                        [--cmd TEMPLATE] [--timeout S] [--keep]
    bench-grumpy.py score REPORT --case NAME [--format report|pipe|json]
    bench-grumpy.py compare BASELINE.json CANDIDATE.json
    bench-grumpy.py commands --host codex [--model ID] [--pass review]
    bench-grumpy.py cases

Fixtures live in plugins/grumpy/tests/bench/<case>/: a `before/` tree, an
`after/` tree and a `key.json` (see that directory's README). Line numbers in
a key refer to the `after/` files.

Scoring is deterministic: a finding matches a planted issue when it names the
same file, a line within WINDOW lines of the issue's range, and one of its
keywords. Matching is one to one, so a single finding can't claim two issues
and two findings can't both claim one. Findings that match an `acceptable`
entry (real, but not planted) don't count as noise unless they exceed that
entry's `max_severity`.
"""

import argparse
import datetime
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRUMPY = os.path.join(ROOT, "plugins", "grumpy")
CASES_DIR = os.path.join(GRUMPY, "tests", "bench")
RESULTS_DIR = os.path.join(ROOT, ".sharpen", "bench")
PASSES = ("review", "imagine")
WINDOW = 3
RANK = {"NOTE": 1, "WARN": 2, "CRIT": 3}
BLOCKING = {"CRIT", "WARN"}

# ---------------------------------------------------------------------------
# Host profiles. `{model}` is dropped with its flag when no model is given.
# `{prompt}` is the argv slot for native invocation; inline invocation sends
# the prompt on stdin. The codex flags (exec, --model, --sandbox
# workspace-write, --skip-git-repo-check, `-` for stdin) match OpenAI's Codex
# CLI docs as of 2026-11-22; the pi flags match docs.pi.dev. Neither has had a
# live run yet, so results record `flags_verified: false` until one does.
# --cmd overrides any of them without a code change, and `commands` prints
# exactly what would run.
# ---------------------------------------------------------------------------
# What a pass needs on Claude Code, and no more: git and python3 for the diff
# and the receipt, file tools for the report, Agent for per-aspect passes.
CLAUDE_TOOLS = ",".join(
    [
        "Bash(git:*)",
        "Bash(python3:*)",
        "Bash(mkdir:*)",
        "Bash(ls:*)",
        "Bash(head:*)",
        "Read",
        "Write",
        "Edit",
        "Write(.claude/grumpy/**)",
        "Glob",
        "Grep",
        "Agent",
        "TaskCreate",
        "TaskUpdate",
    ]
)

HOSTS = {
    "claude": {
        "binary": "claude",
        "default_invoke": "native",
        "native": [
            "claude",
            "-p",
            "--plugin-dir",
            "{grumpy}",
            "--permission-mode",
            "acceptEdits",
            "--allowedTools={claude_tools}",
            "--model",
            "{model}",
            "{prompt}",
        ],
        "inline": [
            "claude",
            "-p",
            "--permission-mode",
            "acceptEdits",
            "--allowedTools={claude_tools}",
            "--model",
            "{model}",
        ],
        "verified": True,
    },
    "codex": {
        "binary": "codex",
        "default_invoke": "inline",
        "inline": [
            "codex",
            "exec",
            "--model",
            "{model}",
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "-",
        ],
        "verified": False,
    },
    "pi": {
        "binary": "pi",
        "default_invoke": "inline",
        "inline": ["pi", "--print", "--no-session", "--model", "{model}"],
        "verified": False,
    },
}


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
    return key


def _tree_files(root):
    out = {}
    for dirpath, _, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            out[os.path.relpath(full, root).replace(os.sep, "/")] = full
    return out


def changed_lines(case, cases_dir=CASES_DIR):
    """{path: set of after/ line numbers that are added or changed}."""
    base = os.path.join(cases_dir, case)
    before = _tree_files(os.path.join(base, "before"))
    after = _tree_files(os.path.join(base, "after"))
    out = {}
    for rel, path in after.items():
        with open(path, encoding="utf-8") as f:
            new = f.read().splitlines()
        old = []
        if rel in before:
            with open(before[rel], encoding="utf-8") as f:
                old = f.read().splitlines()
        lines = set()
        matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
        for tag, _, _, j1, j2 in matcher.get_opcodes():
            if tag in ("replace", "insert"):
                lines.update(range(j1 + 1, j2 + 1))
        if lines:
            out[rel] = lines
    return out


def build_repo(case, dest, cases_dir=CASES_DIR):
    """Make a git repo at dest: `before/` on main, `after/` committed on a branch.

    Returns the branch name. A real consumer repo ignores sharpen's artifacts,
    so the base commit does too.
    """
    base = os.path.join(cases_dir, case)
    key = load_key(case, cases_dir)
    branch = f"bench/{case}"

    def git(*args):
        subprocess.run(
            ["git", "-C", dest, *args],
            check=True,
            capture_output=True,
            text=True,
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
    # list review must honor) goes where grumpy reads it. .claude/ is ignored,
    # so it leaves the worktree clean.
    art_dir = os.path.join(dest, ".claude", "grumpy", *branch.split("/"))
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
_EMPTY_BULLET = re.compile(
    r"^(none|n/?a|nothing|no (findings|issues|concerns))\b", re.I
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


def parse_pipe(raw_text):
    """Findings from raw SEVERITY|file:line|text|FACT|ASPECT sub-agent lines."""
    findings = []
    for line in raw_text.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        try:
            severity, rest = line.split("|", 1)
            loc, rest = rest.split("|", 1)
            text, _fact, _aspect = rest.rsplit("|", 2)
        except ValueError:
            continue
        severity = severity.strip()
        if severity not in RANK:
            continue
        findings.append(
            {
                "severity": severity,
                "text": text.strip(),
                "locations": parse_locations(loc)
                or [{"file": loc.split(":")[0].strip(), "line": None, "end": None}],
            }
        )
    return findings


def normalize_json(items):
    """Accept {severity, file, line?, text} or {severity, locations, text}."""
    out = []
    for item in items:
        if "locations" in item:
            locs = item["locations"]
        else:
            line = item.get("line")
            locs = [
                {
                    "file": item.get("file", ""),
                    "line": line,
                    "end": item.get("end", line),
                }
            ]
        out.append(
            {
                "severity": item.get("severity", "NOTE"),
                "text": item.get("text", ""),
                "locations": locs,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _same_file(a, b):
    a, b = a.strip("`./"), b.strip("`./")
    if not a or not b:
        return False
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def matches(target, finding, window=WINDOW):
    """Same file, a line inside the target's range +/- window, and a keyword.

    A finding that names the file but no line matches on keyword alone. A
    keyword is required either way: planted issues sit a few lines apart, and
    a line window alone would let one finding claim its neighbour.
    """
    text = finding.get("text", "").lower()
    keywords = target.get("keywords") or []
    if keywords and not any(k.lower() in text for k in keywords):
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

    `owner` seeds the matching; augmenting keeps every seeded right matched.
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
    # issues are paired the right way round; then fill in the rest.
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
    return matched, p_edges


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


def _classify_unmatched(findings, matched, p_edges, acceptable, window):
    """Split the unmatched findings into (duplicates, unlocated, noise)."""
    unmatched = [i for i in range(len(findings)) if i not in matched]
    # An unmatched finding that describes an issue another finding already
    # claimed is a duplicate: a dedup miss, reported separately, not noise.
    duplicates = [
        i
        for i in unmatched
        if p_edges[i] or any(matches(t, findings[i], window) for t in acceptable)
    ]
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

    matched, p_edges = _pair_findings(findings, planted, acceptable, window)
    hits, underrated, overrated, present = _rate_matches(
        matched, findings, planted, acceptable, absent
    )
    duplicates, unlocated, noise = _classify_unmatched(
        findings, matched, p_edges, acceptable, window
    )
    blocking = [
        i
        for i in range(n)
        if findings[i].get("severity") in BLOCKING and i not in unlocated
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
        "clean": false_blocking == 0 and not present,
    }


def _mean(values):
    """statistics.mean, or None when there is nothing to average."""
    values = list(values)
    return statistics.mean(values) if values else None


def summarize(runs):
    """Aggregate per (case, pass) over repeats. Runs without a report count as failures."""
    groups = {}
    for r in runs:
        groups.setdefault(f"{r['case']}/{r['pass']}", []).append(r)
    out = {}
    for name, rs in sorted(groups.items()):
        scored = [r["score"] for r in rs if r.get("score")]
        recalls = [s["recall"] for s in scored if s["recall"] is not None]
        precisions = [s["precision"] for s in scored if s["precision"] is not None]
        out[name] = {
            "runs": len(rs),
            "failed_runs": len(rs) - len(scored),
            "recall": _mean(recalls),
            "recall_spread": (max(recalls) - min(recalls)) if len(recalls) > 1 else 0.0,
            "precision": _mean(precisions),
            "noise_blocking": _mean(s["noise_blocking"] for s in scored),
            "overrated": _mean(len(s["overrated"]) for s in scored),
            "unlocated": _mean(s.get("unlocated", 0) for s in scored),
            "findings": _mean(s["findings"] for s in scored),
            "clean_rate": _mean(1.0 if s["clean"] else 0.0 for s in scored),
            "held_back_failures": sum(1 for s in scored if s.get("present")),
        }
    return out


def compare(baseline, candidate, recall_drop=0.0, noise_rise=0.0, precision_drop=0.1):
    """List regressions of candidate vs baseline summaries. Empty list = no regression."""
    problems, notes = [], []
    b, c = baseline.get("summary", {}), candidate.get("summary", {})
    for name in sorted(set(b) | set(c)):
        if name not in c:
            notes.append(f"{name}: not in candidate")
            continue
        if name not in b:
            notes.append(f"{name}: new in candidate")
            continue
        bs, cs = b[name], c[name]
        if cs["failed_runs"] > bs["failed_runs"]:
            problems.append(
                f"{name}: {cs['failed_runs']} run(s) wrote no report (baseline {bs['failed_runs']})"
            )
        if cs.get("held_back_failures", 0) > bs.get("held_back_failures", 0):
            problems.append(
                f"{name}: {cs['held_back_failures']} run(s) re-raised a settled finding"
                f" (baseline {bs.get('held_back_failures', 0)})"
            )
        if (
            bs["recall"] is not None
            and cs["recall"] is not None
            and cs["recall"] < bs["recall"] - recall_drop
        ):
            problems.append(f"{name}: recall {bs['recall']:.0%} -> {cs['recall']:.0%}")
        if (
            bs["noise_blocking"] is not None
            and cs["noise_blocking"] is not None
            and cs["noise_blocking"] > bs["noise_blocking"] + noise_rise
        ):
            problems.append(
                f"{name}: blocking noise {bs['noise_blocking']:.1f} -> {cs['noise_blocking']:.1f}"
            )
        if (
            bs["precision"] is not None
            and cs["precision"] is not None
            and cs["precision"] < bs["precision"] - precision_drop
        ):
            problems.append(
                f"{name}: precision {bs['precision']:.0%} -> {cs['precision']:.0%}"
            )
    return problems, notes


# ---------------------------------------------------------------------------
# Running a host
# ---------------------------------------------------------------------------
def skill_prompt(pass_name, grumpy=GRUMPY):
    """The pass's cross-host SKILL.md as a self-contained prompt."""
    with open(
        os.path.join(grumpy, "skills", pass_name, "SKILL.md"), encoding="utf-8"
    ) as f:
        body = f.read()
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            body = body[end + 4 :]
    body = body.replace("$ARGUMENTS", "")
    return (
        f"Run grumpy's {pass_name} pass on the current branch of this repository, non-interactively. "
        "Nobody is available to answer questions: make reasonable choices and finish. The arguments are empty. "
        f"Follow the instructions below exactly, including writing the report to .claude/grumpy/<branch>/{pass_name}.md.\n\n"
        + body.lstrip()
    )


def prompt_hash(pass_name, grumpy=GRUMPY):
    h = hashlib.sha256()
    for rel in (
        os.path.join("commands", f"{pass_name}.md"),
        os.path.join("skills", pass_name, "SKILL.md"),
    ):
        path = os.path.join(grumpy, rel)
        if os.path.exists(path):
            with open(path, "rb") as f:
                h.update(f.read())
    return h.hexdigest()[:16]


def build_command(
    host, pass_name, model=None, invoke=None, template=None, grumpy=GRUMPY
):
    """Return (argv, stdin_text) for one run."""
    profile = HOSTS[host]
    invoke = invoke or profile["default_invoke"]
    if template:
        argv = shlex.split(template)
    else:
        if invoke not in profile:
            raise ValueError(f"host {host} has no {invoke} invocation")
        argv = list(profile[invoke])
    out = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "{model}" or (
            i + 1 < len(argv) and argv[i + 1] == "{model}" and tok.startswith("-")
        ):
            if model is None:
                i += 1 if tok == "{model}" else 2
                continue
        out.append(tok)
        i += 1
    native_prompt = f"/grumpy:{pass_name}"
    argv = [
        t.replace("{model}", model or "")
        .replace("{grumpy}", grumpy)
        .replace("{claude_tools}", CLAUDE_TOOLS)
        .replace("{prompt}", native_prompt)
        for t in out
    ]
    stdin_text = None if invoke == "native" else skill_prompt(pass_name, grumpy)
    return argv, stdin_text


def cli_version(binary):
    try:
        r = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=30
        )
        return (
            (r.stdout or r.stderr).strip().splitlines()[0]
            if (r.stdout or r.stderr).strip()
            else None
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


REPORT_TITLES = {"review": "# Code Review", "imagine": "# Production Imagination"}


def report_in_output(output, pass_name):
    """True when output contains the pass's rendered report."""
    return any(
        line.startswith(REPORT_TITLES[pass_name]) for line in output.splitlines()
    )


def find_report(repo, branch, pass_name):
    expected = os.path.join(
        repo, ".claude", "grumpy", *branch.split("/"), f"{pass_name}.md"
    )
    if os.path.isfile(expected):
        return expected
    for dirpath, _, files in os.walk(os.path.join(repo, ".claude", "grumpy")):
        if f"{pass_name}.md" in files:
            return os.path.join(dirpath, f"{pass_name}.md")
    return None


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
):
    key = load_key(case, cases_dir)
    repo = tempfile.mkdtemp(prefix=f"grumpy-bench-{case}-")
    result = {"case": case, "pass": pass_name, "label": label}
    try:
        branch = build_repo(case, repo, cases_dir)
        env = dict(os.environ, CLAUDE_PLUGIN_ROOT=grumpy, GRUMPY_PLUGIN_ROOT=grumpy)
        started = datetime.datetime.now(datetime.timezone.utc)
        try:
            proc = subprocess.run(
                argv,
                cwd=repo,
                input=stdin_text,
                stdin=None if stdin_text is not None else subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            result["exit_code"] = proc.returncode
            output = (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired as e:
            result["exit_code"] = None
            result["status"] = "timeout"
            output = str(e.stdout or "")
        except FileNotFoundError:
            result["status"] = "cli-missing"
            result["error"] = f"{argv[0]} not found on PATH"
            return result
        result["duration_s"] = round(
            (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds(), 1
        )
        with open(os.path.join(out_dir, f"{label}.log"), "w", encoding="utf-8") as f:
            f.write(output)
        report = find_report(repo, branch, pass_name)
        saved = os.path.join(out_dir, f"{label}.md")
        if report:
            shutil.copyfile(report, saved)
            with open(report, encoding="utf-8") as f:
                findings = parse_report(f.read())
            result["status"] = "scored"
        elif report_in_output(output, pass_name):
            # The host printed the report but couldn't save it (Claude Code
            # treats .claude/ as a sensitive path in headless runs). Score what
            # it printed, and say so: the pass's own write step failed.
            with open(saved, "w", encoding="utf-8") as f:
                f.write(output)
            findings = parse_report(output)
            result["status"] = "scored-from-output"
        else:
            result.setdefault("status", "no-report")
            return result
        result["report"] = (
            os.path.relpath(saved, ROOT) if saved.startswith(ROOT) else saved
        )
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


def print_summary(summary, stream=sys.stdout):
    print(
        f"{'case/pass':34} {'runs':>4} {'fail':>4} {'recall':>7} {'spread':>7} {'prec':>6} {'noise':>6} {'over':>5} {'unloc':>6} {'clean':>6}",
        file=stream,
    )
    for name, s in summary.items():
        print(
            f"{name:34} {s['runs']:>4} {s['failed_runs']:>4} {_fmt(s['recall']):>7} {_fmt(s['recall_spread']):>7} "
            f"{_fmt(s['precision']):>6} {_fmt(s['noise_blocking'], False):>6} {_fmt(s['overrated'], False):>5} {_fmt(s['unlocated'], False):>6} {_fmt(s['clean_rate']):>6}",
            file=stream,
        )


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


def _run_labelled(case, pass_name, argv, stdin_text, out_dir, label, args):
    """One run_one call, bracketed by progress lines on stderr."""
    print(f"[bench] {label} ...", file=sys.stderr, flush=True)
    r = run_one(
        case, pass_name, argv, stdin_text, out_dir, label, args.timeout, args.keep
    )
    s = r.get("score")
    detail = f" recall={_fmt(s['recall'])} noise={s['noise_blocking']}" if s else ""
    print(f"[bench] {label}: {r.get('status')}{detail}", file=sys.stderr, flush=True)
    return r


def cmd_run(args):
    profile = HOSTS[args.host]
    binary = shlex.split(args.cmd)[0] if args.cmd else profile["binary"]
    if not shutil.which(binary):
        print(f"error: {binary} is not on PATH", file=sys.stderr)
        return 2
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}-{args.host}-{(args.model or 'default').replace('/', '_')}"
    out_dir = os.path.join(args.out or RESULTS_DIR, run_id)
    os.makedirs(out_dir, exist_ok=True)
    invoke = args.invoke or profile["default_invoke"]
    meta = {
        "protocol_version": "1",
        "host": args.host,
        "model": args.model,
        "invoke": invoke,
        "cli_version": cli_version(binary),
        "flags_verified": profile["verified"] and not args.cmd,
        "started_at": stamp,
        "prompt_hashes": {p: prompt_hash(p) for p in PASSES},
        "commands": {},
        "runs": [],
    }
    for case in _selected_cases(args.cases):
        key = load_key(case)
        for pass_name in _passes(args.pass_):
            if pass_name not in key["passes"]:
                continue
            argv, stdin_text = build_command(
                args.host, pass_name, args.model, invoke, args.cmd
            )
            meta["commands"][pass_name] = shlex.join(argv) + (
                " < prompt" if stdin_text else ""
            )
            for i in range(1, args.repeat + 1):
                label = f"{case}-{pass_name}-{i}"
                meta["runs"].append(
                    _run_labelled(
                        case, pass_name, argv, stdin_text, out_dir, label, args
                    )
                )
    meta["summary"] = summarize(meta["runs"])
    path = os.path.join(out_dir, "results.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
        f.write("\n")
    print_summary(meta["summary"])
    print(f"\nresults: {os.path.relpath(path)}")
    if not meta["flags_verified"]:
        print(
            f"note: the {args.host} command line is unverified; check commands in results.json",
            file=sys.stderr,
        )
    return 0 if all("score" in r for r in meta["runs"]) else 1


def cmd_score(args):
    key = load_key(args.case)
    with open(args.report, encoding="utf-8") as f:
        raw = f.read()
    fmt = args.format or ("json" if args.report.endswith(".json") else "report")
    if fmt == "json":
        findings = normalize_json(json.loads(raw))
    elif fmt == "pipe":
        findings = parse_pipe(raw)
    else:
        findings = parse_report(raw)
    result = score(key, findings)
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
            continue
        with open(saved, encoding="utf-8") as f:
            r["score"] = score(load_key(r["case"]), parse_report(f.read()))
    meta["summary"] = summarize(meta["runs"])
    meta["rescored_at"] = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    with open(args.results, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
        f.write("\n")
    print_summary(meta["summary"])
    return 0


def cmd_compare(args):
    with open(args.baseline, encoding="utf-8") as f:
        base = json.load(f)
    with open(args.candidate, encoding="utf-8") as f:
        cand = json.load(f)
    print(
        f"baseline:  {base.get('host')} {base.get('model') or 'default'} ({base.get('started_at')})"
    )
    print(
        f"candidate: {cand.get('host')} {cand.get('model') or 'default'} ({cand.get('started_at')})"
    )
    if base.get("prompt_hashes") != cand.get("prompt_hashes"):
        print("prompts differ between runs")
    problems, notes = compare(
        base, cand, args.recall_drop, args.noise_rise, args.precision_drop
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
    for pass_name in _passes(args.pass_):
        argv, stdin_text = build_command(
            args.host, pass_name, args.model, args.invoke, args.cmd
        )
        suffix = (
            f"  < (grumpy {pass_name} SKILL.md as the prompt)" if stdin_text else ""
        )
        print(f"{pass_name}: {shlex.join(argv)}{suffix}")
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
            "--model", default=None, help="model id or alias; omit for the host default"
        )
        p.add_argument("--pass", dest="pass_", default="all", choices=[*PASSES, "all"])
        p.add_argument("--invoke", choices=["native", "inline"], default=None)
        p.add_argument(
            "--cmd",
            default=None,
            help="override the host command line ({model}, {grumpy}, {prompt})",
        )

    p = sub.add_parser("run", help="run passes on a host and score them")
    host_args(p)
    p.add_argument(
        "--cases", default=None, help="comma-separated case names (default: all)"
    )
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--timeout", type=int, default=1200, help="seconds per run")
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
    p.add_argument("--format", choices=["report", "pipe", "json"], default=None)
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
