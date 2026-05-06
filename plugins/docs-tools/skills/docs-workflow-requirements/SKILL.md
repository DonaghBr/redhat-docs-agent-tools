---
name: docs-workflow-requirements
description: Analyze documentation requirements for a JIRA ticket using a programmatic graph walk + LLM distillation pipeline. Stage 1 walks the JIRA hierarchy via script. Stage 2 filters and summarizes via two Sonnet subagents. Stage 3 fans out analyst agents per requirement for deep analysis. Assembles the standard requirements.md output. Invoked by the orchestrator.
argument-hint: <ticket> --base-path <path> [--pr <url>]... [--repo <path>]
allowed-tools: Read, Write, Glob, Grep, Edit, Bash, Skill, Agent, WebSearch, WebFetch
---

# Requirements Analysis Step

Step skill for the docs-orchestrator pipeline. Follows the step skill contract: **parse args → run pipeline → write output**.

This skill uses a four-stage pipeline to analyze documentation requirements:

1. **Stage 1 — Graph Walk** (Python script, no LLM) — Programmatically walks JIRA from the central ticket up to the STRAT, down through all children, and sideways through issue links. Extracts repos/PRs from collected data. Outputs `graph-raw.json` and `discovered_repos.json`
2. **Stage 2 — Distillation** (2 Sonnet subagents, sequential) — Filters for relevance, then summarizes into `jiracontext.json`
3. **Stage 3 — Deep Analysis** (analyst agents, parallel) — One agent per requirement from `jiracontext.json`, each performing web search + code verification
4. **Stage 4 — Merge** — Assembles per-requirement JSON results into the standard `requirements.md` format

## Arguments

- `$1` — JIRA ticket ID (required)
- `--base-path <path>` — Base output path (e.g., `.claude/docs/proj-123`)
- `--pr <url>` — PR/MR URL to include in analysis (repeatable)
- `--repo <path>` — Source code repo path (optional, passed to analyst agents for code verification)

## Output

```
<base-path>/requirements/requirements.md
<base-path>/requirements/step-result.json
<base-path>/requirements/graph-raw.json           (debugging artifact)
<base-path>/requirements/discovered_repos.json     (consumed by resolve_source.py)
<base-path>/requirements/relevant-keys.json        (debugging artifact)
<base-path>/requirements/jiracontext.json           (debugging artifact)
```

## Execution

### 1. Parse arguments

Extract the ticket ID, `--base-path`, any `--pr` URLs, and optional `--repo` from the args string.

Set the output path:

```bash
OUTPUT_DIR="${BASE_PATH}/requirements"
OUTPUT_FILE="${OUTPUT_DIR}/requirements.md"
mkdir -p "$OUTPUT_DIR"
```

### 2. Resume check

Check for existing artifacts to determine where to resume:

- If `jiracontext.json` exists → skip to Stage 3
- If `relevant-keys.json` exists → skip to Stage 2b (summarization)
- If `graph-raw.json` exists → skip to Stage 2a (relevance filter)
- Otherwise → start from Stage 1

### 3. Stage 1 — Graph Walk

Run the graph walker script to traverse the JIRA hierarchy and extract repos/PRs:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/jira_graph_walker.py \
  --ticket <TICKET> \
  --base-path <BASE_PATH> \
  [--pr <url>...]
