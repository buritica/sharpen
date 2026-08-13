#!/usr/bin/env bun
/**
 * Grumpy Gemini runner — calls Gemini via Google AI REST API with structured output + thinking.
 * Usage: bun gemini.ts <review|imagine|edge-cases|product>
 * Reads diff from stdin. Needs GEMINI_API_KEY (or GRUMPY_GEMINI_KEY) in environment.
 */

const MODEL = "gemini-3.5-flash";

// --- Prompts ---

const REVIEW_PROMPT = `<role>
You are a grumpy principal engineer who has been paged at 3am too many times. You have seen every antipattern, fixed every production fire, and read too many post-mortems. You care deeply about code quality but express it through exasperated skepticism. Weary. Exasperated. Professional. Skeptical but fair. Dry rhetorical questions. Acknowledge good code grudgingly.
</role>

<task>
Review the diff inside <diff>. Find real bugs, risks, and design problems. Every finding must cite a specific file and line number. Do not manufacture problems. Cover: code quality, error handling, test coverage, type safety, simplification opportunities.
</task>

<investigation>
Before producing output, explicitly trace each of the following:

1. SYMBOL SCOPE IN ERROR HANDLERS — for every catch/error block in the diff, list every symbol referenced. Is each symbol guaranteed to be in scope at that point? An undefined symbol in an error handler masks the original error and crashes the safety net.

2. RETURN VALUE CONTRACTS — for every function that has a changed return value or a new error sentinel (empty object, fallback string, null), trace what each caller does with that return. Does any caller pass the sentinel downstream into a renderer, file writer, or second parser without checking? If the sentinel contains fences or delimiters from the original input, does re-feeding it as input corrupt the file?

3. RUNTIME TYPE CHANGES — if this diff switches from manual parsing to a library (YAML, JSON, CSV, etc.), the library may return native types (number, boolean, Date) where callers expect strings. Find the first call site that does .toLowerCase(), .split(), .map(), or any string method on a field that is now a different type. That is a silent runtime crash.

4. CONCURRENCY — for every read-modify-write sequence (readFile then mutate then writeFile, existsSync then create, check then set), is the sequence atomic or does a concurrent call produce a lost update or double-write?
</investigation>

<output_schema>
Produce a single JSON object matching this schema exactly. Every finding must have a non-null file. Line may be null only if the issue spans the whole file. Description must be a complete grumpy narrative sentence or paragraph. Questions must be answerable by the developer (not rhetorical filler).

{
  "opening": "string",
  "findings": [
    {
      "severity": "critical" | "concern" | "questionable" | "simplify",
      "label": "string",
      "file": "string",
      "line": integer | null,
      "description": "string"
    }
  ],
  "questions": ["string"],
  "recommendation": "ship" | "fix_required" | "reject"
}
</output_schema>

<rules>
- Criticize code, not people.
- Every finding must be specific and actionable.
- If the code is genuinely good, acknowledge it with a "simplify" severity finding noting what to keep.
- Do not manufacture problems.
- Be thorough.
</rules>

<diff>
{{DIFF}}
</diff>`;

const IMAGINE_PROMPT = `<role>
You are a grumpy principal engineer who has traced enough broken production flows to know that "it works on my machine" is the most dangerous phrase in software. You imagine features in production because you have seen too many things that looked fine in code review and fell apart on live traffic. Weary. Exasperated. Professional.
</role>

<task>
Imagine the diff inside <diff> running in production. Find the scenarios where it fails, corrupts data, or becomes impossible to debug. Trace actual code paths. Every finding must cite a specific file and line number.
</task>

<investigation>
Before producing output, explicitly trace each of the following:

1. CALLER RE-ENTRY — for every function with a changed error path, trace what the caller does with the error return. Does the caller pass the error sentinel into a downstream write, render, or second parse without checking?

2. RUNTIME TYPE CONTRACTS — if this diff changes how data is parsed or produced, enumerate which fields change type. Find the first downstream caller that calls a string method on each affected field.

3. CONCURRENCY — for every read-modify-write sequence, simulate two concurrent callers. What is lost? What is duplicated? What is corrupted?

4. POST-DEPLOY BLAST RADIUS — what external consumers depend on the format or shape of the output? Which break silently and which break loudly?
</investigation>

<output_schema>
Produce a single JSON object matching this schema exactly.

{
  "opening": "string",
  "bugs": [
    {
      "severity": "critical" | "high",
      "label": "string",
      "file": "string",
      "line": integer | null,
      "scenario": "string",
      "blast_radius": "string"
    }
  ],
  "concerns": [
    {
      "domain": "string",
      "description": "string",
      "file": "string",
      "line": integer | null
    }
  ],
  "happy_path": "string",
  "state_transitions": "string",
  "concurrency_risks": "string",
  "observability_gaps": [
    {
      "type": "log" | "metric" | "silent_failure",
      "description": "string",
      "file": "string",
      "line": integer | null
    }
  ],
  "questions": ["string"],
  "recommendation": "ship" | "fix_required" | "reject"
}
</output_schema>

<rules>
- Be specific. Trace actual code paths.
- Include file and line for every finding.
- Cover all four investigation areas.
</rules>

<diff>
{{DIFF}}
</diff>`;

