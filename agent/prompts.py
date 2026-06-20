"""
Task prompt construction for the arena solver.

Kept here (out of the orchestrator) so the logic is unit-testable:
- `detect_task_type(title, description)` — a keyword classifier over the arena's task
  categories. In the SETUP baseline this is INFORMATIONAL only (used to tag logs); the
  BUILD phase wires it to per-category tool/effort routing (see arena/MASTER-PLAN.md §4).
- `build_task_prompt(task)` — wraps a fetched task in an ANALYZE -> SOLVE -> REVIEW frame
  and demands a final-answer-only response, so reasoning never leaks into the graded content.
"""

from __future__ import annotations

# Arena task categories (arena/MCP-DOCS.md §04) + a general fallback.
# Order matters: the first pattern that matches wins.
TASK_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("code_synthesis", ("code", "function", "implement", "program", "script",
                        "algorithm", "debug", "refactor", "lint", "compile")),
    ("data_extraction", ("extract", "parse", "entity", "table", "csv", "json",
                         "scrape", "record", "unstructured")),
    ("context_awareness", ("summariz", "summary", "document", "article", "passage",
                           "retriev", "long-form", "according to")),
    ("logic_reason", ("logic", "prove", "proof", "deduce", "puzzle", "reason",
                      "math", "calculate", "compute", "spatial", "infer")),
]

GENERAL = "general"


# --------------------------------------------------------------------------- #
# Per-category server-tool routing (BUILD phase).
#
# Anthropic server tools (run server-side; results return as *_tool_result blocks
# inside resp.content). HARD RULE: never co-declare code_execution with the dated
# web tools in one request (documented misconfig — "a second execution environment
# confuses the model"). One toolset per request.
#
# Tool identifiers verified against the claude-api skill (Server Tools QR + the
# Python tool-use doc): code_execution_20260120 (Opus 4.5+, NON-beta path; result
# block bash_code_execution_tool_result) and web_search_20260209 / web_fetch_20260209
# (dynamic-filtering variants, Opus 4.8).
# --------------------------------------------------------------------------- #
CODE_TOOLS: list[dict] = [
    {"type": "code_execution_20260120", "name": "code_execution"},
]
WEB_TOOLS: list[dict] = [
    {"type": "web_search_20260209", "name": "web_search"},
    {"type": "web_fetch_20260209", "name": "web_fetch"},
]

TOOLSET_BY_CATEGORY: dict[str, list[dict]] = {
    "code_synthesis": CODE_TOOLS,    # write + run + verify code
    "logic_reason": CODE_TOOLS,      # compute / check via code
    "data_extraction": CODE_TOOLS,   # parse / transform via code
    "context_awareness": WEB_TOOLS,  # grounding
    "grounded": WEB_TOOLS,           # explicit grounded-search route
    GENERAL: [],                     # no tools — direct answer
}


def toolset_for_category(category: str) -> list[dict]:
    """Map an arena category to the Anthropic server-tool spec list for the request.

    Kept here (no agent dependency) so routing is unit-testable in one place and
    agent.py can import the map without a circular import.
    """
    return TOOLSET_BY_CATEGORY.get(category, [])


def detect_task_type(title: str, description: str) -> str:
    """Best-effort keyword classification into an arena category.

    SETUP: informational only (log tagging). BUILD: drives tool/effort routing.
    """
    hay = f"{title}\n{description}".lower()
    for category, keywords in TASK_PATTERNS:
        if any(k in hay for k in keywords):
            return category
    return GENERAL


def build_task_prompt(task: dict) -> str:
    """Wrap a fetched arena task in an ANALYZE -> SOLVE -> REVIEW frame.

    `task` is the dict from get_tasks; fields are read defensively (id, title,
    description, level, points). Returns the user message for `Agent.run()`.
    """
    title = task.get("title", "(untitled)")
    description = task.get("description", "")
    level = task.get("level", "?")
    points = task.get("points", "?")

    return (
        f"TASK (Level {level}, {points} points)\n"
        f"Title: {title}\n\n"
        f"Description:\n{description}\n\n"
        "Work through it: (1) ANALYZE the requirements and edge cases, (2) SOLVE it "
        "completely and correctly, (3) REVIEW your solution for correctness before "
        "finalizing.\n\n"
        "Respond with ONLY the final solution/answer — no analysis narration, no "
        "preamble, no 'Here is'. Your response is graded verbatim, so make it complete "
        "and self-contained."
    )
