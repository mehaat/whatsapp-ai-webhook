"""
mcp/server.py
-------------
ME-HAAT Fashion AI Bot v10.0 — Model Context Protocol (MCP) server blueprint.

A single additive Flask blueprint (:data:`mcp_bp`, no ``url_prefix``) that
speaks MCP over JSON-RPC 2.0 at ``POST /mcp``. External MCP clients (Claude
Desktop, IDEs) use it to discover the store's agent tools (``tools/list``) and
invoke them (``tools/call``). The tool registry is shared with the multi-agent
system (:mod:`agents.tools`), so a capability is defined once and reused here.

High-risk tools (refunds, broadcasts, coupons) stay approval-gated: calling one
via MCP returns a non-error "queued for approval" message rather than executing.

Everything is guarded — a handler error becomes a JSON-RPC error object, never a
500. When ``config.mcp_enabled`` is false, every route responds with 404.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Union

from flask import Blueprint, Response, jsonify, request

from agents.tools import call_tool, mcp_tool_schemas
from config import config
from utils.logging import logger

mcp_bp = Blueprint("mcp", __name__)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "mehaat-fashion"

# JSON-RPC 2.0 standard error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _enabled() -> bool:
    """True when the MCP surface is switched on."""
    return bool(getattr(config, "mcp_enabled", True))


def _rpc_result(req_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _public_tool_schemas() -> List[Dict[str, Any]]:
    """MCP ``tools/list`` objects: valid name/description/inputSchema, with the
    internal ``_meta`` moved under ``annotations`` for MCP compatibility."""
    tools: List[Dict[str, Any]] = []
    for schema in mcp_tool_schemas():
        meta = schema.get("_meta") or {}
        tools.append({
            "name": schema.get("name"),
            "description": schema.get("description", ""),
            "inputSchema": schema.get("inputSchema") or {"type": "object", "properties": {}},
            "annotations": meta,
        })
    return tools


def _handle_initialize(params: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "serverInfo": {"name": SERVER_NAME, "version": config.version},
        "capabilities": {"tools": {}},
    }


def _handle_tools_list(params: Dict[str, Any]) -> Dict[str, Any]:
    return {"tools": _public_tool_schemas()}


def _handle_tools_call(params: Dict[str, Any]) -> Dict[str, Any]:
    params = params or {}
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not name:
        # Signal a params problem to the outer dispatcher.
        raise _RpcException(INVALID_PARAMS, "missing tool name")

    outcome = call_tool(str(name), dict(arguments), actor="mcp")

    # Approval-gated high-risk tools: not an error, just queued.
    if isinstance(outcome, dict) and outcome.get("status") == "pending_approval":
        text = outcome.get("message") or (
            "This action is high-risk and has been queued for human approval "
            f"(approval_id={outcome.get('approval_id')})."
        )
        return {"content": [{"type": "text", "text": str(text)}], "isError": False}

    ok = bool(isinstance(outcome, dict) and outcome.get("ok"))
    if ok:
        payload: Any = outcome.get("result")
    else:
        payload = outcome.get("error") if isinstance(outcome, dict) else outcome

    try:
        text = json.dumps(payload, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        text = str(payload)

    return {"content": [{"type": "text", "text": text}], "isError": not ok}


def _handle_ping(params: Dict[str, Any]) -> Dict[str, Any]:
    return {}


_METHODS = {
    "initialize": _handle_initialize,
    "tools/list": _handle_tools_list,
    "tools/call": _handle_tools_call,
    "ping": _handle_ping,
}


class _RpcException(Exception):
    """Internal signal carrying a JSON-RPC error code/message."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _dispatch_one(req: Any) -> Optional[Dict[str, Any]]:
    """Handle a single JSON-RPC request object. Returns a response dict, or
    ``None`` for a notification (no ``id``). Never raises."""
    if not isinstance(req, dict):
        return _rpc_error(None, INVALID_REQUEST, "Invalid Request")

    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    if not isinstance(method, str):
        return _rpc_error(req_id, INVALID_REQUEST, "Invalid Request")

    handler = _METHODS.get(method)
    if handler is None:
        return _rpc_error(req_id, METHOD_NOT_FOUND, "Method not found")

    try:
        result = handler(params if isinstance(params, dict) else {})
        return _rpc_result(req_id, result)
    except _RpcException as exc:
        return _rpc_error(req_id, exc.code, exc.message, exc.data)
    except Exception as exc:  # noqa: BLE001 - a handler error is an RPC error, not a 500
        logger.error("MCP | handler '%s' failed: %s", method, exc)
        return _rpc_error(req_id, INTERNAL_ERROR, "Internal error")