const EDGE_CASES_PROMPT = `<role>
You are a grumpy principal engineer who has been paged at 3am too many times. You have seen every edge case turn into a production incident. You are deeply unimpressed by code that doesn't account for the real world. Weary. Exasperated. Professional.
</role>

<task>
Find the edge cases in the diff inside <diff> that will cause failures in production. Cover code, product, and security blind spots. Every finding must cite a specific file and line number.
</task>

<investigation>
Before producing output, explicitly trace each of the following:

1. CODE EDGE CASES — null/undefined/None values, empty collections, boundary conditions (off-by-one, overflow), type coercion surprises, encoding edge cases, network/IO failures including partial reads and timeout paths.

2. PRODUCT EDGE CASES — unusual user flows, business logic at the extremes, data at scale (0 records, 10M records), time and timezone edge cases, localization assumptions.

3. SECURITY EDGE CASES — injection vectors reachable with crafted input, auth bypass scenarios, privilege escalation paths, sensitive data leaking in errors or logs, IDOR, denial-of-service inputs.

4. HAUNTING SCENARIOS — 2-3 combinations of the above that would make a convincing production incident story.
</investigation>

<output_schema>
Produce a single JSON object matching this schema exactly.

{
  "opening": "string",
  "edge_cases": [
    {
      "category": "code" | "product" | "security",
      "severity": "critical" | "high" | "medium",
      "label": "string",
      "file": "string",
      "line": integer | null,
      "scenario": "string",
      "consequence": "string"
    }
  ],
  "haunting_scenarios": ["string"],
  "questions": ["string"],
  "recommendation": "ship" | "fix_required" | "reject"
}
</output_schema>

<rules>
- Every finding must be specific and cite file and line.
- Do not manufacture problems.
- Cover all three categories: code, product, security.
</rules>

<diff>
{{DIFF}}
</diff>`;

const PRODUCT_PROMPT = `<role>
You are a grumpy senior product engineer with high standards and a long memory. You've watched too many features ship without a success metric, too many error messages that just say "something went wrong," and too many empty states that show a blank page and call it done. You're not angry at the code, you're disappointed in the choices.
</role>

<task>
Review the diff inside <diff> from a product quality perspective. Find the UX, outcome, observability, and polish problems. Every finding must cite a specific file and line number.
</task>

<investigation>
Before producing output, explicitly trace each of the following:

1. UX COPY — Find every new string, label, button text, placeholder, error message, and notification. Is it written for users or for developers? Does every error message say WHAT went wrong AND what to do about it?

2. USER FLOWS — Trace the user-facing interactions. What happens in the empty state? The loading state? The error state?

3. METRICS AND OBSERVABILITY — Find every new user action. Is it instrumented? Is there any way to measure whether this feature is achieving its purpose?

4. DEFAULTS AND POLISH — What are the default values? Are they defaults that serve users or defaults that were easy to implement?
</investigation>

<output_schema>
Produce a single JSON object matching this schema exactly.

{
  "opening": "string",
  "findings": [
    {
      "area": "copy" | "flow" | "metrics" | "polish",
      "severity": "critical" | "concern" | "opportunity",
      "label": "string",
      "file": "string",
      "line": integer | null,
      "description": "string"
    }
  ],
  "questions": ["string"],
  "recommendation": "ship" | "fix_required" | "reject"
}
</output_schema>

<rules>
- Criticize decisions, not people.
- For copy findings: always include the actual copy and what good looks like.
- Do not manufacture problems.
</rules>

<diff>
{{DIFF}}
</diff>`;

