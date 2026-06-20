"""
Arena orchestrator — the loop that drives the agent (SETUP baseline).

A bounded finite-state loop: register once -> get_task -> Claude solves -> submit or skip.
This module OWNS the four arena lifecycle tools (register/get/submit/skip) and never exposes
them to the inner LLM, so the model can't "decide to submit" mid-reasoning — submission is a
deterministic code path with a double-submit guard.

SETUP scope: minimal but correct. The quality PASS-gate (eval/critic), category->tool routing,
the tokens-per-point ledger, and retry/backoff resilience are the BUILD phase — see
arena/MASTER-PLAN.md. The decision policy here is deliberately simple and clearly marked.

Run:  python -m arena.orchestrator   (from the repo root, venv active, .env filled)
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

import anthropic

from agent.agent import Agent
from agent.prompts import (
    VERIFY_SYSTEM,
    build_revision_prompt,
    build_task_prompt,
    build_verify_prompt,
    detect_task_type,
    parse_verdict,
    toolset_for_category,
)
from arena.config import Config, assert_valid, load_config
from arena.mcp_client import (
    ArenaAuthError,
    ArenaCallError,
    mcp_call,
    parse_agent_id,
    parse_level,
    parse_leveled_up,
    parse_score,
    parse_task,
)

MAX_TASKS = 10          # hard ceiling on tasks attempted per run
MAX_CONSEC_SKIPS = 3    # stop if we skip this many in a row (anti-starvation)

# Keep-alive on ALL_TASKS_ATTEMPTED (BUILD TARGET 2). The arena returns the plain-text
# sentinel "ALL_TASKS_ATTEMPTED: ... wait for level advancement" when the current level
# is drained; new levels unlock over time. Instead of stopping, we sleep and re-poll a
# BOUNDED number of times so the single live process keeps climbing as levels open.
MAX_KEEPALIVE_RETRIES = 6
KEEPALIVE_SLEEP_S = 15

# Verify gate (BUILD phase): an independent reviewer checks the draft (running code where
# possible) before the one-shot submit; on FAIL we revise up to N times, then submit
# best-effort (live runs showed wrong submits only score low — no ban — so a 40 beats a 0).
VERIFY_ENABLED = True
VERIFY_MAX_REVISIONS = 1


@dataclass
class RunState:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str = ""
    level: int = 1
    execution_id: str = ""
    consecutive_skips: int = 0
    tasks_done: int = 0
    submitted_task_ids: set[str] = field(default_factory=set)
    # True when the LAST get_tasks returned the ALL_TASKS_ATTEMPTED sentinel (level
    # drained, wait for advancement) rather than a genuine end-state / parse failure.
    # Drives keep-alive: only the sentinel triggers sleep+re-poll.
    last_get_tasks_sentinel: bool = False


# --------------------------------------------------------------------------- #
# Tracing (best-effort in setup; the hard "tracing emits" assert is build-phase).
# Replicates the reference bot's attributable-traces wiring.
# --------------------------------------------------------------------------- #
def init_tracing(cfg: Config) -> None:
    if not cfg.traceloop_api_key:
        print("[tracing] no TRACELOOP_API_KEY — skipping (build phase makes this required).")
        return
    try:
        from traceloop.sdk import Traceloop

        Traceloop.init(app_name="arena-claude-agent", api_key=cfg.traceloop_api_key,
                       disable_batch=True)
        print("[tracing] Traceloop initialized.")
    except Exception as exc:  # noqa: BLE001 — tracing is optional in setup
        print(f"[tracing] WARN: Traceloop.init failed ({exc}); continuing without tracing.")


def _associate(state: RunState, *, task_id: str = "") -> None:
    """Tie subsequent traces to this run/agent/task. Best-effort; never fatal in setup."""
    props = {"run.id": state.run_id, "agent.id": state.agent_id,
             "execution.id": state.execution_id}
    if task_id:
        props["task.id"] = task_id
    try:
        from traceloop.sdk import Traceloop

        Traceloop.set_association_properties(props)
    except Exception:  # noqa: BLE001 — optional in setup; build phase hardens this
        pass


# --------------------------------------------------------------------------- #
# Arena lifecycle steps (orchestrator-owned; not exposed to the inner LLM).
# --------------------------------------------------------------------------- #
async def register_once(cfg: Config, state: RunState) -> None:
    args = {
        "idToken": cfg.arena_id_token,
        "name": cfg.agent_name,
        "stack": cfg.agent_stack,
        "linkedinUrl": cfg.linkedin_url,
        "githubUrl": cfg.github_url,
    }
    text = await mcp_call("register_agent", args, endpoint=cfg.mcp_endpoint,
                          fallback_endpoint=cfg.mcp_endpoint_fallback)
    agent_id = parse_agent_id(text)
    if not agent_id:  # loud failure: the reference silently continued with no agent id
        raise ArenaCallError("register_agent", f"could not parse AGENT_ID from: {text!r}")
    state.agent_id = agent_id
    state.level = parse_level(text) or 1
    _associate(state)
    print(f"[register] agent_id={state.agent_id} level={state.level}")


def _is_all_tasks_attempted(text: str) -> bool:
    """True if a get_tasks response is the ALL_TASKS_ATTEMPTED level-drained sentinel.

    This is a deliberate, narrow match: only this sentinel means 'wait for a new level
    to unlock'. A genuine end-state or an unparseable reply must NOT trigger keep-alive
    (otherwise the run stalls 90s on a real stop), so we key keep-alive to this string.
    """
    return "all_tasks_attempted" in (text or "").lower()


async def fetch_task(cfg: Config, state: RunState) -> dict | None:
    text = await mcp_call(
        "get_tasks", {"idToken": cfg.arena_id_token, "agentId": state.agent_id},
        endpoint=cfg.mcp_endpoint, fallback_endpoint=cfg.mcp_endpoint_fallback,
    )
    task = parse_task(text)
    if task is None:
        state.last_get_tasks_sentinel = _is_all_tasks_attempted(text)
        kind = "ALL_TASKS_ATTEMPTED (level drained)" if state.last_get_tasks_sentinel \
            else "no parseable task"
        print(f"[get_tasks] {kind}. raw: {text!r}")
        return None
    state.last_get_tasks_sentinel = False
    _associate(state, task_id=str(task.get("id", "")))
    category = detect_task_type(str(task.get("title", "")), str(task.get("description", "")))
    print(f"[get_tasks] task={task.get('id')} L{task.get('level')} "
          f"'{task.get('title')}' [{category}]")
    return task


async def fetch_task_keepalive(cfg: Config, state: RunState) -> dict | None:
    """fetch_task, but on the ALL_TASKS_ATTEMPTED sentinel: sleep and re-poll a bounded
    number of times before giving up — so the single live process keeps climbing as new
    levels unlock. A genuine None (real end-state / parse failure) returns immediately."""
    for attempt in range(MAX_KEEPALIVE_RETRIES + 1):
        task = await fetch_task(cfg, state)
        if task is not None:
            return task
        if not state.last_get_tasks_sentinel:
            return None  # genuine end-state — don't wait
        if attempt < MAX_KEEPALIVE_RETRIES:
            print(f"[keepalive] level drained (attempt {attempt + 1}/{MAX_KEEPALIVE_RETRIES}); "
                  f"sleeping {KEEPALIVE_SLEEP_S}s for level unlock")
            await asyncio.sleep(KEEPALIVE_SLEEP_S)
    return None


def effort_for(task: dict, category: str) -> str:
    """Effort floor per task (BUILD TARGET 3). xhigh for code/logic and high arena
    levels (best for coding/agentic per the claude-api skill); high otherwise."""
    level = int(task.get("level", 1) or 1)
    if category in ("code_synthesis", "logic_reason") or level >= 5:
        return "xhigh"
    return "high"


def route(task: dict) -> tuple[str, list[dict], str]:
    """Single source of routing: category -> (server-tool set, effort). code/logic/data ->
    code_execution ALONE; context/grounded -> web tools ALONE; general -> none."""
    category = detect_task_type(str(task.get("title", "")), str(task.get("description", "")))
    return category, toolset_for_category(category), effort_for(task, category)


async def solve(task: dict, agent: Agent, server_tools: list[dict], effort: str) -> str:
    # Inner Claude loop is synchronous; fine for this sequential orchestrator.
    return agent.run(build_task_prompt(task), effort=effort, server_tools=server_tools)


async def review(task: dict, draft: str, critic: Agent,
                 server_tools: list[dict], effort: str) -> dict:
    """Independent reviewer: run/check the draft, return a verdict dict. Never raises."""
    try:
        verdict_text = critic.run(build_verify_prompt(task, draft),
                                  effort=effort, server_tools=server_tools)
    except Exception as exc:  # noqa: BLE001 — a critic failure must not block submit
        print(f"[verify] critic error: {exc} — defaulting to PASS")
        return {"pass": True, "score_estimate": None, "reason": str(exc), "fix_hint": ""}
    return parse_verdict(verdict_text)


async def revise(task: dict, draft: str, fix_hint: str, agent: Agent,
                 server_tools: list[dict], effort: str) -> str:
    """Re-solve once with the reviewer's fix. Empty string on error (-> keep the draft)."""
    try:
        return agent.run(build_revision_prompt(task, draft, fix_hint),
                         effort=effort, server_tools=server_tools)
    except Exception as exc:  # noqa: BLE001
        print(f"[verify] revision error: {exc} — keeping the original draft")
        return ""


