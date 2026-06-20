"""
Arena endpoint — a thin HTTP wrapper around the agent core, ready to deploy.

The Showdown requires deploying an agent endpoint to the live leaderboard. This
is the scaffold for it: the arena POSTs state, we run the agent, we return an
action. ADAPT the request/response shape to the arena's actual contract once it
drops (see arena/STRATEGY.md "first 10 minutes").

Run:  ANTHROPIC_API_KEY=sk-... uvicorn server:app --host 0.0.0.0 --port 8000
Test: curl -s localhost:8000/health
      curl -s -X POST localhost:8000/act -H 'content-type: application/json' \
           -d '{"task":"What is 6*7? Use the calc tool."}'
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

# Import the agent core from ../agent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))
from agent import Agent  # noqa: E402

app = FastAPI(title="Dev-Sprint Arena Agent")
_agent = Agent()


class ActRequest(BaseModel):
    # ADAPT: replace `task` with the arena's actual per-tick state fields.
    task: str


class ActResponse(BaseModel):
    action: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/act", response_model=ActResponse)
def act(req: ActRequest) -> ActResponse:
    # ADAPT: build the task/state string the arena sends, return the arena's
    # action shape. The agent loop itself doesn't change.
    return ActResponse(action=_agent.run(req.task))
