"""
mcp
---
ME-HAAT Fashion AI Bot v10.0 — Model Context Protocol (MCP) tool server.

Exposes the internal agent tool registry (:mod:`agents.tools`) over an
MCP-compatible JSON-RPC 2.0 HTTP endpoint, so external MCP clients (Claude
Desktop, IDEs, etc.) can discover and invoke the store's tools.

Import-safe: importing this package performs no network or database work. Wire
it up by registering :data:`mcp.server.mcp_bp` on the Flask app.
"""

from __future__ import annotations

from config import config
from utils.logging import logger


def is_enabled() -> bool:
    """True when the MCP tool server is switched on (default)."""
    return bool(getattr(config, "mcp_enabled", True))
