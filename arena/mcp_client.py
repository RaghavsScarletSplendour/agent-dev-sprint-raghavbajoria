"""
The ONLY code that talks to the arena over the network.

`mcp_call` opens a FRESH FastMCP StreamableHttpTransport connection per call (reusing a
session times out while the model generates), with a single `.dev` -> Cloud Run fallback.
Auth failures raise `ArenaAuthError` (so the orchestrator can HALT and ask for a fresh
~1h JWT — never a silent retry); other failures raise `ArenaCallError` (never returned as
if they were a tool result — that silent-error pattern was a reference-bot bug).

The parsers are PURE (no network, no fastmcp) and never invent a wrong value: a miss
returns None, and the caller decides what a miss means. (The reference defaulted a missing
score to -1 and swallowed JSON errors with a bare `except: pass` — both caused wrong runs.)

fastmcp is imported lazily inside `mcp_call`, so this module (and its parsers) import fine
even before `pip install -r agent/requirements.txt`, and tests can monkeypatch `mcp_call`.
"""

from __future__ import annotations

import json
import re


class ArenaError(Exception):
    """Base class for arena transport errors."""


class ArenaAuthError(ArenaError):
    """401 / expired or invalid JWT. The orchestrator must HALT for a fresh idToken."""


class ArenaCallError(ArenaError):
    """A tool call failed for a non-auth reason (network, bad params, server error)."""

    def __init__(self, tool: str, detail: str) -> None:
        super().__init__(f"{tool}: {detail}")
        self.tool = tool
        self.detail = detail


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
def _extract_text(result: object) -> str:
    """Join the text content blocks of an MCP tool result."""
    content = getattr(result, "content", None) or []
    return "\n".join(getattr(b, "text", "") for b in content if getattr(b, "text", None))


def _looks_like_auth_error(exc: Exception) -> bool:
    s = str(exc).lower()
    return any(m in s for m in ("401", "unauthorized", "expired", "invalid token", "invalid jwt"))


async def _call_once(tool: str, args: dict, endpoint: str) -> str:
    # Lazy import so the module + parsers import without fastmcp installed.
    from fastmcp.client import Client
    from fastmcp.client.transports import StreamableHttpTransport

    transport = StreamableHttpTransport(url=endpoint)
    try:
        async with Client(transport, name="arena-agent") as client:
            result = await client.call_tool(tool, args)
    except Exception as exc:  # noqa: BLE001 — classify, then re-raise typed
        if _looks_like_auth_error(exc):
            raise ArenaAuthError(
                f"{tool}: auth rejected at {endpoint} — your ~1h JWT (ARENA_ID_TOKEN) is "
                "likely expired. Paste a fresh one and re-run."
            ) from exc
        raise
    return _extract_text(result)


async def mcp_call(
    tool: str,
    args: dict,
    *,
    endpoint: str,
    fallback_endpoint: str | None = None,
) -> str:
    """Call one arena MCP tool; return its text result.

    Tries `endpoint`; on any NON-auth failure retries ONCE against `fallback_endpoint`
    (if given). Auth failures are never retried — they raise `ArenaAuthError` immediately.
    """
    try:
        return await _call_once(tool, args, endpoint)
    except ArenaAuthError:
        raise
    except Exception as primary:  # noqa: BLE001
        if not fallback_endpoint:
            raise ArenaCallError(tool, f"{endpoint}: {primary}") from primary
        try:
            return await _call_once(tool, args, fallback_endpoint)
        except ArenaAuthError:
            raise
        except Exception as fallback:  # noqa: BLE001
            raise ArenaCallError(
                tool,
                f"both endpoints failed (primary {endpoint}: {primary}; "
                f"fallback {fallback_endpoint}: {fallback})",
            ) from fallback


# --------------------------------------------------------------------------- #
# Pure parsers — each returns Optional and never invents a wrong default.
# The arena returns JSON (confirmed from a live register_agent response:
#   {"status":"REGISTERED","agentId":"...","level":1,"message":"..."}), so each parser
# is JSON-first with a text/regex fallback for robustness against either shape.
# --------------------------------------------------------------------------- #
def _try_json(text: str) -> object | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def parse_agent_id(text: str) -> str | None:
    """Agent id from a register_agent response: JSON `agentId` (live shape) or `AGENT_ID:` text."""
    data = _try_json(text)
    if isinstance(data, dict):
        for key in ("agentId", "agent_id"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    m = re.search(r"AGENT[_ ]?ID[:\s]+([A-Za-z0-9_\-]+)", text or "", re.IGNORECASE)
    return m.group(1) if m else None


def parse_level(text: str) -> int | None:
    data = _try_json(text)
    if isinstance(data, dict) and isinstance(data.get("level"), int):
        return data["level"]
    m = re.search(r"Level[:\s]+(\d+)", text or "", re.IGNORECASE)
    return int(m.group(1)) if m else None


def parse_score(text: str) -> int | None:
    """Score 0-100 from a submit_task response: JSON `score` or `Score: N` text.
    Returns None (NOT -1, NOT 0) on no match — e.g. an async 'Evaluation pending' reply."""
    data = _try_json(text)
    if isinstance(data, dict) and isinstance(data.get("score"), (int, float)):
        return int(data["score"])
    m = re.search(r"Score[:\s]+(\d+)", text or "", re.IGNORECASE)
    return int(m.group(1)) if m else None


def parse_leveled_up(text: str) -> bool:
    """True if a submit response signals a level-up: a JSON bool field, or a LEVEL_UP marker."""
    data = _try_json(text)
    if isinstance(data, dict):
        for key in ("leveledUp", "leveled_up", "levelUp", "level_up"):
            if isinstance(data.get(key), bool):
                return data[key]
        blob = " ".join(str(data.get(k, "")) for k in ("message", "status", "result")).lower()
        return "level_up" in blob or "leveled up" in blob
    low = (text or "").lower()
    return "level_up" in low or "leveled up" in low


def parse_task(text: str) -> dict | None:
    """Parse a get_tasks response into a task dict.

    Accepts a dict with an `id`, a dict wrapping it under `task`, OR a non-empty list whose
    first item is a dict with `id`. Returns None on malformed/empty/no-task JSON (caller logs
    the raw text and skips) — never a bare `except: pass` that hides the failure.
    """
    data = _try_json(text)
    if isinstance(data, dict):
        if "id" in data:
            return data
        nested = data.get("task")
        if isinstance(nested, dict) and "id" in nested:
            return nested
    if isinstance(data, list) and data and isinstance(data[0], dict) and "id" in data[0]:
        return data[0]
    return None
