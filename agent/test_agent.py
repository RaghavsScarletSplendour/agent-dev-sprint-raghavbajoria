"""
Offline tests for the agent core — no API key, no network.

Injects a fake Claude client that scripts responses, so we can PROVE the loop
guards fire correctly before the arena. A reliable loop is what wins; an
untested guard is a guard you don't have.

Run:  python test_agent.py   (or: pytest test_agent.py)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import agent as A


# --- minimal stand-ins for the Anthropic response shape ---------------------
@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    name: str
    input: dict[str, Any]
    id: str = "tu_1"
    type: str = "tool_use"


@dataclass
class Resp:
    content: list[Any]
    stop_reason: str = "tool_use"


class FakeMessages:
    def __init__(self, scripted: list[Resp]):
        self._scripted = scripted
        self.i = 0

    def create(self, **_: Any) -> Resp:
        r = self._scripted[min(self.i, len(self._scripted) - 1)]
        self.i += 1
        return r


class FakeClient:
    def __init__(self, scripted: list[Resp]):
        self.messages = FakeMessages(scripted)


# --- tests ------------------------------------------------------------------
def test_converges_to_final_answer():
    """Tool call -> result fed back -> model answers. Happy path."""
    client = FakeClient([
        Resp([ToolUseBlock("calc", {"expression": "6*7"})], stop_reason="tool_use"),
        Resp([TextBlock("The answer is 42.")], stop_reason="end_turn"),
    ])
    out = A.Agent(client=client).run("What is 6*7?")
    assert out == "The answer is 42.", out
    print("PASS: converges to final answer")


def test_max_steps_cap():
    """Model that never stops calling tools must hit the hard ceiling, not hang."""
    # Distinct args each step so the repeat-guard doesn't fire first.
    scripted = [Resp([ToolUseBlock("calc", {"expression": f"{n}+1"})]) for n in range(50)]
    out = A.Agent(client=FakeClient(scripted)).run("loop forever")
    assert "MAX_STEPS" in out, out
    print("PASS: MAX_STEPS cap stops a runaway loop")


def test_repeat_action_guard(monkeypatch=None):
    """Identical (tool,args) every step -> the tool runs at most MAX_REPEAT_ACTIONS
    times, then the guard substitutes a STOP message instead of re-running it."""
    runs = {"n": 0}
    real_run = A.tools.run

    def counting_run(name: str, args: dict[str, Any]) -> str:
        runs["n"] += 1
        return real_run(name, args)

    A.tools.run = counting_run
    try:
        same = Resp([ToolUseBlock("calc", {"expression": "1+1"})])
        out = A.Agent(client=FakeClient([same])).run("repeat")
    finally:
        A.tools.run = real_run

    # Guard math: with MAX_REPEAT_ACTIONS=2, the tool runs on steps 1 and 2,
    # then STOP from step 3 onward — so it executes exactly twice no matter
    # how many steps the loop takes.
    assert runs["n"] == A.MAX_REPEAT_ACTIONS, f"tool ran {runs['n']} times"
    assert "MAX_STEPS" in out, out  # model never stops, so loop ends at the cap
    print(f"PASS: repeat-action guard caps tool execution at {A.MAX_REPEAT_ACTIONS}")


if __name__ == "__main__":
    test_converges_to_final_answer()
    test_max_steps_cap()
    test_repeat_action_guard()
    print("\nAll guard tests passed — the loop is arena-safe.")