```

The `--pr` arguments are conditional — include only if PR URLs were provided.

After the script completes, validate that both output files exist:
- `<OUTPUT_DIR>/graph-raw.json`
- `<OUTPUT_DIR>/discovered_repos.json`

If the script fails (non-zero exit), STOP and report the error.

Read `graph-raw.json` to extract the central ticket summary for use in Stage 2 prompts.

### 4. Stage 2a — Relevance Filter

Dispatch a Sonnet subagent to filter tickets for documentation relevance:

```
Agent(
  model: "sonnet",
  description: "Filter JIRA tickets for documentation relevance",
  prompt: |
    You are filtering JIRA tickets for documentation relevance. The central ticket is {TICKET} — "{SUMMARY}". A documentation writer needs to understand what changed, why, and what users need to know.

    Read the ticket graph at {BASE_PATH}/requirements/graph-raw.json.

    For each ticket, decide: does this ticket contain information that a technical writer would need to document the feature described by the central ticket?

    Include tickets that:
    - Describe the same feature or a component of it
    - Define requirements, acceptance criteria, or scope
    - Track implementation work (engineering tasks, PRs) for the feature
    - Provide strategic context (the STRAT, parent epics)

    Exclude tickets that:
    - Belong to unrelated features that happen to share a parent
    - Are administrative (sprint planning, backlog grooming)
    - Are in Rejected/Won't Do status with no useful context

    Write a JSON object to {BASE_PATH}/requirements/relevant-keys.json:
    {"relevant_tickets": ["KEY-1", "KEY-2", ...], "excluded_count": N, "reasoning": "one sentence on what you filtered out"}
)
```

After the agent completes, validate that `relevant-keys.json` exists and contains a `relevant_tickets` array.

### 5. Stage 2b — Summarization

Dispatch a Sonnet subagent to distill context for the analyst agents:

```
Agent(
  model: "sonnet",
  description: "Distill JIRA context for documentation",
  prompt: |
    You are preparing context for documentation analyst agents. The central ticket is {TICKET} — "{SUMMARY}".

    Read the relevant ticket keys from {BASE_PATH}/requirements/relevant-keys.json, then read the full ticket data from {BASE_PATH}/requirements/graph-raw.json. Extract only the tickets whose keys appear in the relevant list.

    For the STRAT (top-level ancestor), extract:
    - Feature name and strategic goal
    - Target release / fix version
    - High-level scope

    For each relevant ticket, extract:
    - What changed or is being implemented
    - Why (user need, business driver, technical motivation)
    - User-facing impact (what a user sees, does differently, or needs to know)
    - Technical details relevant to documentation (API changes, CLI flags, config options, behavioral changes)
    - Any acceptance criteria or definition of done

    Group related tickets into single requirements where they describe the same user-facing change.

    Write the result to {BASE_PATH}/requirements/jiracontext.json with this schema:
    {
      "central_ticket": "<TICKET>",
      "strat": {"key": "...", "summary": "...", "goal": "...", "release": "..."},
      "requirements": [
        {
          "id": "req-1",
          "source_tickets": ["KEY-1", "KEY-2"],
          "title": "...",
          "what_changed": "...",
          "why": "...",
          "user_impact": "...",
          "technical_details": "...",
          "acceptance_criteria": ["...", "..."]
        }
      ],
      "context": {"product": "...", "release": "...", "related_features": ["..."]}
    }
)
```

After the agent completes, validate that `jiracontext.json` exists and contains a `requirements` array.

If `requirements` is empty, write a minimal `requirements.md` noting that no requirements were found, write `step-result.json`, and exit successfully.

### 6. Stage 3 — Deep Analysis

Read `jiracontext.json` and dispatch one `requirements-analyst` agent per requirement. Launch ALL agents in a **single message** (parallel execution).

For each requirement, use:

```
Agent:
  subagent_type: requirements-analyst
  description: "Analyze REQ-NNN: <title truncated to 40 chars>"
  prompt: |
    Perform deep analysis of this single documentation requirement.

    REQUIREMENT:
    <JSON of the requirement object from jiracontext.json>

    STRAT CONTEXT:
    <JSON of the strat object from jiracontext.json>

    PRODUCT CONTEXT:
    <JSON of the context object from jiracontext.json>

    DISCOVERED REPOS:
    <JSON from discovered_repos.json — so the analyst knows what repos exist>

    [If --repo was provided: "REPO_PATH: <repo_path>"]

    Enrich this requirement with web search (2-4 queries for upstream docs,
    RFCs, blog posts) and optional code verification (if REPO_PATH provided).

    Print your JSON result to stdout.
```

The `REPO_PATH` line is conditional — include it only if `--repo` was passed to this step.

**Important:** All Agent calls MUST be in a single message so they run in parallel.

### 7. Merge results

Each agent returns a JSON object (or text containing a JSON object). Parse each agent's response to extract the JSON.

If an agent's response is not valid JSON or is missing the `id` field, create a fallback entry. Carry forward the requirement's source ticket references so the requirement retains traceability even on failure:

```json
{
  "id": "<expected REQ-NNN>",
  "title": "<expected title from jiracontext>",
  "error": "Agent did not return valid JSON",
  "priority": "medium",
  "category": "feature",
  "sources": [
    {"label": "<source_ticket>", "url": "https://redhat.atlassian.net/browse/<source_ticket>", "note": "From jiracontext (deep analysis failed)"}
  ],
  "summary": "<what_changed from jiracontext>",
  "user_impact": "<user_impact from jiracontext>",
  "scope": null,
  "documentation_actions": [],
  "acceptance_criteria": [],
  "references": [],
  "web_findings": [],
  "is_breaking_change": false,
  "deprecation_version": null,
  "notes": "Deep analysis failed — using jiracontext data only"
}
```

Collect all per-requirement results into a list ordered by requirement ID.

### 8. Assemble requirements.md

Write `<OUTPUT_FILE>` by assembling the merged results into the standard requirements format. The document structure must match the existing output contract exactly:

```markdown
# Documentation Requirements

