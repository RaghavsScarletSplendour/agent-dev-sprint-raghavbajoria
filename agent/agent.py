"""
Agent Arena — inner solver (Claude ReAct core).

A minimal, dependency-light ReAct agent (Perception -> Reasoning -> Tool-Calling) on a
manual agentic loop so we control every iteration (logging, loop guards). The arena
orchestrator (arena/orchestrator.py) drives this per task: it passes a task prompt to
`Agent.run()` and gets back the final answer to submit.

The loop guards that keep us safe in a long run:
  1. MAX_STEPS            -> a hard ceiling; never loops forever.
  2. MAX_REPEAT_ACTIONS   -> kills "agent stuck calling the same tool".
Errors from tools come back as tool_results (text), never crashes.

Run the full arena loop with:  python -m arena.orchestrator
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic

# Latest + most capable Claude model. Adaptive thinking lets the model decide how
# hard to think per turn; effort tunes the cost/quality dial for the arena.
MODEL = "claude-opus-4-8"
MAX_STEPS = 12            # hard ceiling on the agentic loop — the #1 loop guard
MAX_REPEAT_ACTIONS = 2    # same (tool, args) this many times in a row -> stop

# max_tokens per turn. Tool-using turns (server-side code execution / web grounding)
# need headroom for tool output + reasoning; no-tool turns stay tight to keep the
# arena fast/cheap. 8192 stays well under the SDK non-streaming HTTP timeout.
MAX_TOKENS_DEFAULT = 4096
MAX_TOKENS_TOOLS = 8192

# Default effort floor (BUILD TARGET 3). Raised off the old hardcoded "medium".
# output_config.effort ∈ low|medium|high|xhigh|max. The orchestrator bumps this
# per task/level (xhigh for code/logic and high arena levels).
DEFAULT_EFFORT = "high"


# --------------------------------------------------------------------------- #
# Tool registry — decorate a function to expose it to the agent.
# Each tool needs a JSON-schema description so the model knows when to call it.
# --------------------------------------------------------------------------- #
@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[..., Any]


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, name: str, description: str, input_schema: dict[str, Any]):
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._tools[name] = Tool(name, description, input_schema, fn)
            return fn
        return deco

    def specs(self) -> list[dict[str, Any]]:
        # Stable order keeps the prompt prefix byte-identical -> prompt cache hits.
        return [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in sorted(self._tools.values(), key=lambda t: t.name)
        ]

    def run(self, name: str, args: dict[str, Any]) -> str:
        if name not in self._tools:
            return f"Error: unknown tool {name!r}"
        try:
            return str(self._tools[name].fn(**args))
        except Exception as e:  # tool errors come back as text, not crashes
            return f"Error running {name}: {e}"


tools = Registry()


# --------------------------------------------------------------------------- #
# No tools are registered in the SETUP baseline: the model answers each task
# directly in one turn (tools.specs() -> []). The point-getter tools
# (code_execution, web_search/web_fetch, a safe-AST calc, ...) are wired in the
# BUILD phase — see arena/MASTER-PLAN.md §5.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# The agent loop.
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = (
    "You are an expert problem-solver competing in the Agent Arena, where an AI evaluator "
    "scores each answer 0-100 on correctness, completeness, and rigor.\n\n"
    "For the task you are given:\n"
    "1. ANALYZE — restate the problem, surface every explicit and implicit requirement, "
    "and note edge cases.\n"
    "2. SOLVE — produce a complete, correct, self-contained solution. Make reasonable "
    "assumptions and state them; never ask for clarification.\n"
    "3. REVIEW — before finalizing, verify correctness, completeness, and edge cases.\n\n"
    "Then output ONLY the final answer/solution — no analysis narration, no preamble, no "
    "'Here is...'. The text you return IS what gets graded, so it must stand on its own. "
    "Be thorough but do not pad."
)


@dataclass
class Agent:
    client: anthropic.Anthropic = field(default_factory=anthropic.Anthropic)
    system: str = SYSTEM_PROMPT
    last_usage: Any = None  # resp.usage from the most recent call (for the build-phase ledger)

    def run(self, task: str, *, effort: str = DEFAULT_EFFORT,
            server_tools: list[dict] | None = None) -> str:
        """Solve one task.

        effort        — output_config.effort floor (orchestrator-tunable per task/level).
        server_tools  — the per-category Anthropic server-tool specs the orchestrator
                        selected (code_execution ALONE, or web_search+web_fetch ALONE —
                        never mixed). None -> the client-tool registry (empty baseline),
                        which keeps the no-tool direct-answer path intact.

        Finality is stop_reason-driven, not "no client tool_use": server-tool turns and
        pause_turn turns have no client tool_use block but are NOT final. We append the
        assistant turn unconditionally, re-send on pause_turn to resume the server-side
        loop, and only return the final text on a normal stop.
        """
        active_tools = tools.specs() if server_tools is None else server_tools
        max_tokens = MAX_TOKENS_TOOLS if active_tools else MAX_TOKENS_DEFAULT

        messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
        last_action: tuple[str, str] | None = None
        repeat_count = 0

        for step in range(1, MAX_STEPS + 1):
            resp = self.client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=self.system,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                tools=active_tools,
                messages=messages,
            )
            self.last_usage = getattr(resp, "usage", None)

            if resp.stop_reason == "refusal":
                return "[agent refused]"

            # ALWAYS append the assistant turn before deciding anything — the appended
            # turn carries any server_tool_use / *_tool_result blocks back on resume.
            messages.append({"role": "assistant", "content": resp.content})

            # Server-side tool loop hit its per-turn cap: re-send messages as-is (no new
            # user message) so the server resumes. MAX_STEPS still bounds the resumes.
            if resp.stop_reason == "pause_turn":
                continue

            # Only CLIENT-executed registry tools need a tool_result from us. Server-tool
            # result blocks are already resolved server-side and sit in resp.content.
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                # end_turn (or any non-pause, non-client-tool stop) -> final answer.
                # Take the LAST text block: server-tool turns interleave text + result
                # blocks, and the text after the last tool result is the graded answer.
                texts = [b.text for b in resp.content if b.type == "text"]
                if texts:
                    return texts[-1]
                # Pure tool turn with no text and not a pause — keep looping rather than
                # returning "" (safe default per the spec).
                continue

            results = []
            for tu in tool_uses:
                # Loop guard: detect the same (tool, args) fired back-to-back.
                sig = (tu.name, json.dumps(tu.input, sort_keys=True))
                if sig == last_action:
                    repeat_count += 1
                else:
                    repeat_count, last_action = 0, sig

                if repeat_count >= MAX_REPEAT_ACTIONS:
                    out = ("STOP: you have repeated this exact action too many times. "
                           "Change strategy or give your final answer.")
                else:
                    out = tools.run(tu.name, tu.input)

                print(f"  step {step}: {tu.name}({tu.input}) -> {out[:120]}")
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})

            messages.append({"role": "user", "content": results})

        return "[hit MAX_STEPS — agent did not converge]"


if __name__ == "__main__":
    # This module is the inner solver, driven per-task by arena/orchestrator.py.
    print("agent.py is a library. Run the arena loop with: python -m arena.orchestrator")
