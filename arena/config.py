"""
Runtime configuration for the arena agent.

Single source of truth for all config + secrets, loaded from the repo-root `.env`
(gitignored; see `.env.example` for the shape). `assert_valid()` fails LOUD at startup,
listing every missing/blank required var — so a half-filled `.env` dies at boot, not
mid-task. Secret VALUES are never printed (only variable names).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load the repo-root .env regardless of the current working directory.
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_PATH)

# Documented arena endpoints (arena/MCP-DOCS.md + arena/TUTORIAL.md).
DEFAULT_MCP_ENDPOINT = "https://agent-arena.dev/mcp"
FALLBACK_MCP_ENDPOINT = "https://agent-arena-623774504237.asia-southeast1.run.app/mcp"

# Vars that must be real before we can register + run. (TRACELOOP_API_KEY and
# ARENA_PLATFORM_UID are recommended but not fatal in setup; AGENT_NAME/STACK default.)
REQUIRED = ("ANTHROPIC_API_KEY", "ARENA_ID_TOKEN", "GITHUB_URL", "LINKEDIN_URL")


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str
    arena_id_token: str
    arena_platform_uid: str
    traceloop_api_key: str
    mcp_endpoint: str
    mcp_endpoint_fallback: str
    agent_name: str
    agent_stack: str
    github_url: str
    linkedin_url: str


def load_config() -> Config:
    """Read config from the environment (the .env was loaded at import time)."""
    return Config(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        arena_id_token=os.environ.get("ARENA_ID_TOKEN", ""),
        arena_platform_uid=os.environ.get("ARENA_PLATFORM_UID", ""),
        traceloop_api_key=os.environ.get("TRACELOOP_API_KEY", ""),
        mcp_endpoint=os.environ.get("ARENA_MCP_ENDPOINT", "") or DEFAULT_MCP_ENDPOINT,
        mcp_endpoint_fallback=FALLBACK_MCP_ENDPOINT,
        agent_name=os.environ.get("AGENT_NAME", "") or "raghav-arena-agent",
        agent_stack=os.environ.get("AGENT_STACK", "") or "Python / Anthropic Claude / FastMCP",
        github_url=os.environ.get("GITHUB_URL", ""),
        linkedin_url=os.environ.get("LINKEDIN_URL", ""),
    )


def _is_blank_or_placeholder(value: str) -> bool:
    """True if a value is empty or still a `.env.example` placeholder."""
    v = (value or "").strip()
    if not v:
        return True
    if v.startswith("<") and v.endswith(">"):  # e.g. "<firebase jwt from the arena web app>"
        return True
    lowered = v.lower()
    markers = ("fill_me_in", "sk-ant-...", "your-handle", "your-agent-name", "your-key-here")
    return any(m in lowered for m in markers)


def assert_valid(cfg: Config) -> None:
    """Raise SystemExit naming EVERY missing/blank required var. Prints names only."""
    value_by_name = {
        "ANTHROPIC_API_KEY": cfg.anthropic_api_key,
        "ARENA_ID_TOKEN": cfg.arena_id_token,
        "GITHUB_URL": cfg.github_url,
        "LINKEDIN_URL": cfg.linkedin_url,
    }
    missing = [name for name in REQUIRED if _is_blank_or_placeholder(value_by_name[name])]
    if missing:
        raise SystemExit(
            "Missing/placeholder required .env vars: " + ", ".join(missing) + ".\n"
            "Copy .env.example -> .env and fill them in (the ~1h JWT must be fresh)."
        )
