# Arena Strategy — how we win the Showdown

Goal: top the live leaderboard. The arena is a gamified sim where agents parse
dynamic data, call APIs, and react to real-time events. Below is the plan; fill
in the unknowns the moment the rules drop on-site.

## The one thing that decides it

Most agents in a live arena lose for one of three reasons, in order:
1. **They loop / hang** and forfeit turns. → Already solved: `MAX_STEPS` + repeat-action guard in `agent/agent.py`.
2. **They're too slow per tick** and miss the real-time window. → Keep turns cheap: low/medium `effort`, tight context, minimal tools.
3. **They misread the scoring** and optimize the wrong thing. → Read the rubric first, encode it in the system prompt, optimize the actual win metric.

Win = reliable + fast + aimed at the real score. In that order.

## First 10 minutes after rules drop (fill these in)

- [ ] **Win condition / scoring** — exactly what increments the leaderboard? Write it verbatim here: ______
- [ ] **State format** — what does the arena send each tick (JSON shape)? ______
- [ ] **Action API** — what can the agent DO, and what's the call signature / endpoint? ______
- [ ] **Turn/rate limits** — time per move? calls per tick? token/cost caps? ______
- [ ] **Failure modes** — what happens on a timeout, bad action, or crash? ______
- [ ] **Endpoint contract** — what shape must our deployed endpoint expose? ______

## Build order (Laps 2–3)

1. **Local loop first.** Hardcode one sample tick of arena state, get `Agent.run` to emit one valid action. Don't touch the network until this works.
2. **Map every action to a tool.** One `@tools.add` per legal move; schema = exactly the args the API needs. No extra tools — they only slow decisions.
3. **Encode scoring in the system prompt.** State the win metric and the "always do / never do" rules explicitly. The model follows a clear rubric.
4. **Add a fallback move.** If reasoning stalls or a call fails, the agent should always have a safe default action rather than passing/forfeiting.
5. **Then deploy.** Wrap the loop in the required endpoint shape only after it's solid locally.

## Tuning for the live round

- **Speed:** drop `effort` to `low` for routine ticks; reserve `medium`/`high` for pivotal moves only.
- **Context discipline:** don't resend the whole history every tick if the arena is stateful — send current state + a short rolling summary. Watch the token count.
- **Determinism where it helps:** for a clearly-correct move, a tight prompt + low effort beats deliberation.
- **Observe opponents:** if opponent moves are visible in the state, feed them in — react, don't just act blind.

## Pre-flight checklist (before submitting to the leaderboard)

- [ ] Agent never hangs (guards verified against a stuck-state input)
- [ ] Every action it can emit is a valid arena action (schema matches)
- [ ] Handles malformed / unexpected state without crashing (errors → recover)
- [ ] Median turn comfortably under the time limit
- [ ] A sensible fallback move on any failure
- [ ] Endpoint returns the exact contract the arena expects
- [ ] API key / secrets set in the deploy env, not hardcoded

## Mentor questions to bank

Limited mentor time — spend it on what we can't quickly test ourselves:
- Edge cases in the scoring we might be misreading
- The fastest legit way to cut per-tick latency
- Whether opponent state is exploitable
- Any known gotchas in the arena's endpoint/deploy path
