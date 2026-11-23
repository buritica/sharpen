#!/usr/bin/env python3
"""Read grumpy's review and imagine commands into the prompts oneshot.py sends.

Every prompt the no-agent path sends comes from `commands/review.md` or
`commands/imagine.md` at run time, so the path changes when a command does.
A command edit these extractors can't follow raises ExtractionError (oneshot
exits 2); nothing falls back to a prompt of its own. Pure stdlib.
"""

import os
import re

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Imagine's five simulation passes: the name `--aspects` takes, and the start
# of the `### Agent N:` heading in imagine.md whose fence is that pass's
# prompt. A sixth agent in imagine.md stops the run until it is named here.
IMAGINE_AGENTS = (
    ("happy-path", "Agent 1: Happy Path"),
    ("state", "Agent 2: State Transitions"),
    ("concurrency", "Agent 3: Concurrency"),
    ("observability", "Agent 4: Observability"),
    ("docs", "Agent 5: Documentation"),
)

# grumpy's pass severities and the report section each one lands in on
# review. Both commands define the same three ("SEVERITY is one of CRIT ...,
# WARN ..., NOTE ..."; imagine names its sections differently); the
# extractors read them back and a test pins them together.
SEVERITY_SECTIONS = {
    "CRIT": "## Critical Issues",
    "WARN": "## Serious Concerns",
    "NOTE": "## Questionable Decisions",
}


class ExtractionError(ValueError):
    """review.md or imagine.md no longer has a part this path builds its
    prompts from."""


# --- reading the command ----------------------------------------------------


def _strip_frontmatter(text):
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5 :]
    return text


def _headings(lines):
    """(index, level, title) for every markdown heading outside a code fence."""
    out, fenced = [], False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        m = None if fenced else re.match(r"^(#{1,6})\s+(.*\S)\s*$", line)
        if m:
            out.append((i, len(m.group(1)), m.group(2)))
    return out


def section(text, title, *, prefix=False):
    """The body of the heading named `title` (or starting with it, with
    `prefix`), up to the next heading of the same or a higher level. Headings
    inside code fences don't count, so a template's own `## Verdict` stays in.
    Raises ExtractionError when the heading isn't there."""
    lines = text.split("\n")
    heads = _headings(lines)
    for n, (i, level, name) in enumerate(heads):
        if name == title or (prefix and name.startswith(title)):
            end = len(lines)
            for j, lvl, _ in heads[n + 1 :]:
                if lvl <= level:
                    end = j
                    break
            return "\n".join(lines[i + 1 : end]).strip("\n")
    raise ExtractionError(f"heading {title!r} not found")


def _fence_after(text, marker):
    at = text.find(marker)
    if at == -1:
        raise ExtractionError(f"{marker!r} not found")
    m = re.search(r"```[^\n]*\n(.*?)\n```", text[at:], re.S)
    if not m:
        raise ExtractionError(f"no code fence after {marker!r}")
    return m.group(1)


def extract_review(text):
    """The parts of review.md this path sends, and nothing else.

    Taken: the persona (the text under `# Grumpy Review`), the aspect list
    (`### Available Review Aspects`), the finding-pass prompt template (the
    first fence after "Example agent prompt structure" in Step 3) and the
    severities it defines, `## Audit discipline`, all of Step 4 (settled
    findings and the report template) and `## Personality Guidelines`.

    Left out, because a single call can't do them and this script does them
    instead: level flags and worktree targeting (CLI flags here), Step 1's
    git commands (`resolve_diff`), Step 3's subagent dispatch and model
    routing (one call per aspect here), Step 5's save, Step 6's plan note
    (skipped), Step 7's receipt (`receipt.write_receipt`).
    """
    body = _strip_frontmatter(text)
    persona = section(body, "Grumpy Review").split("\n## ")[0].strip()
    if "grumpy principal engineer" not in persona:
        raise ExtractionError("persona no longer names the grumpy principal engineer")
    aspects = re.findall(
        r"^- \*\*([a-z]+)\*\* - (.+?)(?=\n- \*\*|\n\n|\Z)",
        section(body, "Available Review Aspects"),
        re.S | re.M,
    )
    if not any(name == "code" for name, _ in aspects):
        raise ExtractionError("aspect list has no `code` aspect")
    step3 = section(body, "Step 3:", prefix=True)
    template = _fence_after(step3, "Example agent prompt structure")
    for slot in ("[ASPECT]", "[DIFF_BASE]", "[DIFF_CONTENT]", "SEVERITY|file:line"):
        if slot not in template:
            raise ExtractionError(f"pass template lost {slot}")
    sentence = re.search(r"SEVERITY is one of (.+?)\.\n", template, re.S)
    if not sentence:
        raise ExtractionError("pass template no longer says which severities exist")
    severities = re.findall(r"\b([A-Z]{3,5}) \(", sentence.group(1))
    step4 = section(body, "Step 4:", prefix=True)
    for anchor in (
        "# Code Review:",
        "_Model:",
        "## Verdict",
        "Settled findings held back",
    ):
        if anchor not in step4:
            raise ExtractionError(f"Step 4 report template lost {anchor!r}")
    aspects = [(n, " ".join(d.split())) for n, d in aspects]
    audit = section(body, "Audit discipline")
    personality = section(body, "Personality Guidelines")
    return {
        "persona": persona,
        "aspects": aspects,
        "pass_template": template,
        "severities": severities,
        "audit": audit,
        "step4": step4,
        "personality": personality,
        # What `run` uses, the same shape for both passes: one prompt per
        # unit, the report's title, the aggregation sections in order.
        "units": [
            (n, d, template.replace("[ASPECT]", f"{n} ({d})")) for n, d in aspects
        ],
        "title": "# Code Review",
        "aggregate": [
            ("## Audit discipline", audit),
            ("## Step 4: Aggregate and Deliver the Verdict", step4),
            ("## Personality Guidelines", personality),
        ],
        "context_labels": [],
    }


