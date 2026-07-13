"""
tests/test_v10_mcp.py
---------------------
Tests for the v10.0 MCP (Model Context Protocol) tool server blueprint.
"""

from __future__ import annotations

import json

import pytest
from flask import Flask

import commerce
from mcp.server import mcp_bp


@pytest.fixture(scope="module")
def client():
    commerce.bootstrap()  # register commerce services / tools
    app = Flask(__name__)
    app.register_blueprint(mcp_bp)
    return app.test_client()


def _rpc(client, method, params=None, req_id=1):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    resp = client.post("/mcp", data=json.dumps(body), content_type="application/json")
    return resp


def test_initialize(client):
    resp = _rpc(client, "initialize", {})
    assert resp.status_code == 200
    data = resp.get_json()
    result = data["result"]
    assert result["serverInfo"]["name"]
    assert result["protocolVersion"] == "2024-11-05"


def test_tools_list(client):
    resp = _rpc(client, "tools/list", {})
    assert resp.status_code == 200
    tools = resp.get_json()["result"]["tools"]
    assert isinstance(tools, list) and len(tools) > 0
    for t in tools:
        assert t.get("name")
        assert "description" in t
        assert "inputSchema" in t
    names = {t["name"] for t in tools}
    assert "search_products" in names


def test_tools_call(client):
    resp = _rpc(
        client,
        "tools/call",
        {"name": "search_products", "arguments": {"query": "saree"}},
    )
    assert resp.status_code == 200
    result = resp.get_json()["result"]
    assert isinstance(result["content"], list)
    assert result["isError"] is False


def test_unknown_method(client):
    resp = _rpc(client, "does/not/exist", {})
    assert resp.status_code == 200
    assert resp.get_json()["error"]["code"] == -32601


def test_parse_error(client):
    resp = client.post("/mcp", data="{bad", content_type="application/json")
    assert resp.status_code == 200
    assert resp.get_json()["error"]["code"] == -32700