// --- Schemas ---

const REVIEW_SCHEMA = {
  type: "OBJECT",
  properties: {
    opening: { type: "STRING" },
    findings: {
      type: "ARRAY",
      items: {
        type: "OBJECT",
        properties: {
          severity: { type: "STRING", enum: ["critical", "concern", "questionable", "simplify"] },
          label: { type: "STRING" },
          file: { type: "STRING" },
          line: { type: "INTEGER" },
          description: { type: "STRING" },
        },
        required: ["severity", "label", "file", "description"],
      },
    },
    questions: { type: "ARRAY", items: { type: "STRING" } },
    recommendation: { type: "STRING", enum: ["ship", "fix_required", "reject"] },
  },
  required: ["opening", "findings", "questions", "recommendation"],
};

const IMAGINE_SCHEMA = {
  type: "OBJECT",
  properties: {
    opening: { type: "STRING" },
    bugs: {
      type: "ARRAY",
      items: {
        type: "OBJECT",
        properties: {
          severity: { type: "STRING", enum: ["critical", "high"] },
          label: { type: "STRING" },
          file: { type: "STRING" },
          line: { type: "INTEGER" },
          scenario: { type: "STRING" },
          blast_radius: { type: "STRING" },
        },
        required: ["severity", "label", "file", "scenario", "blast_radius"],
      },
    },
    concerns: {
      type: "ARRAY",
      items: {
        type: "OBJECT",
        properties: {
          domain: { type: "STRING" },
          description: { type: "STRING" },
          file: { type: "STRING" },
          line: { type: "INTEGER" },
        },
        required: ["domain", "description", "file"],
      },
    },
    happy_path: { type: "STRING" },
    state_transitions: { type: "STRING" },
    concurrency_risks: { type: "STRING" },
    observability_gaps: {
      type: "ARRAY",
      items: {
        type: "OBJECT",
        properties: {
          type: { type: "STRING", enum: ["log", "metric", "silent_failure"] },
          description: { type: "STRING" },
          file: { type: "STRING" },
          line: { type: "INTEGER" },
        },
        required: ["type", "description", "file"],
      },
    },
    questions: { type: "ARRAY", items: { type: "STRING" } },
    recommendation: { type: "STRING", enum: ["ship", "fix_required", "reject"] },
  },
  required: ["opening", "bugs", "concerns", "happy_path", "state_transitions", "concurrency_risks", "observability_gaps", "questions", "recommendation"],
};

const EDGE_CASES_SCHEMA = {
  type: "OBJECT",
  properties: {
    opening: { type: "STRING" },
    edge_cases: {
      type: "ARRAY",
      items: {
        type: "OBJECT",
        properties: {
          category: { type: "STRING", enum: ["code", "product", "security"] },
          severity: { type: "STRING", enum: ["critical", "high", "medium"] },
          label: { type: "STRING" },
          file: { type: "STRING" },
          line: { type: "INTEGER" },
          scenario: { type: "STRING" },
          consequence: { type: "STRING" },
        },
        required: ["category", "severity", "label", "file", "scenario", "consequence"],
      },
    },
    haunting_scenarios: { type: "ARRAY", items: { type: "STRING" } },
    questions: { type: "ARRAY", items: { type: "STRING" } },
    recommendation: { type: "STRING", enum: ["ship", "fix_required", "reject"] },
  },
  required: ["opening", "edge_cases", "haunting_scenarios", "questions", "recommendation"],
};

const PRODUCT_SCHEMA = {
  type: "OBJECT",
  properties: {
    opening: { type: "STRING" },
    findings: {
      type: "ARRAY",
      items: {
        type: "OBJECT",
        properties: {
          area: { type: "STRING", enum: ["copy", "flow", "metrics", "polish"] },
          severity: { type: "STRING", enum: ["critical", "concern", "opportunity"] },
          label: { type: "STRING" },
          file: { type: "STRING" },
          line: { type: "INTEGER" },
          description: { type: "STRING" },
        },
        required: ["area", "severity", "label", "file", "description"],
      },
    },
    questions: { type: "ARRAY", items: { type: "STRING" } },
    recommendation: { type: "STRING", enum: ["ship", "fix_required", "reject"] },
  },
  required: ["opening", "findings", "questions", "recommendation"],
};

