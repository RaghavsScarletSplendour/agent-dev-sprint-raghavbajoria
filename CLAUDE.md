# CLAUDE.md — Agent Dev-Sprint

## What this folder is

This is my working folder for **Agent Dev-Sprint: Build, Deploy, and Battle** — a one-day, hands-on event about building autonomous AI agents, ending in a live "Agent Arena" leaderboard battle. Everything I do for this event lives here: notes, scratch code, the agent I build, and whatever the organizers hand out on-site.

I'll be **dropping notes as the day goes** — captured live, often rough. Treat them as the running source of truth for what's happening and what I need next.

> 📌 **Full event details are in [`EVENT.md`](EVENT.md)** — the canonical, chat-reset-proof reference (logistics, entry rules, the "Laps" flow, arena format, the goal). Read it first if you're missing context. The summary below is just a quick glance.

## The event at a glance

- **Date:** Saturday, June 20th, 2026 · 10:30 AM – 4:30 PM (arrive 10:00 AM, check-in is strict)
- **Venue:** Amadeus Software Labs, 6 Etamin B Block, Prestige Tech Park, Kadubeesanahalli, Marathahalli, Bengaluru
- **Bring:** laptop, charger, Government ID, an IDE + Python environment ready to go. Starter kits / boilerplate provided on-site.
- **Organizer:** Joinal Ahmed

**The day's "Laps":**
1. **Lap 1 — Warm-Up:** deep dives on agent architecture (Perception, Reasoning via ReAct / Chain-of-Thought, Tool-Calling).
2. **Lap 2 — Straightaway (Hacking Phase 1):** set up from starter kit, build core agent logic, test local execution loops.
3. **Pit Stop:** lunch + mentorship / unblocking.
4. **Lap 3 — Final Corner (Optimization):** tune prompts, add custom tools, run sandbox simulations.
5. **Checkered Flag — Agent Arena:** deploy the agent endpoint to the live leaderboard; agents parse dynamic data, call APIs, react to real-time events, and battle head-to-head.

## How to help me here

1. **Read my latest notes first.** Before acting, skim the newest notes in this folder so you're working from where the day actually is — not from this summary, which goes stale fast.
2. **Default to capturing, not editing.** When I paste a note or a thought, file it cleanly (right place, dated) unless I clearly ask you to build or change something.
3. **When I am building the agent**, bias toward the event's reality: this is a fast, gamified arena. Working-and-deployed beats elegant-and-late. Keep changes small, get a local execution loop running early, then optimize.
4. **Match whatever the starter kit gives us.** Once the boilerplate/framework is on disk, follow its patterns and conventions instead of inventing my own. Read the provided files before writing code against them.
5. **Surface blockers loudly.** Mentor time is limited — if something's stuck (env, API keys, the agent looping endlessly, a deploy failing), say so plainly with the exact error so I can take it to a mentor.
6. **Ask before doing anything outward-facing** — deploying to the arena endpoint, hitting external APIs with credentials, or anything hard to undo.

## Suggested layout (create as needed)

- `notes/` — my live notes, one file per topic or `notes/YYYY-MM-DD-<lap>.md`
- `agent/` — the actual agent I'm building (once the starter kit lands)
- `scratch/` — throwaway experiments, prompt drafts, snippets
- `arena/` — anything about the deployment target, leaderboard, API contracts

Nothing here is fixed — adapt the layout to whatever the starter kit and the day demand.

## Agent-building reminders (from the Lap 1 themes)

These are the pillars the event is built around — useful defaults when I'm building:
- **Perception → Reasoning → Tool-Calling** is the core loop. Get a minimal version of all three running before polishing any one.
- **ReAct / Chain-of-Thought** for the reasoning step.
- **Guard against infinite loops** — step caps, repeat-action detection, a clear stop condition. This is called out explicitly as a thing agents get wrong.
- **Watch the context window** — keep prompts tight; the arena rewards fast, cheap decisions.
- **Tool-use is where the points are** — the arena agent must parse dynamic data and call APIs to make strategic, real-time decisions.

When I'm building anything LLM-facing, default to the latest Claude models and check the `claude-api` skill for current model IDs and params rather than guessing.