async def submit(cfg: Config, state: RunState, task: dict, content: str) -> None:
    task_id = str(task["id"])
    if task_id in state.submitted_task_ids:  # deterministic double-submit guard
        print(f"[submit] task {task_id} already submitted — not resubmitting.")
        return
    execution_id = str(uuid.uuid4())
    state.execution_id = execution_id
    # Mark submitted BEFORE the network call: a dropped response can't trigger a second
    # submit of the one-shot task.
    state.submitted_task_ids.add(task_id)
    _associate(state, task_id=task_id)
    args = {
        "idToken": cfg.arena_id_token,
        "agentId": state.agent_id,
        "taskId": task_id,
        "executionId": execution_id,
        "content": content,
        "metadata": {
            "agent_name": cfg.agent_name,
            "agent_stack": cfg.agent_stack,
            "run_id": state.run_id,
            "model": "claude-opus-4-8",
        },
    }
    text = await mcp_call("submit_task", args, endpoint=cfg.mcp_endpoint,
                          fallback_endpoint=cfg.mcp_endpoint_fallback)
    score = parse_score(text)          # None == evaluation pending / unknown (never -1)
    leveled = parse_leveled_up(text)
    if leveled:
        state.level += 1
    state.consecutive_skips = 0
    score_str = f"{score}/100" if score is not None else "pending/unknown"
    print(f"[submit] task={task_id} score={score_str}{' LEVEL_UP' if leveled else ''}")


