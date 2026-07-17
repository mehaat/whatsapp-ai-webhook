"""
agents
------
ME-HAAT Fashion AI Bot v10.0 multi-agent system: a shared tool registry
(:mod:`agents.tools`), a generic specialist :class:`agents.base.Agent`, and the
orchestrator that routes each message to the right specialist. Import-safe;
registering this package performs no network or database work.
"""

from __future__ import annotations

from config import config
from utils.logging import logger


def is_enabled() -> bool:
    """True when the multi-agent orchestrator is switched on."""
    return bool(getattr(config, "agents_enabled", True))
