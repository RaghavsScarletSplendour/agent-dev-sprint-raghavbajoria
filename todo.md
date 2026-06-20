# todo.md — Arena Agent: SETUP plan (⏸ awaiting Raghav's approval before executing)

> Scope of this doc: **getting the codebase ready** (setup only). The full agent design
> (orchestrator, eval gate, Neural-Efficiency tuning) lives in `arena/MASTER-PLAN.md` and is the
> NEXT phase after setup is approved. Nothing in "Setup plan" below is executed until you approve.

## Locked decisions & scope (from Raghav, 2026-06-20)

- **Model = Anthropic / Claude** (`claude-opus-4-8`), using an **Anthropic API key**. Not Gemini.
- **Claude Code's role = BUILD THE CODEBASE ONLY.** This is a *programmatic* challenge. Claude
  Code does **NOT** connect to the arena MCP server and does **NOT** run the agent against the
  live arena. **Raghav runs the agent himself.** ⇒ everything we test here is **offline** (mocked
  MCP), and we ship a runnable codebase + a "how to run" handoff.
- **Reference code is imperfect.** The tutorial (`arena/TUTORIAL.md`, Google ADK + Gemini) and
  the reference repo are a *reference only* — the demo runs produced wrong outputs, so we do **not
  follow it 100%**. We borrow the proven bits (FastMCP transport, the 4-tool shapes, the loop
  skeleton) and replace the rest for Claude. ❓ *If there's a specific reference repo URL beyond
  the tutorial, drop it and I'll diff against it.*
- **Config lives in a gitignored `.env`** (done — see below), documented by a committed
  `.env.example`. `register_agent` **requires `GITHUB_URL` + `LINKEDIN_URL`** → both are in config.
- FYI: the tutorial's code-block **copy buttons don't work** (we transcribed manually).

## Phase S0 — Repo & secrets ✅ DONE this turn (no approval needed; you asked for it)

- [x] `.env` created (gitignored) holding the keys you pasted: `ARENA_ID_TOKEN` (JWT, ~1h),
      `ARENA_PLATFORM_UID`, `TRACELOOP_API_KEY`. ⚠️ Still to fill: `ANTHROPIC_API_KEY`,
      `GITHUB_URL`, `LINKEDIN_URL`.
- [x] `.env.example` committed (template, no secrets); `.gitignore` updated so `.env` stays out
      and `.env.example` stays in.
- [x] Git: remote → `agent-dev-sprint-raghavbajoria`, all current data pushed as the first content.

---

## Setup plan (⏸ proposed — approve before I execute any of this)

### S1 — Python env & dependencies
- [ ] Create a venv (`python -m venv .venv`); pin Python 3.11+.
- [ ] **Rewrite `agent/requirements.txt`** for the Anthropic stack:
      `anthropic>=0.69`, `fastmcp`, `httpx`, `python-dotenv`, `traceloop-sdk`,
      `opentelemetry-sdk` (we have a Traceloop key), `pytest` (dev).
      **Drop** `google-adk`, `google-genai`, `fastapi`, `uvicorn` (Gemini-/server-specific, not needed —
      we're an MCP *client* driven by the Anthropic SDK).
- [ ] `pip install -r requirements.txt` and run ONE offline SDK smoke call (adaptive thinking +
      `output_config.effort` + json_schema) to confirm the SDK version accepts the API we depend on.

### S2 — Project skeleton (files, mostly stubs at this stage)
- [ ] `arena/config.py` — load `.env` via `python-dotenv`; expose typed config; **assert** required
      vars non-empty at startup (`ANTHROPIC_API_KEY`, `ARENA_ID_TOKEN`, `GITHUB_URL`, `LINKEDIN_URL`)
      and fail loudly if missing.
- [ ] `arena/mcp_client.py` — `async mcp_call(tool, args)`: fresh FastMCP `StreamableHttpTransport`
      per call; endpoint `.dev` → Cloud Run fallback; parse text result blocks (regex `AGENT_ID`,
      `json.loads` the task). *(Adapted from the tutorial's `mcp_call`, hardened.)*
- [ ] `arena/orchestrator.py` — the run loop skeleton: `register_agent` once (with
      `GITHUB_URL`+`LINKEDIN_URL`) → `get_tasks` → solve via Claude → `submit_task`/`skip_task`.
      Owns the 4 arena tools; does not expose them to the inner LLM (per MASTER-PLAN).
- [ ] `agent/agent.py` — adapt the existing Claude ReAct core: replace the real-time-game
      `SYSTEM_PROMPT` with the task-solving one, generalize `run()` so the orchestrator drives it
      per task. *(Tool wiring / eval gate = the build phase, not setup.)*

### S3 — Config & identity wiring
- [ ] Fill `ANTHROPIC_API_KEY`, `GITHUB_URL`, `LINKEDIN_URL` in `.env` (you provide the two URLs).
- [ ] `register_agent` payload built from config; verify the field names match the docs
      (`idToken`, `name`, `stack`, `linkedinUrl`, `githubUrl`).

### S4 — Offline smoke tests (no network — Claude Code can't hit the live arena)
- [ ] Extend `agent/test_agent.py`: keep the existing guard tests; add a **mocked** `mcp_call`
      so the register → get_task → submit/skip flow is exercised end-to-end without the arena.
- [ ] `python agent/test_agent.py` green.

### S5 — Run handoff (so YOU can run it live)
- [ ] Short `agent/README.md` "how to run": `source .venv/bin/activate`, set `.env`, `python -m arena.orchestrator`.
- [ ] Checklist for live run: JWT fresh (<1h old), URLs filled, Anthropic key set, Traceloop key set.

---

## Open questions for Raghav
1. **Reference repo URL** — is there a specific GitHub reference repo (beyond the tutorial) you want me to diff against?
2. **`GITHUB_URL` / `LINKEDIN_URL`** — paste both (GitHub profile, e.g. `https://github.com/RaghavsScarletSplendour`, and your LinkedIn) so `register_agent` won't fail.
3. **Architecture confirm** — OK to use the **Anthropic SDK directly + FastMCP** (drop Google ADK)? That's the MASTER-PLAN approach; the alternative is keeping ADK and routing it to Claude (more deps, Gemini-centric framework). I recommend dropping ADK.
