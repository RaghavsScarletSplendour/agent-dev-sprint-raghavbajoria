"""
Offline tests — no API key, no network, no live arena.

Two layers:
  1. Agent loop guards (FakeClient scripts the model) — proves the loop can't hang.
  2. Arena layer — pure parsers (never a wrong default) + orchestrator flow with a mocked
     mcp_call — proves register->get->submit/skip is correct and bounded.

Run:  python agent/test_agent.py    (or: python -m pytest agent/test_agent.py -q)
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Make the repo root importable so `agent.*` and `arena.*` packages resolve regardless of
# how this file is invoked (`python agent/test_agent.py` or `python -m pytest`).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import agent as A
from arena import mcp_client as M
from arena import orchestrator as O
from arena.config import Config
from arena.mcp_client import ArenaAuthError


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
    usage: Any = None


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


# --- 1. Agent loop guard tests ----------------------------------------------
def test_converges_to_final_answer():
    """Tool call -> result fed back -> model answers. Happy path."""
    client = FakeClient([
        Resp([ToolUseBlock("noop", {"x": 1})], stop_reason="tool_use"),
        Resp([TextBlock("The answer is 42.")], stop_reason="end_turn"),
    ])
    out = A.Agent(client=client).run("What is 6*7?")
    assert out == "The answer is 42.", out
    print("PASS: converges to final answer")


def test_max_steps_cap():
    """Model that never stops calling tools must hit the hard ceiling, not hang."""
    scripted = [Resp([ToolUseBlock("noop", {"x": n})]) for n in range(50)]
    out = A.Agent(client=FakeClient(scripted)).run("loop forever")
    assert "MAX_STEPS" in out, out
    print("PASS: MAX_STEPS cap stops a runaway loop")


def test_repeat_action_guard():
    """Identical (tool,args) every step -> the tool runs at most MAX_REPEAT_ACTIONS times,
    then the guard substitutes a STOP message instead of re-running it."""
    runs = {"n": 0}
    real_run = A.tools.run

    def counting_run(name: str, args: dict[str, Any]) -> str:
        runs["n"] += 1
        return real_run(name, args)

    A.tools.run = counting_run
    try:
        same = Resp([ToolUseBlock("noop", {"x": 1})])
        out = A.Agent(client=FakeClient([same])).run("repeat")
    finally:
        A.tools.run = real_run

    assert runs["n"] == A.MAX_REPEAT_ACTIONS, f"tool ran {runs['n']} times"
    assert "MAX_STEPS" in out, out
    print(f"PASS: repeat-action guard caps tool execution at {A.MAX_REPEAT_ACTIONS}")


# --- 2a. Pure parser tests (no network, no mocking) -------------------------
def test_parsers():
    # parse_score: None on no-match (NOT -1, NOT 0) — the reference's silent-default bug.
    assert M.parse_score("Evaluation pending. No score yet.") is None
    assert M.parse_score("Score: 88/100") == 88
    assert M.parse_score("Score:  72 / 100  LEVEL_UP") == 72
    # parse_agent_id: tolerant; None when absent.
    assert M.parse_agent_id("Registered. AGENT_ID: agnt_01. Level: 1") == "agnt_01"
    assert M.parse_agent_id("Agent Id: agnt_77") == "agnt_77"
    assert M.parse_agent_id("welcome, no id present") is None
    # level + level_up
    assert M.parse_level("Level: 3") == 3
    assert M.parse_level("no level") is None
    assert M.parse_leveled_up("Score: 91/100 LEVEL_UP") is True
    assert M.parse_leveled_up("Score: 40/100") is False
    # parse_task: dict-with-id, list-of-dict, None on malformed/empty.
    assert M.parse_task('{"id": "t1", "title": "x"}')["id"] == "t1"
    assert M.parse_task('[{"id": "t2", "title": "y"}]')["id"] == "t2"
    assert M.parse_task("not json at all") is None
    assert M.parse_task("[]") is None
    assert M.parse_task('{"no_id": true}') is None
    # Real arena JSON shapes (the live register_agent response is JSON, not "AGENT_ID:" text).
    reg = '{"status":"REGISTERED","agentId":"aTx5tsOVq3MgGZrljFfo","level":2,"message":"ok"}'
    assert M.parse_agent_id(reg) == "aTx5tsOVq3MgGZrljFfo"
    assert M.parse_level(reg) == 2
    assert M.parse_agent_id('{"id":"task1"}') is None  # a task id must NOT be read as agent id
    assert M.parse_score('{"score":88,"leveledUp":true}') == 88
    assert M.parse_leveled_up('{"score":88,"leveledUp":true}') is True
    assert M.parse_leveled_up('{"score":50,"leveledUp":false}') is False
    assert M.parse_task('{"task":{"id":"t5","title":"x"}}')["id"] == "t5"
    print("PASS: parsers (JSON + text shapes; score None-not-(-1); agent-id vs task-id)")


# --- 2b. Orchestrator flow tests (mocked mcp_call) --------------------------
class FakeArena:
    """Async stand-in for mcp_call. Scripts get_tasks responses; records every call."""

    def __init__(self, get_responses,
                 register_text='{"status":"REGISTERED","agentId":"agnt_1","level":1,"message":"ok"}',
                 submit_text='{"score":88,"leveledUp":true,"message":"LEVEL_UP"}', auth_fail_on=None):
        self.get_responses = list(get_responses)
        self.register_text = register_text
        self.submit_text = submit_text
        self.auth_fail_on = auth_fail_on
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, tool, args, *, endpoint, fallback_endpoint=None):
        self.calls.append((tool, dict(args)))
        if self.auth_fail_on == tool:
            raise ArenaAuthError(f"{tool}: simulated 401 unauthorized")
        if tool == "register_agent":
            return self.register_text
        if tool == "get_tasks":
            return self.get_responses.pop(0) if self.get_responses else "NO_TASKS"
        if tool == "submit_task":
            return self.submit_text
        if tool == "skip_task":
            return "skipped"
        return ""

    def count(self, tool: str) -> int:
        return sum(1 for t, _ in self.calls if t == tool)

    def args_for(self, tool: str) -> dict:
        return next(a for t, a in self.calls if t == tool)


def _fake_cfg(**overrides) -> Config:
    base = dict(
        anthropic_api_key="sk-ant-test123", arena_id_token="jwt-token",
        arena_platform_uid="uid", traceloop_api_key="", mcp_endpoint="http://test/mcp",
        mcp_endpoint_fallback="http://test-fallback/mcp", agent_name="test-agent",
        agent_stack="test-stack", github_url="https://github.com/test",
        linkedin_url="https://linkedin.com/in/test",
    )
    base.update(overrides)
    return Config(**base)


def _fake_agent(answer: str) -> "A.Agent":
    return A.Agent(client=FakeClient([Resp([TextBlock(answer)], stop_reason="end_turn")]))


def _with_mocked_arena(arena: FakeArena, coro_factory):
    """Run a coroutine with O.mcp_call swapped for `arena`, always restoring it."""
    O.mcp_call = arena
    try:
        return asyncio.run(coro_factory())
    finally:
        O.mcp_call = M.mcp_call


def test_flow_register_get_submit():
    task = '{"id": "t1", "title": "Add", "description": "1+1", "level": 1, "points": 10}'
    arena = FakeArena(get_responses=[task])
    _with_mocked_arena(arena, lambda: O.run(cfg=_fake_cfg(), agent=_fake_agent("THE ANSWER")))
    assert arena.count("register_agent") == 1
    assert arena.count("submit_task") == 1
    sub = arena.args_for("submit_task")
    assert sub["content"] == "THE ANSWER"
    assert sub["taskId"] == "t1"
    assert uuid.UUID(sub["executionId"]).version == 4  # fresh UUIDv4
    assert sub["metadata"]["model"] == "claude-opus-4-8"
    # register_agent carried both mandatory URLs
    reg = arena.args_for("register_agent")
    assert reg["githubUrl"] and reg["linkedinUrl"]
    print("PASS: register->get->submit happy path (uuid4 executionId + content + URLs)")


def test_double_submit_blocked():
    arena = FakeArena(get_responses=[])
    state = O.RunState(agent_id="agnt_1")
    task = {"id": "t9"}

    async def _two_submits():
        await O.submit(_fake_cfg(), state, task, "ans")
        await O.submit(_fake_cfg(), state, task, "ans")  # second must be a no-op

    _with_mocked_arena(arena, _two_submits)
    assert arena.count("submit_task") == 1, arena.count("submit_task")
    assert "t9" in state.submitted_task_ids
    print("PASS: double-submit blocked")


def test_skip_on_empty_answer():
    task = '{"id": "t1", "title": "x", "description": "y"}'
    arena = FakeArena(get_responses=[task])
    _with_mocked_arena(arena, lambda: O.run(cfg=_fake_cfg(), agent=_fake_agent("")))
    assert arena.count("skip_task") == 1
    assert arena.count("submit_task") == 0
    print("PASS: empty answer -> skip, never submits garbage")


def test_sticky_already_submitted_skip():
    same = '{"id": "t1", "title": "x", "description": "y"}'
    arena = FakeArena(get_responses=[same, same])  # sticky returns the same task twice
    _with_mocked_arena(arena, lambda: O.run(cfg=_fake_cfg(), agent=_fake_agent("ANS")))
    assert arena.count("submit_task") == 1
    assert arena.count("skip_task") == 1
    print("PASS: sticky already-submitted -> skip (no resubmit)")


def test_malformed_get_tasks_breaks_clean():
    arena = FakeArena(get_responses=["{ this is not valid json"])
    _with_mocked_arena(arena, lambda: O.run(cfg=_fake_cfg(), agent=_fake_agent("ANS")))
    assert arena.count("submit_task") == 0
    assert arena.count("skip_task") == 0
    print("PASS: malformed get_tasks -> clean break, no submit")


def test_max_tasks_bound():
    """get_tasks returns a fresh distinct task forever -> loop must stop at MAX_TASKS."""

    class InfiniteArena(FakeArena):
        def __init__(self):
            super().__init__(get_responses=[])
            self.n = 0

        async def __call__(self, tool, args, *, endpoint, fallback_endpoint=None):
            if tool == "get_tasks":
                self.n += 1
                self.calls.append((tool, dict(args)))
                return json.dumps({"id": f"t{self.n}", "title": "x", "description": "y"})
            return await super().__call__(tool, args, endpoint=endpoint,
                                          fallback_endpoint=fallback_endpoint)

    arena = InfiniteArena()
    _with_mocked_arena(arena, lambda: O.run(cfg=_fake_cfg(), agent=_fake_agent("ANS")))
    assert arena.count("submit_task") == O.MAX_TASKS, arena.count("submit_task")
    print(f"PASS: run bounded at MAX_TASKS={O.MAX_TASKS}")


def test_auth_error_clean_exit():
    arena = FakeArena(get_responses=[], auth_fail_on="register_agent")
    raised = False
    try:
        _with_mocked_arena(arena, lambda: O.run(cfg=_fake_cfg(), agent=_fake_agent("ANS")))
    except SystemExit:
        raised = True
    assert raised, "expected SystemExit on auth failure"
    print("PASS: auth error -> clean SystemExit (no infinite retry)")


if __name__ == "__main__":
    test_converges_to_final_answer()
    test_max_steps_cap()
    test_repeat_action_guard()
    test_parsers()
    test_flow_register_get_submit()
    test_double_submit_blocked()
    test_skip_on_empty_answer()
    test_sticky_already_submitted_skip()
    test_malformed_get_tasks_breaks_clean()
    test_max_tasks_bound()
    test_auth_error_clean_exit()
    print("\nAll offline tests passed — agent guards + arena flow are verified.")