def _imagine_units(step2):
    """([(name, agent title, prompt)], CONTEXT labels) from Step 2, in
    IMAGINE_AGENTS order. Raises ExtractionError on an agent added, lost or
    renamed, a prompt with no fence, or a prompt that lost a placeholder."""
    heads = [t for _, lvl, t in _headings(step2.split("\n")) if lvl == 3]
    agents = [t for t in heads if re.match(r"Agent \d+:", t)]
    if len(agents) != len(IMAGINE_AGENTS):
        raise ExtractionError(
            f"Step 2 has {len(agents)} agent prompts, this path knows "
            f"{len(IMAGINE_AGENTS)}: update IMAGINE_AGENTS"
        )
    units, labels = [], []
    for name, start in IMAGINE_AGENTS:
        title = next((t for t in agents if t.startswith(start)), None)
        if title is None:
            raise ExtractionError(f"Step 2 lost the {start!r} agent")
        m = re.search(r"```[^\n]*\n(.*?)\n```", section(step2, title), re.S)
        if not m:
            raise ExtractionError(f"no prompt fence under {title!r}")
        prompt = m.group(1)
        for slot in ("[DIFF_BASE]", "[DIFF_CONTENT]", "SEVERITY|file:line"):
            if slot not in prompt:
                raise ExtractionError(f"{title!r} prompt lost {slot}")
        labels += re.findall(r"^`([a-z][a-z-]*)(?::\[Name\])?\|", prompt, re.M)
        units.append((name, title, prompt))
    if not labels:
        raise ExtractionError("no agent prompt defines a CONTEXT line any more")
    return units, labels


def _imagine_severities(units):
    """The severities Agent 1's prompt defines, checked against every prompt."""
    sentence = re.search(r"SEVERITY is (.+?)\.\n", units[0][2], re.S)
    if not sentence:
        raise ExtractionError("Agent 1's prompt no longer says which severities exist")
    severities = re.findall(r"\b([A-Z]{3,5})\s+\(", sentence.group(1))
    for _, title, prompt in units:
        missing = [s for s in severities if s not in prompt]
        if missing:
            raise ExtractionError(f"{title!r} prompt doesn't name {missing}")
    return severities


def extract_imagine(text):
    """The parts of imagine.md this path sends, and nothing else.

    Taken: the persona (the text under `# Grumpy Imagine`), each of Step 2's
    five agent prompts (the first fence under each `### Agent N:` heading,
    named by `IMAGINE_AGENTS`), the severities Agent 1's prompt defines, the
    CONTEXT line labels the prompts define (`transport:`, `cleanup`, ...),
    `## Audit Discipline`, all of Step 3 (section mapping, settled findings
    and the report template) and `## Personality Guidelines`.

    Left out, as for review: level flags and worktree targeting, Step 1's
    git commands, Step 2's subagent dispatch, Step 4's save, Step 5's plan
    note and Step 6's receipt, which this script does or skips itself.
    """
    body = _strip_frontmatter(text)
    persona = section(body, "Grumpy Imagine").split("\n## ")[0].strip()
    if "grumpy principal engineer" not in persona:
        raise ExtractionError("persona no longer names the grumpy principal engineer")
    units, labels = _imagine_units(section(body, "Step 2:", prefix=True))
    severities = _imagine_severities(units)
    step3 = section(body, "Step 3:", prefix=True)
    for anchor in (
        "# Production Imagination:",
        "_Model:",
        "_Analyzed:",
        "## Verdict",
        "Settled findings held back",
    ):
        if anchor not in step3:
            raise ExtractionError(f"Step 3 report template lost {anchor!r}")
    return {
        "persona": persona,
        "units": units,
        "severities": severities,
        "context_labels": sorted(set(labels)),
        "title": "# Production Imagination",
        "aggregate": [
            ("## Audit Discipline", section(body, "Audit Discipline")),
            ("## Step 3: Aggregate and Deliver the Simulation Report", step3),
            ("## Personality Guidelines", section(body, "Personality Guidelines")),
        ],
    }


EXTRACTORS = {"review": extract_review, "imagine": extract_imagine}


def load_command(pass_name, plugin_root=PLUGIN_ROOT):
    path = os.path.join(plugin_root, "commands", f"{pass_name}.md")
    with open(path, encoding="utf-8") as f:
        parts = EXTRACTORS[pass_name](f.read())
    if parts["severities"] != list(SEVERITY_SECTIONS):
        raise ExtractionError(
            f"{pass_name}.md's severities are {parts['severities']}, this path "
            f"knows {list(SEVERITY_SECTIONS)}: update SEVERITY_SECTIONS"
        )
    return parts
