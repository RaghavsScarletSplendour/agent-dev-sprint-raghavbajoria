# Arena Agent — how to run

An MCP **client** that registers with the Agent Arena, pulls tasks, solves them with
`claude-opus-4-8`, and submits/skips. **Claude Code built this; you run it** (the agent talks
to `agent-arena.dev` on its own — Claude Code never connects to the arena).

> This is the SETUP baseline: a correct, bounded `register → get_task → solve → submit/skip`
> loop with the proven guardrails. The point-getter tools, eval/critic gate, ledger, and
> efficiency tuning are the next ("build") phase — see `arena/MASTER-PLAN.md`.

## One-time setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r agent/requirements.txt
```

## Before every run — checklist (this is the load-bearing part)

1. **JWT is FRESH.** `ARENA_ID_TOKEN` in `.env` expires ~1 hour after sign-in. Re-paste a fresh
   one from the arena web app (DevTools → Application → Storage) right before running.
2. **`GITHUB_URL` + `LINKEDIN_URL` filled** in `.env` — `register_agent` requires both.
3. **`ANTHROPIC_API_KEY` set** in `.env`.
4. **`TRACELOOP_API_KEY` set** in `.env` (recommended — the arena uses tracing to credit tool use).
5. `.env` exists (copy from `.env.example` on a fresh clone). It is gitignored — never committed.

The agent fails loudly at startup naming any missing/placeholder var, so a half-filled `.env`
won't silently misbehave.

## Run

```bash
# from the repo root, venv active:
python -m arena.orchestrator
```

You'll see logs for: register (AGENT_ID + level), each task fetched (with its detected category),
and either a submit score / `LEVEL_UP` or a skip. The loop stops at `MAX_TASKS` (default 10) or
after `MAX_CONSEC_SKIPS` (default 3) consecutive skips — it can't run away.

## Troubleshooting

- **`SystemExit: [auth] ...` or a 401** — your JWT expired. Paste a fresh `ARENA_ID_TOKEN` in
  `.env` and re-run.
- **`Missing/placeholder required .env vars: ...`** — fill those in `.env`.
- **Endpoint trouble** — the client auto-falls back from `agent-arena.dev/mcp` to the Cloud Run
  URL; both live in `arena/config.py`.

## Layout

- `agent/agent.py` — the inner Claude solver (manual ReAct loop + loop guards).
- `agent/prompts.py` — the task prompt builder (ANALYZE → SOLVE → REVIEW, final-answer-only).
- `arena/config.py` — loads + validates `.env`.
- `arena/mcp_client.py` — the resilient MCP transport + response parsers.
- `arena/orchestrator.py` — the loop that owns the 4 arena tools (`python -m arena.orchestrator`).
- `agent/test_agent.py` — offline tests (no network): `python agent/test_agent.py`.

## Tests (offline — no network, no arena)

```bash
python agent/test_agent.py        # or: python -m pytest agent/test_agent.py -q
```
