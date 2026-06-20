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

    def run(self, task: str) -> str:
        messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
        last_action: tuple[str, str] | None = None
        repeat_count = 0

        for step in range(1, MAX_STEPS + 1):
            resp = self.client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=self.system,
                thinking={"type": "adaptive"},
                output_config={"effort": "medium"},  # fast, cheap arena turns
                tools=tools.specs(),
                messages=messages,
            )
            self.last_usage = getattr(resp, "usage", None)

            if resp.stop_reason == "refusal":
                return "[agent refused]"
            # TODO(build): handle stop_reason == "pause_turn" (re-send to resume) once
            # server-side tools (code_execution / web_search) are added. With no tools
            # registered in the setup baseline, pause_turn cannot occur.

            messages.append({"role": "assistant", "content": resp.content})

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:  # no tools requested -> final answer
                return next((b.text for b in resp.content if b.type == "text"), "")

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