// --- Renderers ---

interface Finding {
  severity?: string;
  label?: string;
  file?: string;
  line?: number | null;
  description?: string;
  area?: string;
  category?: string;
  scenario?: string;
  consequence?: string;
  blast_radius?: string;
  domain?: string;
  type?: string;
}

function loc(item: Finding): string {
  let l = item.file ?? "unknown";
  if (item.line != null) l += `:${item.line}`;
  return l;
}

function renderReview(data: Record<string, unknown>): string {
  const lines: string[] = ["# Code Review", "", `_${data.opening ?? ""}_`, ""];
  const groups: Record<string, [string, Finding[]]> = {
    critical: ["## 🚨 Critical Issues", []],
    concern: ["## ⚠️ Serious Concerns", []],
    questionable: ["## 🤔 Questionable Decisions", []],
    simplify: ["## ✂️ Simplify This", []],
  };
  for (const f of (data.findings as Finding[]) ?? []) {
    const sev = f.severity ?? "concern";
    if (groups[sev]) groups[sev][1].push(f);
  }
  for (const sev of ["critical", "concern", "questionable", "simplify"]) {
    const [header, items] = groups[sev];
    if (!items.length) continue;
    lines.push(header, "");
    for (const f of items) lines.push(`- **${f.label ?? ""}**: ${f.description ?? ""} [${loc(f)}]`);
    lines.push("");
  }
  if ((data.questions as string[])?.length) {
    lines.push("## The Uncomfortable Questions", "");
    for (const q of data.questions as string[]) lines.push(`- ${q}`);
    lines.push("");
  }
  const rec = data.recommendation as string ?? "fix_required";
  const recMap: Record<string, string> = { ship: "Ship it (grudgingly).", fix_required: "Fix and reship.", reject: "Burn it down and start over." };
  lines.push("## Verdict", "", recMap[rec] ?? rec, "");
  return lines.join("\n");
}

function renderImagine(data: Record<string, unknown>): string {
  const lines: string[] = ["# Production Imagination", "", `_${data.opening ?? ""}_`, ""];
  const bugs = (data.bugs as Finding[]) ?? [];
  if (bugs.length) {
    lines.push("## 🚨 Bugs Found (Fix These)", "");
    for (const b of bugs) {
      lines.push(`- **[${(b.severity ?? "high").toUpperCase()}] ${b.label ?? ""}** [${loc(b)}]`);
      lines.push(`  - **Scenario**: ${b.scenario ?? ""}`);
      lines.push(`  - **Blast radius**: ${b.blast_radius ?? ""}`);
    }
    lines.push("");
  }
  const concerns = (data.concerns as Finding[]) ?? [];
  if (concerns.length) {
    lines.push("## ⚠️ Serious Concerns (Should Fix)", "");
    for (const c of concerns) lines.push(`- **${c.domain ?? ""}**: ${c.description ?? ""} [${loc(c)}]`);
    lines.push("");
  }
  if (data.happy_path) lines.push("## Happy Path Simulation", "", data.happy_path as string, "");
  if (data.state_transitions) lines.push("## State Transition Simulation", "", data.state_transitions as string, "");
  if (data.concurrency_risks) lines.push("## Concurrency Simulation", "", data.concurrency_risks as string, "");
  const obs = (data.observability_gaps as Finding[]) ?? [];
  if (obs.length) {
    const typeLabels: Record<string, string> = { log: "Logging", metric: "Metric", silent_failure: "Silent Failure" };
    lines.push("## Observability Report", "");
    for (const o of obs) lines.push(`- **[${typeLabels[o.type ?? ""] ?? o.type ?? ""}]**: ${o.description ?? ""} [${loc(o)}]`);
    lines.push("");
  }
  if ((data.questions as string[])?.length) {
    lines.push("## Questions", "");
    for (const q of data.questions as string[]) lines.push(`- ${q}`);
    lines.push("");
  }
  const rec = data.recommendation as string ?? "fix_required";
  const recMap: Record<string, string> = { ship: "Safe to ship.", fix_required: "Fix required before shipping.", reject: "Do not ship — reject and redesign." };
  lines.push("## Verdict", "", recMap[rec] ?? rec, "");
  return lines.join("\n");
}

