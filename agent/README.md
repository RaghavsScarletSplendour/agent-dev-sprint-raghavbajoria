# Agent core — quick start

A framework-agnostic ReAct agent skeleton, ready to adapt to whatever the
starter kit hands us on-site.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-...
python agent.py            # runs the built-in smoke test
```

## What's in here

`agent.py` is one file with three pieces:

- **`Registry`** — register a tool with `@tools.add(name, description, schema)`. Tools are sorted by name so the prompt prefix stays byte-stable (prompt-cache friendly).
- **`Agent.run(task)`** — the manual ReAct loop: call the model → if it asks for tools, run them and feed results back → repeat until it answers or a guard trips.
- **Loop guards** — `MAX_STEPS` (hard ceiling) and `MAX_REPEAT_ACTIONS` (kills repeated identical calls). These are why our agent won't hang in the arena.

## Adapting it for the Arena (Lap 3 / Showdown)

1. **Replace the `calc` tool** with the arena's real tools/APIs — one `@tools.add` per action the agent can take.
2. **Edit `SYSTEM_PROMPT`** to encode the arena's rules, scoring, and win condition.
3. **Wire `Agent.run` to the arena loop** — if the arena pushes state each tick, feed it in as the next user message instead of a static task string.
4. **Tune `effort`** in `agent.py`: `low` for speed-critical ticks, `medium` (default) for balance, `high` when a move really matters.
5. If we must expose an HTTP endpoint, wrap `Agent.run` in a tiny FastAPI/Flask handler — the loop itself doesn't change.

## Why these choices

- **Manual loop, not the SDK tool-runner** — we need to see and gate every step (logging, loop guards, real-time state injection). The arena rewards control.
- **`claude-opus-4-8` + adaptive thinking** — latest, most capable model; the model decides how hard to think per turn, `effort` caps the spend.
- **Errors become tool results, not crashes** — a failed tool returns text the model can recover from, so one bad call doesn't kill the run.
