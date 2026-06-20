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

import json
import re

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


# --------------------------------------------------------------------------- #
# Verify gate (BUILD phase) — an INDEPENDENT reviewer that prefers to verify by
# RUNNING/checking (not just reading) before the one-shot submit.
# --------------------------------------------------------------------------- #
VERIFY_SYSTEM = (
    "You are a STRICT, INDEPENDENT reviewer in the Agent Arena. You did NOT write the "
    "candidate answer — do not assume it is correct. Given a TASK and a CANDIDATE ANSWER, "
    "decide whether it is correct, complete, and in a form that scores >= 70/100.\n"
    "VERIFY BY DOING, not by reading: if it is code/computation and you have a code "
    "execution tool, RUN it on the requirements + edge cases; if it has factual claims and "
    "you have web tools, check them. Judge by reading ONLY when you cannot run anything.\n"
    "Respond with ONLY a JSON object and nothing else:\n"
    '{"pass": true|false, "score_estimate": <int 0-100>, "reason": "<one line>", '
    '"fix_hint": "<one concrete fix if not passing, else empty>"}'
)


def build_verify_prompt(task: dict, draft: str) -> str:
    """Reviewer's user message: the task + the candidate answer to check."""
    return (
        f"TASK (Level {task.get('level', '?')}, {task.get('points', '?')} points)\n"
        f"Title: {task.get('title', '')}\n\n"
        f"Description:\n{task.get('description', '')}\n\n"
        f"CANDIDATE ANSWER (review this — you did NOT write it):\n{draft}\n\n"
        "Verify it (run/check wherever you can). Output ONLY the JSON verdict."
    )


def build_revision_prompt(task: dict, draft: str, fix_hint: str) -> str:
    """Re-solve prompt carrying the reviewer's concrete fix."""
    return (
        f"{build_task_prompt(task)}\n\n"
        f"YOUR PREVIOUS ATTEMPT:\n{draft}\n\n"
        f"An independent reviewer found a problem — FIX IT:\n{fix_hint}\n\n"
        "Output ONLY the corrected final answer."
    )


def parse_verdict(text: str) -> dict:
    """Parse the reviewer's JSON verdict (bare or embedded in prose). Defaults to PASS on
    anything unparseable so a critic glitch never BLOCKS a submit (the gate only helps)."""
    default = {"pass": True, "score_estimate": None, "reason": "", "fix_hint": ""}
    data = None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}", text or "", re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                data = None
    if not isinstance(data, dict):
        return default
    est = data.get("score_estimate")
    return {
        "pass": bool(data.get("pass", True)),
        "score_estimate": int(est) if isinstance(est, (int, float)) else None,
        "reason": str(data.get("reason", "")),
        "fix_hint": str(data.get("fix_hint", "")),
    }