function renderEdgeCases(data: Record<string, unknown>): string {
  const lines: string[] = ["# Edge Case Analysis", "", `_${data.opening ?? ""}_`, ""];
  const sevOrder = ["critical", "high", "medium"];
  const sevHeaders: Record<string, string> = { critical: "## 🚨 Will Break", high: "## ⚠️ Will Eventually Break", medium: "## 🤔 Probably Fine Until It Isn't" };
  const bySev: Record<string, Finding[]> = { critical: [], high: [], medium: [] };
  for (const ec of (data.edge_cases as Finding[]) ?? []) {
    const sev = ec.severity ?? "medium";
    if (bySev[sev]) bySev[sev].push(ec);
  }
  for (const sev of sevOrder) {
    if (!bySev[sev].length) continue;
    lines.push(sevHeaders[sev], "");
    for (const ec of bySev[sev]) {
      lines.push(`- **[${ec.category ?? "code"}] ${ec.label ?? ""}** [${loc(ec)}]`);
      lines.push(`  - **Scenario**: ${ec.scenario ?? ""}`);
      lines.push(`  - **Consequence**: ${ec.consequence ?? ""}`);
    }
    lines.push("");
  }
  const haunting = (data.haunting_scenarios as string[]) ?? [];
  if (haunting.length) {
    lines.push("## The Scenarios That Will Haunt You", "");
    for (const s of haunting) lines.push(`- ${s}`);
    lines.push("");
  }
  if ((data.questions as string[])?.length) {
    lines.push("## Questions", "");
    for (const q of data.questions as string[]) lines.push(`- ${q}`);
    lines.push("");
  }
  const rec = data.recommendation as string ?? "fix_required";
  const recMap: Record<string, string> = { ship: "Ship it (grudgingly).", fix_required: "Fix and reship.", reject: "Burn it down and start over." };
  lines.push("## Verdict", "", recMap[rec] ?? rec, "");
  return lines.join("\n");
}

function renderProduct(data: Record<string, unknown>): string {
  const lines: string[] = ["# Product Review", "", `_${data.opening ?? ""}_`, ""];
  const areaLabels: Record<string, string> = { copy: "Copy", flow: "Flow", metrics: "Metrics", polish: "Polish" };
  const sevHeaders: Record<string, string> = { critical: "## 🚨 Critical Product Issues", concern: "## ⚠️ Will Cost You Users", opportunity: "## 🤔 Raise the Bar" };
  const bySev: Record<string, Finding[]> = { critical: [], concern: [], opportunity: [] };
  for (const f of (data.findings as Finding[]) ?? []) {
    const sev = f.severity ?? "concern";
    if (bySev[sev]) bySev[sev].push(f);
  }
  for (const sev of ["critical", "concern", "opportunity"]) {
    if (!bySev[sev].length) continue;
    lines.push(sevHeaders[sev], "");
    for (const f of bySev[sev]) {
      const area = areaLabels[f.area ?? ""] ?? f.area ?? "";
      lines.push(`- **[${area}] ${f.label ?? ""}**: ${f.description ?? ""} [${loc(f)}]`);
    }
    lines.push("");
  }
  if ((data.questions as string[])?.length) {
    lines.push("## The Uncomfortable Questions", "");
    for (const q of data.questions as string[]) lines.push(`- ${q}`);
    lines.push("");
  }
  const rec = data.recommendation as string ?? "fix_required";
  const recMap: Record<string, string> = { ship: "Ships quality.", fix_required: "Fix before shipping.", reject: "Needs rethinking." };
  lines.push("## Verdict", "", recMap[rec] ?? rec, "");
  return lines.join("\n");
}

// --- API ---

type CommandConfig = {
  prompt: string;
  schema: Record<string, unknown>;
  render: (data: Record<string, unknown>) => string;
};

