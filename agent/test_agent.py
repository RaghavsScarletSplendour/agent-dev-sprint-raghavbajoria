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


# A kwarg-capturing variant for asserting per-category tool routing / effort / max_tokens.
# Subclass (do NOT touch the shared FakeMessages — keep the 11 green).
class CapturingMessages(FakeMessages):
    def __init__(self, scripted: list[Resp]):
        super().__init__(scripted)
        self.calls: list[dict[str, Any]] = []  # one kwargs dict per create()

    def create(self, **kw: Any) -> Resp:
        self.calls.append(kw)
        return super().create(**kw)


class CapturingClient:
    def __init__(self, scripted: list[Resp]):
        self.messages = CapturingMessages(scripted)


def _tool_types(kw: dict[str, Any]) -> set:
    """Server-tool type identifiers declared in a captured create() call."""
    return {t.get("type") for t in kw["tools"]}


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


# --- 1b. Server-tool / pause_turn loop tests (the BUILD branches) -----------
def test_pause_turn_continues_then_finishes():
    """A pause_turn turn (server-tool loop paused) has NO client tool_use but is NOT
    final: the loop must append it, re-send, and keep going until end_turn. This is the
    regression anchor — it fails against the old 'no tool_use -> return' logic."""
    client = FakeClient([
        Resp([TextBlock("(searching...)")], stop_reason="pause_turn"),
        Resp([TextBlock("FINAL ANSWER")], stop_reason="end_turn"),
    ])
    out = A.Agent(client=client).run("grounded q")
    assert out == "FINAL ANSWER", out
    assert client.messages.i == 2, client.messages.i  # made the 2nd create(), didn't return early
    print("PASS: pause_turn continues, then returns the final answer")


def test_pause_turn_does_not_return_partial():
    """The intermediate pause_turn text must never leak as the answer."""
    client = FakeClient([
        Resp([TextBlock("(searching...)")], stop_reason="pause_turn"),
        Resp([TextBlock("FINAL ANSWER")], stop_reason="end_turn"),
    ])
    out = A.Agent(client=client).run("grounded q")
    assert out != "(searching...)", out
    print("PASS: pause_turn never leaks the intermediate text")


@dataclass
class CodeResultBlock:
    """Stand-in for a server-side bash_code_execution_tool_result block."""
    type: str = "bash_code_execution_tool_result"
    content: Any = None


def test_server_tool_result_turn_returns_text():
    """An end_turn whose content interleaves a server-tool result block + text has NO
    client tool_use — the loop must return the (last) text without expecting a
    client-side tool_result."""
    client = FakeClient([
        Resp([CodeResultBlock(), TextBlock("computed answer")], stop_reason="end_turn"),
    ])
    out = A.Agent(client=client).run("compute something")
    assert out == "computed answer", out
    assert client.messages.i == 1, client.messages.i
    print("PASS: server-tool result turn returns text, no client tool_result needed")


def test_code_category_declares_code_execution_alone():
    """code_synthesis routing declares code_execution and NEVER the dated web tools
    (documented misconfig: never co-declare them)."""
    from agent.prompts import toolset_for_category
    for category in ("code_synthesis", "logic_reason", "data_extraction"):
        client = CapturingClient([Resp([TextBlock("ok")], stop_reason="end_turn")])
        A.Agent(client=client).run("x", server_tools=toolset_for_category(category))
        types = _tool_types(client.messages.calls[0])
        assert types == {"code_execution_20260120"}, (category, types)
        assert "web_search_20260209" not in types and "web_fetch_20260209" not in types
    print("PASS: code/logic/data declare code_execution ALONE (no web tools)")


def test_context_category_declares_web_tools_alone():
    """context/grounded routing declares the web tools and NEVER code_execution."""
    from agent.prompts import toolset_for_category
    for category in ("context_awareness", "grounded"):
        client = CapturingClient([Resp([TextBlock("ok")], stop_reason="end_turn")])
        A.Agent(client=client).run("x", server_tools=toolset_for_category(category))
        types = _tool_types(client.messages.calls[0])
        assert types == {"web_search_20260209", "web_fetch_20260209"}, (category, types)
        assert "code_execution_20260120" not in types
    print("PASS: context/grounded declare web tools ALONE (no code_execution)")


def test_general_category_declares_no_server_tools():
    """general -> no server tools; baseline direct-answer path preserved."""
    from agent.prompts import toolset_for_category
    client = CapturingClient([Resp([TextBlock("ok")], stop_reason="end_turn")])
    A.Agent(client=client).run("x", server_tools=toolset_for_category("general"))
    assert client.messages.calls[0]["tools"] == [], client.messages.calls[0]["tools"]
    print("PASS: general declares no server tools")