**Source**: <ticket summary from jiracontext strat or central ticket>
**Date**: <YYYY-MM-DD>
**Release/Sprint**: <release from jiracontext>

## Summary

- Total requirements analyzed: <count>
- New modules needed: <count documentation_actions with action "Create">
- Existing modules to update: <count documentation_actions with action "Update">
- Breaking changes requiring docs: <count where is_breaking_change is true>

## Requirements by priority

### Critical

#### REQ-001: [title]
- **Source**: [label](url) | [label](url)
- **Summary**: [summary]
- **User impact**: [user_impact]
- **Documentation action**:
  - [ ] [action] `[file]` ([type]) [note if present]
- **Acceptance criteria**:
  - [ ] [criterion]
- **References**:
  - [label](url): [note]

### High
[Same format, requirements with priority "high"]

### Medium
[Same format, requirements with priority "medium"]

### Low
[Same format, requirements with priority "low"]

## Documentation scope

### New documentation needed

| Requirement | Scope | References |
|-------------|-------|------------|
| REQ-XXX | [From documentation_actions where action is "Create"] | [source labels] |

### Existing documentation to update

| Requirement | What changed | References |
|-------------|-------------|------------|
| REQ-XXX | [From documentation_actions where action is "Update"] | [source labels] |

## Breaking changes

[Table of requirements where is_breaking_change is true. Omit section if none.]

| Change | Migration steps needed | Deprecation notice | References |
|--------|------------------------|-------------------|------------|

## Notes

[Aggregate any non-null notes from requirements. Omit section if none.]

## Sources consulted

### JIRA tickets
[From source_tickets across all requirements in jiracontext — deduplicated]

### Pull requests / Merge requests
[From discovered_repos.json PR URLs — deduplicated]

### Code files
[From references with type "code" across all requirements — deduplicated]

### External references
[From references without type "code" — deduplicated]

### Web search findings
[From web_findings across all requirements — deduplicated by URL]
```

**Priority section rules:**
- Only include priority sections that have requirements (omit empty `### High` if no high-priority requirements)
- Requirements with errors should be included under their original priority with a note: `**Note:** Deep analysis failed for this requirement. Jiracontext data only.`

**Deduplication:** Sources consulted and references are gathered across all per-requirement results. Deduplicate by URL or file path.

### 9. Write step-result.json

Run the title-extraction script:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/parse_title.py "<OUTPUT_FILE>"
```

The script prints `{"title": "..."}` to stdout. If it exits non-zero, report the stderr message as an error.

Use the `title` value from the script's JSON output to write the sidecar to `<OUTPUT_DIR>/step-result.json`:

```json
{
  "schema_version": 1,
  "step": "requirements",
  "ticket": "<TICKET>",
  "completed_at": "<current ISO 8601 timestamp>",
  "title": "<first heading, max 80 chars>"
}
```

### 10. Verify output

Verify that `<OUTPUT_FILE>` and `<OUTPUT_DIR>/step-result.json` exist.

## Notes

- **Four-stage architecture:** Stage 1 (graph walk) is a Python script with no LLM calls. Stage 2 (distillation) uses two lightweight Sonnet agents. Stage 3 (deep analysis) uses analyst agents for web search and code verification. Stage 4 (merge) is deterministic assembly
- **Context isolation:** Each deep-analysis agent sees only one requirement. This prevents context degradation when analyzing tickets with many requirements
- **Parallel execution:** All Stage 3 agents are dispatched in a single message for parallel execution
- **Error isolation:** A failed deep-analysis agent does not block other requirements — the merge step uses jiracontext data as a fallback
- **Output contract:** The assembled `requirements.md` is identical in format to the previous output. Downstream consumers (scope-req-audit, planning, orchestrator) see no change
- **Resume support:** Each stage writes an artifact file. On resume, the skill checks for existing artifacts and skips completed stages
- **Discovered repos:** The `discovered_repos.json` file is consumed by `resolve_source.py` (Priority 4b) for cloning repos used by downstream steps (code-evidence, writing)