async def skip(cfg: Config, state: RunState, task: dict, reason: str = "setup-baseline skip") -> None:
    await mcp_call(
        "skip_task",
        {"idToken": cfg.arena_id_token, "agentId": state.agent_id,
         "taskId": str(task["id"]), "reason": reason},
        endpoint=cfg.mcp_endpoint, fallback_endpoint=cfg.mcp_endpoint_fallback,
    )
    state.consecutive_skips += 1
    print(f"[skip] task={task['id']} reason='{reason}' (consec={state.consecutive_skips})")


# --------------------------------------------------------------------------- #
# Helpers + entrypoint
# --------------------------------------------------------------------------- #
async def _solve_safe(task: dict, agent: Agent, server_tools: list[dict], effort: str) -> str:
    """Run the solver; a solver/Anthropic error becomes an empty answer (-> skip)."""
    try:
        return await solve(task, agent, server_tools, effort)
    except Exception as exc:  # noqa: BLE001 — one bad task must not kill the run
        print(f"[solve] error: {exc}")
        return ""


def _usable_answer(content: str) -> str | None:
    """None if the solver produced nothing submittable (refusal / non-convergence / empty)."""
    if not content:
        return None
    if content == "[agent refused]" or content.startswith("[hit MAX_STEPS"):
        return None
    return content


async def run(cfg: Config | None = None, agent: Agent | None = None) -> None:
    cfg = cfg or load_config()
    assert_valid(cfg)
    init_tracing(cfg)
    agent = agent or Agent(client=anthropic.Anthropic(api_key=cfg.anthropic_api_key))
    # Independent reviewer for the verify gate: shares the client, distinct system prompt,
    # fresh message list per call (so it re-examines rather than rubber-stamps).
    critic = Agent(client=agent.client, system=VERIFY_SYSTEM)
    state = RunState()

    try:
        await register_once(cfg, state)
        while state.tasks_done < MAX_TASKS and state.consecutive_skips < MAX_CONSEC_SKIPS:
            task = await fetch_task_keepalive(cfg, state)
            if task is None:
                print("[run] no task available (after keep-alive) — stopping.")
                break
            # Sticky get_tasks can return a task we already submitted -> skip it.
            if str(task["id"]) in state.submitted_task_ids:
                await skip(cfg, state, task, reason="already submitted (sticky)")
                continue
            category, server_tools, effort = route(task)
            final = _usable_answer(await _solve_safe(task, agent, server_tools, effort))
            if final is None:
                await skip(cfg, state, task, reason="no usable answer from solver")
            else:
                # Verify gate: an independent reviewer runs/checks the draft before the
                # one-shot submit; on FAIL, revise up to VERIFY_MAX_REVISIONS, then submit.
                revisions = 0
                while VERIFY_ENABLED:
                    verdict = await review(task, final, critic, server_tools, effort)
                    if verdict["pass"]:
                        print(f"[verify] PASS (est={verdict['score_estimate']})")
                        break
                    if revisions >= VERIFY_MAX_REVISIONS:
                        print(f"[verify] FAIL after {revisions} revision(s) "
                              f"(est={verdict['score_estimate']}) — submitting best-effort")
                        break
                    print(f"[verify] FAIL (est={verdict['score_estimate']}): "
                          f"{verdict['reason'][:100]} — revising")
                    revised = _usable_answer(
                        await revise(task, final, verdict["fix_hint"], agent,
                                     server_tools, effort))
                    if revised is not None:
                        final = revised
                    revisions += 1
                await submit(cfg, state, task, final)
            state.tasks_done += 1
    except ArenaAuthError as exc:
        raise SystemExit(f"[auth] {exc}\nPaste a fresh ARENA_ID_TOKEN in .env and re-run.")
    except ArenaCallError as exc:
        print(f"[run] arena call failed: {exc}\nStopping (build phase adds retry/backoff).")

    print(f"[run] done. tasks={state.tasks_done} level={state.level} "
          f"submitted={len(state.submitted_task_ids)} consec_skips={state.consecutive_skips}")


if __name__ == "__main__":
    asyncio.run(run())