def test_tool_using_turn_raises_max_tokens():
    """Tool-using turns raise max_tokens off the no-tool default; general stays at it."""
    from agent.prompts import toolset_for_category
    code = CapturingClient([Resp([TextBlock("ok")], stop_reason="end_turn")])
    A.Agent(client=code).run("x", server_tools=toolset_for_category("code_synthesis"))
    assert code.messages.calls[0]["max_tokens"] == A.MAX_TOKENS_TOOLS

    plain = CapturingClient([Resp([TextBlock("ok")], stop_reason="end_turn")])
    A.Agent(client=plain).run("x", server_tools=toolset_for_category("general"))
    assert plain.messages.calls[0]["max_tokens"] == A.MAX_TOKENS_DEFAULT
    print(f"PASS: tool turns use max_tokens={A.MAX_TOKENS_TOOLS}, no-tool stays "
          f"{A.MAX_TOKENS_DEFAULT}")


def test_effort_threaded_into_create():
    """The effort arg reaches output_config.effort on the create() call."""
    client = CapturingClient([Resp([TextBlock("ok")], stop_reason="end_turn")])
    A.Agent(client=client).run("x", effort="xhigh", server_tools=[])
    assert client.messages.calls[0]["output_config"] == {"effort": "xhigh"}
    print("PASS: effort is threaded into output_config")


def test_effort_for_routing():
    """effort_for: xhigh for code/logic and high levels; high otherwise."""
    assert O.effort_for({"level": 1}, "code_synthesis") == "xhigh"
    assert O.effort_for({"level": 1}, "logic_reason") == "xhigh"
    assert O.effort_for({"level": 6}, "general") == "xhigh"      # high level
    assert O.effort_for({"level": 2}, "general") == "high"
    assert O.effort_for({"level": 2}, "context_awareness") == "high"
    print("PASS: effort_for -> xhigh for code/logic/high-level, high otherwise")


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


_SENTINEL = "ALL_TASKS_ATTEMPTED: all current-level tasks done; wait for level advancement"


def _with_stubbed_sleep(arena: FakeArena, coro_factory):
    """Run with O.mcp_call mocked AND O.asyncio.sleep replaced by a counting no-op,
    so keep-alive's wait path is exercised instantly. Returns the sleep counter dict."""
    sleeps = {"n": 0}
    real_sleep = O.asyncio.sleep

    async def _noop(*_a, **_k):
        sleeps["n"] += 1

    O.asyncio.sleep = _noop
    O.mcp_call = arena
    try:
        asyncio.run(coro_factory())
    finally:
        O.asyncio.sleep = real_sleep
        O.mcp_call = M.mcp_call
    return sleeps


def test_keepalive_retries_then_stops():
    """Sentinel returned more times than the retry cap: poll initial + MAX_KEEPALIVE_RETRIES
    times, sleep MAX_KEEPALIVE_RETRIES times, never submit/skip, then exit cleanly."""
    arena = FakeArena(get_responses=[_SENTINEL] * (O.MAX_KEEPALIVE_RETRIES + 5))
    sleeps = _with_stubbed_sleep(
        arena, lambda: O.run(cfg=_fake_cfg(), agent=_fake_agent("ANS")))
    assert arena.count("get_tasks") == O.MAX_KEEPALIVE_RETRIES + 1, arena.count("get_tasks")
    assert sleeps["n"] == O.MAX_KEEPALIVE_RETRIES, sleeps["n"]
    assert arena.count("submit_task") == 0
    assert arena.count("skip_task") == 0
    print("PASS: keep-alive retries the sentinel a bounded number of times, then stops")


def test_keepalive_resumes_when_new_task_unlocks():
    """Sentinel twice, then a real task unlocks: keep-alive waits through both sentinels,
    resumes, and submits the new task."""
    new_task = '{"id":"t_new","title":"x","description":"y"}'
    arena = FakeArena(get_responses=[_SENTINEL, _SENTINEL, new_task])
    sleeps = _with_stubbed_sleep(
        arena, lambda: O.run(cfg=_fake_cfg(), agent=_fake_agent("ANS")))
    assert arena.count("submit_task") == 1, arena.count("submit_task")
    assert arena.args_for("submit_task")["taskId"] == "t_new"
    assert sleeps["n"] == 2, sleeps["n"]  # waited through exactly the two sentinels
    print("PASS: keep-alive resumes and submits when a new level unlocks")


def test_no_keepalive_on_genuine_none():
    """A non-sentinel un-parseable reply must break immediately — ZERO sleeps, ONE poll —
    so keep-alive doesn't stall mid-run on a real end-state."""
    arena = FakeArena(get_responses=["NO_TASKS"])
    sleeps = _with_stubbed_sleep(
        arena, lambda: O.run(cfg=_fake_cfg(), agent=_fake_agent("ANS")))
    assert sleeps["n"] == 0, sleeps["n"]
    assert arena.count("get_tasks") == 1, arena.count("get_tasks")
    print("PASS: genuine None breaks immediately (keep-alive is sentinel-only)")


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


