# Agent Arena — Master Plan (how we win)

> The single source of truth for our arena agent: what the arena actually is, the
> winning thesis, the architecture, the build order, and the on-site probe.
> Supersedes the old `arena/STRATEGY.md` (which assumed a real-time game — wrong model).
> Built from: `arena/MCP-DOCS.md` + `arena/TUTORIAL.md` (captured docs), the Lap 1
> talk notes, and a 9-agent strategy workshop (4 expert lenses → adversarial critique → synthesis).
> Status: **plan only — agent not yet rebuilt.** Execute on the user's go-ahead.

---

## 1. Shared understanding — what the arena actually is

- **It's a task-solving MCP benchmark, NOT a real-time PvP game.** Our agent is an **MCP
  client that connects OUT** to the arena's MCP server. We register once, pull tasks one at a
  time (sticky `get_tasks`), solve them, and submit answers an AI evaluator scores **0–100**.
  No ticks, no visible opponents, no board, no inbound endpoint we host.
- **The leaderboard ("Sigma Leaderboard") ranks on TWO axes:** per-task **score** (0–100;
  **≥70 levels up**) **AND "Neural Efficiency"** = high score at **low token cost**. So:
  **score-first, then efficiency.** A 65 doesn't level up; an 88 outranks a terse 68. Pure
  token-minimization loses; rigor-everywhere loses the efficiency half.
- **Mandatory tool use is real and in tension with token-saving.** A correct answer that
  didn't demonstrably use the right tool is penalized as a **"lucky guess"** (Traceloop/OTel
  observes tool calls). ⚠️ **UNVERIFIED:** whether the arena's tracer sees our *in-process*
  Claude tool calls or only the 4 arena MCP calls. **Probe this before relying on local tools.**
- **`submit_task` is ONE-SHOT per task** — a wrong/low submit is unrecoverable. **`skip_task`
  is documented penalty-free** and unlocks a fresh task (sticky `get_tasks` otherwise returns
  the same task). **Stage 2 reportedly BANS** agents that fail to show functional correctness,
  and a ban ends the run. ⚠️ Exact ban threshold, whether skip is truly free, and how the
  realized score comes back (`"Evaluation pending"` is async) are all **UNCONFIRMED** → probe.
- **Three keys from web sign-in:** Platform UID; the **ephemeral JWT** (`idToken` on every
  call, **~1h expiry** → manual re-paste mid-event); the **Traceloop API key** (likely how the
  arena credits tool use). JWT expiry + tracing are *code-level preconditions*, not reasoning.
- **API facts (claude-api skill):** `claude-opus-4-8` uses `thinking={"type":"adaptive"}`;
  effort lives in `output_config.effort` (`low|medium|high|xhigh|max`; **xhigh** = agentic
  sweet spot); structured outputs via `output_config.format` json_schema (use for the critic);
  **prompt-cache minimum prefix is 4096 tokens** (our current ~80-token system silently won't
  cache); **never co-declare a standalone code_execution tool with the `_20260209` web tools.**

## 2. Win thesis

**Win the Sigma Leaderboard with a SCORE-FIRST, EFFICIENCY-SECOND agent:** a hardcoded
per-task state machine — `get_tasks → classify → route the one right tool → solve → cheap
deterministic self-check → submit-or-skip` — that **defaults to a single tool-grounded
high-effort pass**, escalates to a fresh-context critic **only when cheap signals say it's
risky** (Stage 2, low confidence, high points), and is **tuned the moment we have real data**
from a 15-minute on-site probe of the four load-bearing unknowns.

## 3. Scoring strategy (resolving the tool-vs-tokens tension)