@mcp_bp.route("/mcp", methods=["POST"])
def mcp_rpc():
    """MCP JSON-RPC 2.0 endpoint. Supports single and batch requests."""
    if not _enabled():
        return jsonify({"error": "mcp disabled"}), 404

    raw = request.get_data(cache=False, as_text=True) or ""
    try:
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001
        return jsonify(_rpc_error(None, PARSE_ERROR, "Parse error")), 200

    # Batch: a JSON array of request objects.
    if isinstance(payload, list):
        if not payload:
            return jsonify(_rpc_error(None, INVALID_REQUEST, "Invalid Request")), 200
        responses = [r for r in (_dispatch_one(item) for item in payload) if r is not None]
        return jsonify(responses), 200

    response = _dispatch_one(payload)
    if response is None:
        # Notification: no response body expected.
        return Response(status=204)
    return jsonify(response), 200


@mcp_bp.route("/mcp/tools", methods=["GET"])
def mcp_tools_debug():
    """Convenience JSON dump of the MCP tool schemas (for humans/debugging)."""
    if not _enabled():
        return jsonify({"error": "mcp disabled"}), 404
    tools = _public_tool_schemas()
    return jsonify({"count": len(tools), "tools": tools}), 200


@mcp_bp.route("/mcp", methods=["GET"])
def mcp_info():
    """Small human-friendly info page describing the MCP endpoint."""
    if not _enabled():
        return jsonify({"error": "mcp disabled"}), 404

    try:
        tool_count = len(_public_tool_schemas())
    except Exception:  # noqa: BLE001
        tool_count = 0

    # Content negotiation: JSON for API clients, HTML for browsers.
    info = {
        "server": SERVER_NAME,
        "version": config.version,
        "protocol": "Model Context Protocol",
        "protocolVersion": PROTOCOL_VERSION,
        "transport": "JSON-RPC 2.0 over HTTP",
        "endpoint": "POST /mcp",
        "methods": ["initialize", "tools/list", "tools/call", "ping"],
        "tools_count": tool_count,
        "tools_endpoint": "GET /mcp/tools",
    }
    accept = (request.headers.get("Accept") or "").lower()
    if "text/html" in accept:
        html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{SERVER_NAME} MCP server</title></head>
<body style="font-family:system-ui,sans-serif;max-width:640px;margin:2rem auto">
  <h1>{SERVER_NAME} — MCP tool server</h1>
  <p>Version {config.version} · protocol {PROTOCOL_VERSION} · JSON-RPC 2.0 over HTTP.</p>
  <p><strong>{tool_count}</strong> tools available.</p>
  <h2>Connect</h2>
  <p>Point an MCP client at <code>POST /mcp</code>. Methods:
     <code>initialize</code>, <code>tools/list</code>, <code>tools/call</code>,
     <code>ping</code>.</p>
  <p>Browse the tool catalogue at <a href="/mcp/tools"><code>GET /mcp/tools</code></a>.</p>
  <p>High-risk tools are human-approval gated.</p>
</body></html>"""
        return Response(html, mimetype="text/html")
    return jsonify(info), 200
