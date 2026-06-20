"""
Agent Dev-Sprint — battle-ready agent core.

A minimal, dependency-light ReAct agent: Perception -> Reasoning -> Tool-Calling,
on a manual agentic loop so we control every iteration (logging, loop guards,
human-in-the-loop). Built to be ADAPTED once the starter kit lands — swap the
tools, swap the system prompt, point `act()` at the arena endpoint.

The three things that win the arena and that most teams get wrong are all here:
  1. A hard step cap                  -> never loops forever.
  2. Repeat-action detection          -> kills "agent stuck calling the same tool".
  3. Tight context + cheap decisions  -> fast, cheap turns in a real-time arena.

Run:  ANTHROPIC_API_KEY=sk-... python agent.py
"""

from __future__ import annotations

import json
import os
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
# Example tools — DELETE THESE and register the arena's real tools/APIs.
# --------------------------------------------------------------------------- #
@tools.add(
    "calc",
    "Evaluate a basic arithmetic expression and return the numeric result.",
    {
        "type": "object",
        "properties": {"expression": {"type": "string", "description": "e.g. '2 * (3 + 4)'"}},
        "required": ["expression"],
        "additionalProperties": False,
    },
)
def _calc(expression: str) -> float:
    # Arena tools will hit real APIs; this is just a placeholder.
    import ast
    import operator as op
    ops = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul,
           ast.Div: op.truediv, ast.Pow: op.pow, ast.USub: op.neg}

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.BinOp):
            return ops[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp):
            return ops[type(node.op)](ev(node.operand))
        raise ValueError("unsupported expression")

    return ev(ast.parse(expression, mode="eval").body)


# --------------------------------------------------------------------------- #
# The agent loop.
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = (
    "You are an autonomous agent competing in a live, real-time arena. "
    "Perceive the current state, reason briefly with a clear plan, then act via tools. "
    "Prefer decisive single moves over long deliberation — turns are time- and token-budgeted. "
    "Never repeat a tool call that already failed or that returned the same result; "
    "change your approach instead. When the task is done, state the final answer plainly."
)


@dataclass
class Agent:
    client: anthropic.Anthropic = field(default_factory=anthropic.Anthropic)
    system: str = SYSTEM_PROMPT

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

            if resp.stop_reason == "refusal":
                return "[agent refused]"

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
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit("Set ANTHROPIC_API_KEY first (e.g. `export ANTHROPIC_API_KEY=sk-...`).")
    agent = Agent()
    print(agent.run("What is (17 * 23) + 5, and is that prime? Use the calc tool."))