- **Score-first, efficiency-second.** Target a high bar (~85+; the tutorial notes "85+ needs
  helper tools") on every task, then minimize tokens **only among runs that already clear it.**
  Efficiency is a tie-breaker — you can't tie-break past someone who out-scored *and* out-leveled you.
- **Exactly ONE real tool round-trip per task whose result feeds the answer** (separate
  thinking from acting: write+run a program, read its output, answer FROM that output). Never
  fire a tool whose result we ignore (*that* is the lucky-guess shape); never skip the tool to
  save tokens. Budget one round-trip — not zero, not an exploratory loop.
- **Default path** = single tool-grounded pass at `effort=high` + one **cheap deterministic
  self-check** (re-run the code / recompute the math). **Escalate** to the separate
  fresh-context critic + executable verification **only** when (a) in ban-prone Stage 2, (b)
  solver self-confidence is low, or (c) the task is high-points.
- **Effort by phase AND criticality, never flat:** Cold Boot / Stage 2 **floor at `high`** (a
  ban ends the run — don't default L1 to `low`); Evolutionary Cycle `high`, drop to `medium`
  only where the ledger proves a category is easy; APEX `high`/`xhigh`; reserve `max` for the
  hardest latency-insensitive tasks. **The critic always runs at ≥ the solver's effort.**
- **Submit only on a recorded PASS;** else skip (penalty-free, once confirmed) rather than burn
  the one-shot on a wrong answer that risks the Stage-2 ban. **BUT if the probe shows wrong
  submits only score low (no ban), flip to submit-best-effort** (a 40 beats a 0) and reserve
  skip for genuinely unwinnable/too-expensive tasks.
- **Submit content = final-answer-only** (CODE: verified program + run output; others: grounded
  answer + citations), with an explicit "no exploratory reasoning in the response" instruction
  so lower-effort Opus doesn't leak chain-of-thought into the graded content.
- **Measure cost-per-point on the first 3–5 real tasks**, re-tune the effort policy + PASS
  threshold at the Pit Stop from realized scores — the gate is a blind controller until the
  score channel and the ledger both exist.

## 4. Architecture — two-layer loop

Both layers built on the existing manual ReAct core in `agent/agent.py` (manual control = every
guardrail enforceable in code).

**OUTER — `arena/orchestrator.py` (new): a finite state machine that OWNS the 4 arena lifecycle
tools and never exposes them to the inner LLM.**
```
BOOT      register_agent once (idempotent; mandatory linkedin+github URLs);
          assert non-empty AGENT_ID; assert Traceloop.init succeeded
  ↓
GET_TASK  get_tasks (sticky)
  ↓
CLASSIFY  code-level keyword classifier on title+description (PRIMARY router;
          a live 'category' field is only an optimization if get_tasks shows one)
  ↓
ROUTE     category → { required_tool, effort, acceptance_checklist }
  ↓
SOLVE     inner Agent.run with the category's point-getter tool + code-selected effort
  ↓
SELF-CHECK  cheap deterministic verify first (re-run code / recompute in the same
            code_execution container); escalate to a SEPARATE fresh-context critic
            (json_schema → {pass, reason, confidence}) only on FAIL or high-risk
  ↓
DECIDE    PASS → submit_task (fresh uuid4 executionId, generated+persisted BEFORE the call)
          FAIL after MAX_REVISIONS=2 (diversified re-drafts) | unsolvable | cost ceiling → skip_task
```

**INNER — the existing `Agent.run`**, with the placeholder `calc` replaced by category tools
(server-side `code_execution_20260120` for CODE/LOGIC/DATA; `web_search_20260209` +
`web_fetch_20260209` **alone** for CONTEXT; client-side safe-AST calc / regex-json parser as a
trivial-task fast path to avoid Wi-Fi round-trips and Stage-2 timeouts).

**Transport — `arena/mcp_client.py` `mcp_call(tool, args)`:** fresh FastMCP
`StreamableHttpTransport` connection **per call**; endpoint `.dev` → Cloud Run fallback; on
**401 → HALT + prompt for fresh JWT** (watched file) then retry once; `executionId`-then-without
fallback (tutorial discrepancy); top-level try/except degrades to **skip-and-continue** so the
6-hour run never dies.

**Guards:** keep `MAX_STEPS=12`, `MAX_REPEAT_ACTIONS=2`; add `MAX_REVISIONS=2`, a per-level
consecutive-skip cap, a **submitted-taskId set** (submit reachable ONLY via the PASS branch),
and a **tokens-per-point ledger** from `resp.usage` on every call.

## 5. Toolset

| Tool | Serves | Notes |
|---|---|---|
| **`code_execution_20260120`** (server-side, REPL + container reuse) | CODE SYNTHESIS (write+run+verify), LOGIC & REASON (solve/brute-check math), DATA EXTRACTION (regex/json/csv parse) | Verify in the **same container** the solver produced artifacts in. |
| **`web_search_20260209` + `web_fetch_20260209`** (server-side, declared **alone**) | CONTEXT AWARENESS | Retrieve then ground in fetched text with citations (retrieval > bigger context). |
| **Client-side safe-AST calc + regex/json parser** (in `agent.py` Registry, errors→tool_results) | trivial LOGIC/DATA fast path | Avoids a network round-trip + Stage-2 timeout. |
| **Fresh-context critic** (separate `messages.create`, clean message list, json_schema → `{pass, reason, confidence}`, run at ≥ solver effort) | reflection gate for risky/high-points tasks (LOGIC/CONTEXT, no executable oracle) | Independently validate the oracle — generate test cases with a *different* prompt than wrote the solution. |
| **`arena/mcp_client.py`** | the 4 arena lifecycle tools | orchestrator-only, never inner-LLM tools. |
| **`Traceloop.init`** (traceloop-sdk / opentelemetry-sdk) | tracing surface that credits tool use | wired at startup; **verify it actually emits during the probe.** |

## 6. Guardrails (hardcoded at the code level — not prompt prose)

- **`submit_task` reachable ONLY through the gate's PASS branch** — enforced in orchestrator
  code, asserted, and covered by a test. The 4 arena tools are NOT exposed to the inner LLM, so
  a model can't "decide to submit" mid-reasoning.
- **Per-taskId `submitted` set blocks double-submit;** `uuid4` executionId generated + persisted
  **before** the network call so a dropped response dedupes server-side instead of burning the one-shot.
- **Category → required_tool map with a hard rule:** refuse to submit unless the category's
  required tool was demonstrably called this task (defeats the lucky-guess penalty deterministically).
- `MAX_STEPS=12`, `MAX_REPEAT_ACTIONS=2` (existing) **+** `MAX_REVISIONS=2`, per-level
  consecutive-skip cap, per-task token ceiling that forces a submit-or-skip, and **seen-task
  memory** to prevent skip-loop starvation on a small hard-task pool.
- **Startup asserts:** non-empty AGENT_ID; Traceloop up before any submit; the installed
  anthropic SDK accepts one real `effort`+`adaptive`+`json_schema` call. Refuse to run if any
  fails — surface the exact error loudly for a mentor.
- **Transport resilience** (see §4) — fresh connection per call, endpoint fallback,
  401→refresh, executionId fallback, skip-and-continue.
- **Sandboxed/server-side code execution only**, errors → tool_results (existing Registry
  pattern), so arbitrary generated code can't hang/crash the loop.
- **NEVER co-declare** a standalone `code_execution` tool alongside the `_20260209` web tools.

## 7. Memory & feedback

- **`arena/run_log.jsonl`** appended after every submit: `{ts, taskId, category, level, effort,
  tools_used, self_score, realized_score, input_tokens, output_tokens, cache_read_tokens,
  tokens_per_point, leveled_up}` — memory + eval + feedback + crash-recovery in one file.
- **Reflection from day 1:** the self-eval gate runs before EVERY submit (cheap deterministic
  by default, escalating to the fresh-context critic for risky tasks). Built in before going live.
- **Tokens-per-point ledger keyed by category+level** drives the effort policy: at the Pit
  Stop, lower effort only where realized scores prove a category reliably clears the bar — empirical.
- **The on-site probe is the primary feedback** for the load-bearing unknowns; encode findings
  as constants before enabling the gate.
- **JWT `exp` decoded** as a proactive refresh signal (~5min early) + react to 401s. Load
  `run_log.jsonl` on startup to resume level/policy after a restart — *only if* registration is
  confirmed idempotent across restart.

## 8. Build plan (phased)

**Phase 0 — Fix the build so nothing 400s at Cold Boot.** Bump `requirements.txt`
(`anthropic>=0.69`; add fastmcp, httpx, traceloop-sdk, opentelemetry-sdk; drop fastapi/uvicorn
from the critical path). Pre-install on the venue laptop + run ONE real smoke call
(`messages.create` with adaptive thinking + `output_config.effort` + json_schema format) to
prove the SDK is new enough. Confirm all keys are in env, asserted non-empty at startup.

> **API key config note:** The config file reads all secrets (Anthropic API key, Traceloop key,
> arena JWT) from a `.env` file via `python-dotenv`. The `.env` file is listed in `.gitignore`
> and will never be committed — copy `.env.example` on a fresh clone and fill in the real values.

**Phase 1 — Local loop first (no network).** Rewrite `agent/agent.py` `SYSTEM_PROMPT` for the
real mechanic (solve one task → self-verify → final-answer-only; optimize score AND cost); delete
the game framing. Replace `calc` with the category tools (code_execution for CODE/LOGIC/DATA;
web tools **alone** for CONTEXT; keep the safe-AST/regex fast path). Handle server-tool result
blocks + `stop_reason=='pause_turn'` (don't return early when there's no client `tool_use`
block). Raise `max_tokens` off 4096 (tie to effort; stream large outputs). Make `system` a
`cache_control` block AND grow the frozen prefix past 4096 tokens (routing rubric + tool specs +
exemplars); keep idToken/UUID/task JSON out of the prefix. Capture `resp.usage` into the ledger.
Add the deterministic self-check + the fresh-context critic. Extend `test_agent.py`
(PASS→submit; FAIL×(MAX_REVISIONS+1)→skip & submit never fires; missing-tool→skip; double-submit
guard; classifier routing) with realistic server-tool fixtures.

**Phase 2 — Connect to the arena (Cold Boot).** Build `arena/mcp_client.py` (resilient
`mcp_call`) and `arena/orchestrator.py` (state machine reading keys from env; `register_agent`
once with mandatory LinkedIn+GitHub URLs; assert AGENT_ID). Wire `Traceloop.init` and assert
tracing before any submit. Capture the **first real `get_tasks` and `submit_task` response
shapes**; hardcode the live endpoint + the working executionId variant.

**Phase 3 — On-site PROBE (first ~15 min, gate OFF) — highest leverage.** Empirically resolve
the four load-bearing unknowns (see §9), encode as constants, THEN enable the gate tuned to reality.

**Phase 4 — Harden the 6-hour run.** JWT guard (decode `exp`, refresh ~5min early + on 401,
loud "paste fresh idToken" prompt reading a watched file). Idempotent submit (persist
executionId before the call; re-query sticky state on a dropped response; mark submitted only on
ack). Anti-stuck (per-level skip cap + seen-task memory + per-task token ceiling). Persist
`run_log.jsonl` after every submit; confirm registration idempotency-across-restart before relying on resume.

**Phase 5 — Optimize for Neural Efficiency (Pit Stop onward).** From the ledger, lower effort
one tier only where a category reliably clears the high bar (never below the score floor).
Difficulty-aware routing (trivial L1 → fast path + inline check; APEX → full critic pipeline).
Verify cache hits (`usage.cache_read_input_tokens > 0` on turn 2+). Add a format-compliance
check once the expected output format is known.

## 9. On-site PROBE — the four load-bearing unknowns (do FIRST, gate OFF)

1. **BAN behavior:** submit ONE deliberately-wrong cheap answer → ban, or just low score? Decides submit-vs-skip aggression.
2. **LUCKY-GUESS / Traceloop reach:** submit one correct-but-untooled and one tool-backed answer; compare scores. Does an *in-process* tool call get credited to this taskId, or does the tracer only see the 4 arena MCP calls? If the latter, route required-tool evidence through `submit_task` metadata or arena-side tools.
3. **SCORE channel:** how does the realized 0–100 come back after "Evaluation pending" — inline, SSE, a status/poll tool, or the next `get_tasks`? Build the matching score-reader. Confirm skip is truly penalty-free and doesn't recycle the same task.
4. **Endpoint + submit shape:** confirm live endpoint (`agent-arena.dev/mcp` vs Cloud Run) and whether `executionId` is required — via the smoke test, before any one-shot submit.

## 10. Open questions to resolve on-site

- Does a wrong/low submit BAN (run-ending) or just score low? One strike or N? *(flips submit-vs-skip default)*
- Is `skip_task` truly penalty-free in all respects (rank, level, rate), and does it return a FRESH task or recycle?
- How does the realized score come back? *(no ledger/calibration input without it)*
- Does Traceloop observe our in-process Claude tool calls and attribute to the taskId, or only the 4 arena calls?
- Exact `get_tasks` schema — is there a `category` field? An expected OUTPUT FORMAT/constraints the evaluator scores on?
- Does "token expenditure" count input+output+THINKING or output only? Per-task or cumulative? *(sets how expensive the critic may be)*
- Confirmed live endpoint + accepted `submit_task` arg shape.
- Is `register_agent` idempotent across a process restart (same name → same agentId)? *(crash-recovery resume)*

## 11. Current repo state & cleanup

**Kept / accurate:**
- `arena/MCP-DOCS.md`, `arena/TUTORIAL.md` — accurate captured references + the probe checklist. Don't modify; resolve their open questions on-site.
- `agent/agent.py` core (manual ReAct loop, guards, errors-as-tool-results, cache-stable `specs()`) — right pattern; **modify** per Phases 0–1 (not yet done).
- `agent/test_agent.py` — right pattern, all pass; **extend** per Phase 1.
- `notes/` — the live running log for the day.

**Removed in this cleanup (encoded the wrong "game" model):**
- `arena/STRATEGY.md` — assumed real-time ticks / opponents / we-host-an-endpoint. Replaced by this doc.
- `arena/server.py` — a FastAPI inbound endpoint; we're an MCP client that connects OUT, so there's no inbound contract.

**To build (the bulk of remaining work — the arena interface is entirely unbuilt):**
- `arena/orchestrator.py` (state machine + classifier + ledger), `arena/mcp_client.py` (resilient `mcp_call`), `arena/run_log.jsonl` (memory/feedback/crash-recovery).
- Rewrite `agent/requirements.txt` (`anthropic>=0.69`; +fastmcp/httpx/traceloop-sdk/opentelemetry-sdk; −fastapi/uvicorn) and `agent/agent.py` `SYSTEM_PROMPT` + loop per Phases 0–1.

## 12. LIVE FINDINGS — confirmed against the real arena (2026-06-20)

The setup baseline (Anthropic SDK + FastMCP, **no helper tools, no eval gate**) was run live by
Raghav. The full pipeline works end-to-end: **register → get_tasks → Claude solves → submit_task**.
It solved 9 tasks and climbed **Level 1 → Level 6** (5 level-ups) in one process.

**Confirmed response shapes (these resolve §9/§10 unknowns):**
- `register_agent` → **JSON**: `{"status":"REGISTERED","agentId":"<id>","level":1,"message":"..."}`
  (NOT the `AGENT_ID:` text the docs implied — the reference bot's regex would fail here). Parsers
  are now JSON-first (`arena/mcp_client.py`).
- `get_tasks` → **JSON task**: `{id, title, description, level, points, ...}`. `id` is sometimes a
  slug (`grounded-search-framework-lifecycle`), sometimes a random token (`3i22FyUH0RZ7NYnIpOMb`).
- End-of-tasks → **plain text**: `ALL_TASKS_ATTEMPTED: You have solved all active tasks for Level N.
  Please wait for level advancement or new tasks.` (our `parse_task` returns None → clean stop).
- `submit_task` → score parses fine; **≥70 = LEVEL_UP confirmed** (70 leveled up, 60 did not).

**⚠️ `register_agent` is NOT idempotent.** Each call returns a NEW agentId (`aTx5...` then `DJ7...`),
despite the same name — so each run is a fresh agent **starting at Level 1**. Progress does NOT
persist across runs. ⇒ **The real competitive run must be ONE strong single process**; re-running
throws away progress. (Contradicts the tutorial's "won't create a duplicate.")

**Score breakdown — the low scores are exactly the tool-needing tasks (validates the thesis):**
- Grounded Search (data_extraction) → **15** · needs `web_search` grounding.
- BigCodeBench/10 (code_synthesis) → **30** · needs `code_execution` to run/verify.
- Basic Log Extraction (data_extraction) → **60** · needs programmatic parsing.
- Ticker (general) → **15**.
- The rest (Python/0, Financial Math, BigCodeBench/540, Saga, Swarm Consensus) → **70–80**, leveled up.

**Build-phase priorities (now data-driven):**
1. `code_execution` — write+run+verify code (fix the 30s).
2. `web_search` / grounding — fix grounded-search/data tasks (the 15s).
3. The eval/verify PASS-gate — lift 60→70+ and dodge the lucky-guess penalty.
4. **Keep-alive on `ALL_TASKS_ATTEMPTED`** — wait/poll for level advancement instead of exiting.
5. Run as a single long process (register once; don't restart — see non-idempotency above).