# --- 2c. Thinking-strip + verify gate (newest BUILD branches) ---------------
@dataclass
class ThinkingBlock:
    thinking: str = "..."
    type: str = "thinking"


def test_strips_trailing_thinking_before_resend():
    """An assistant turn ending in a thinking block must be stripped before re-send
    (the live 400: 'final block ... cannot be `thinking`')."""
    client = CapturingClient([
        Resp([TextBlock("partial"), ThinkingBlock()], stop_reason="pause_turn"),
        Resp([TextBlock("FINAL")], stop_reason="end_turn"),
    ])
    out = A.Agent(client=client).run("x")
    assert out == "FINAL", out
    resent = client.messages.calls[1]["messages"]          # the 2nd create() re-sent msgs
    last_block = resent[-1]["content"][-1]                  # last block of the assistant turn
    assert getattr(last_block, "type", None) != "thinking", last_block
    print("PASS: trailing thinking stripped before re-send (no 400)")


def test_parse_verdict():
    from agent.prompts import parse_verdict
    v = parse_verdict('{"pass": false, "score_estimate": 50, "reason": "r", "fix_hint": "f"}')
    assert v["pass"] is False and v["score_estimate"] == 50 and v["fix_hint"] == "f"
    v2 = parse_verdict('verdict: {"pass": true, "score_estimate": 88} done')
    assert v2["pass"] is True and v2["score_estimate"] == 88
    assert parse_verdict("not json")["pass"] is True  # default non-blocking
    print("PASS: parse_verdict (json / embedded / default-pass on garbage)")


def _fake_agent_seq(texts: list[str]) -> "A.Agent":
    return A.Agent(client=FakeClient(
        [Resp([TextBlock(t)], stop_reason="end_turn") for t in texts]))


def test_verify_pass_submits():
    arena = FakeArena(get_responses=['{"id":"t1","title":"x","description":"y"}'])
    agent = _fake_agent_seq(["GOOD DRAFT", '{"pass": true, "score_estimate": 90}'])
    _with_mocked_arena(arena, lambda: O.run(cfg=_fake_cfg(), agent=agent))
    assert arena.count("submit_task") == 1
    assert arena.args_for("submit_task")["content"] == "GOOD DRAFT"
    print("PASS: verify PASS -> submits the draft, no revision")


def test_verify_fail_triggers_revision():
    arena = FakeArena(get_responses=['{"id":"t1","title":"x","description":"y"}'])
    agent = _fake_agent_seq([
        "DRAFT v1",
        '{"pass": false, "score_estimate": 45, "fix_hint": "handle empty input", "reason": "incomplete"}',
        "DRAFT v2 fixed",
    ])
    _with_mocked_arena(arena, lambda: O.run(cfg=_fake_cfg(), agent=agent))
    assert arena.count("submit_task") == 1
    assert arena.args_for("submit_task")["content"] == "DRAFT v2 fixed"
    print("PASS: verify FAIL -> revises, submits the corrected answer")


def test_verify_unparseable_defaults_pass():
    arena = FakeArena(get_responses=['{"id":"t1","title":"x","description":"y"}'])
    agent = _fake_agent_seq(["DRAFT", "sorry, no json here"])
    _with_mocked_arena(arena, lambda: O.run(cfg=_fake_cfg(), agent=agent))
    assert arena.count("submit_task") == 1
    assert arena.args_for("submit_task")["content"] == "DRAFT"
    print("PASS: unparseable verdict -> default pass, submits draft (non-blocking)")


if __name__ == "__main__":
    test_converges_to_final_answer()
    test_max_steps_cap()
    test_repeat_action_guard()
    test_pause_turn_continues_then_finishes()
    test_pause_turn_does_not_return_partial()
    test_server_tool_result_turn_returns_text()
    test_code_category_declares_code_execution_alone()
    test_context_category_declares_web_tools_alone()
    test_general_category_declares_no_server_tools()
    test_tool_using_turn_raises_max_tokens()
    test_effort_threaded_into_create()
    test_effort_for_routing()
    test_parsers()
    test_flow_register_get_submit()
    test_double_submit_blocked()
    test_skip_on_empty_answer()
    test_sticky_already_submitted_skip()
    test_malformed_get_tasks_breaks_clean()
    test_keepalive_retries_then_stops()
    test_keepalive_resumes_when_new_task_unlocks()
    test_no_keepalive_on_genuine_none()
    test_max_tasks_bound()
    test_auth_error_clean_exit()
    test_strips_trailing_thinking_before_resend()
    test_parse_verdict()
    test_verify_pass_submits()
    test_verify_fail_triggers_revision()
    test_verify_unparseable_defaults_pass()
    print("\nAll offline tests passed — agent guards + arena flow are verified.")