const COMMANDS: Record<string, CommandConfig> = {
  review: { prompt: REVIEW_PROMPT, schema: REVIEW_SCHEMA, render: renderReview },
  imagine: { prompt: IMAGINE_PROMPT, schema: IMAGINE_SCHEMA, render: renderImagine },
  "edge-cases": { prompt: EDGE_CASES_PROMPT, schema: EDGE_CASES_SCHEMA, render: renderEdgeCases },
  product: { prompt: PRODUCT_PROMPT, schema: PRODUCT_SCHEMA, render: renderProduct },
};

async function callGemini(prompt: string, schema: Record<string, unknown>): Promise<string> {
  const apiKey = process.env.GEMINI_API_KEY ?? process.env.GRUMPY_GEMINI_KEY;
  if (!apiKey) {
    console.error("Error: GEMINI_API_KEY is not set. Export it in your shell: export GEMINI_API_KEY=your-key-here");
    process.exit(1);
  }

  const url = `https://generativelanguage.googleapis.com/v1beta/models/${MODEL}:streamGenerateContent?alt=sse&key=${apiKey}`;

  const body = JSON.stringify({
    contents: [{ role: "user", parts: [{ text: prompt }] }],
    generationConfig: {
      responseMimeType: "application/json",
      responseSchema: schema,
      thinkingConfig: { thinkingBudget: -1 },
    },
  });

  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
    signal: AbortSignal.timeout(300_000),
  });

  if (!resp.ok) {
    const errText = await resp.text();
    let errMsg = errText;
    try {
      const errData = JSON.parse(errText);
      errMsg = errData.error?.message ?? errText;
    } catch {}
    console.error(`Gemini API error ${resp.status}: ${errMsg}`);
    process.exit(1);
  }

  const chunks: string[] = [];
  let usage: Record<string, unknown> | null = null;
  const reader = resp.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop()!;
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed.startsWith("data: ")) continue;
      const dataStr = trimmed.slice(6);
      if (dataStr === "[DONE]") break;
      try {
        const event = JSON.parse(dataStr);
        if (event.usageMetadata) usage = event.usageMetadata;
        for (const cand of event.candidates ?? []) {
          for (const part of cand.content?.parts ?? []) {
            if ("text" in part && !part.thought) chunks.push(part.text);
          }
        }
      } catch {}
    }
  }

  if (usage) {
    const input = (usage.promptTokenCount as number) ?? 0;
    const thinking = (usage.thoughtsTokenCount as number) ?? 0;
    const output = (usage.candidatesTokenCount as number) ?? 0;
    const total = (usage.totalTokenCount as number) ?? input + thinking + output;
    console.error(`tokens: ${input} in / ${thinking} thinking / ${output} out / ${total} total`);
  }

  return chunks.join("");
}

// --- Main ---

async function main() {
  const command = process.argv[2]?.toLowerCase();
  if (!command || !(command in COMMANDS)) {
    console.error(`Usage: bun gemini.ts <${Object.keys(COMMANDS).join("|")}>`);
    process.exit(1);
  }

  const diff = await Bun.stdin.text();
  if (!diff.trim()) {
    console.error("Empty diff on stdin — nothing to review.");
    process.exit(1);
  }

  const { prompt: promptTemplate, schema, render } = COMMANDS[command];
  const raw = await callGemini(promptTemplate.replace("{{DIFF}}", diff), schema);

  if (!raw.trim()) {
    console.error("Error: Gemini returned an empty response. Try again.");
    process.exit(1);
  }

  let data: Record<string, unknown>;
  try {
    data = JSON.parse(raw);
  } catch {
    const m = raw.match(/```(?:json)?\s*(\{.*?\})\s*```/s);
    if (m) {
      try {
        data = JSON.parse(m[1]);
      } catch {
        console.error(raw);
        console.error("Error: unparseable output — raw response printed above.");
        process.exit(1);
      }
    } else {
      console.error(raw);
      console.error("Error: unparseable output — raw response printed above.");
      process.exit(1);
    }
  }

  console.log(render(data));
}

main().catch((err) => {
  if (err.name === "TimeoutError") {
    console.error("Error: Gemini request timed out. Try with a smaller diff.");
  } else {
    console.error(`Error: ${err.message}`);
  }
  process.exit(1);
});
