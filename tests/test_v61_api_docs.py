"""
tests/test_v61_api_docs.py
--------------------------
Tests for the v6.1 OpenAPI spec + Swagger UI blueprint
(:mod:`commerce.api_docs`).

The blueprint is registered on a fresh, minimal Flask app so the tests do
not depend on the full application factory.
"""

from __future__ import annotations

import pytest
from flask import Flask

from commerce.api_docs import api_docs_bp


@pytest.fixture()
def client():
    """A test client for a fresh app with only the docs blueprint registered."""
    app = Flask(__name__)
    app.register_blueprint(api_docs_bp)
    return app.test_client()


def test_openapi_json_is_valid_spec(client):
    """GET /api/openapi.json returns a well-formed OpenAPI 3.x document."""
    resp = client.get("/api/openapi.json")
    assert resp.status_code == 200

    spec = resp.get_json()
    assert spec is not None, "response body must be valid JSON"

    assert spec["openapi"].startswith("3.")
    assert spec["openapi"] == "3.0.3"

    assert spec["info"]["title"] == "ME-HAAT Fashion Commerce API"
    assert spec["info"]["version"]  # version present and non-empty


def test_openapi_paths_cover_key_endpoints(client):
    """The spec documents the token, orders and tracking paths."""
    spec = client.get("/api/openapi.json").get_json()
    paths = spec["paths"]

    assert "/api/token" in paths
    assert "/orders" in paths
    assert "/tracking/{ref}" in paths


def test_openapi_has_order_schema(client):
    """components.schemas exposes the Order schema."""
    spec = client.get("/api/openapi.json").get_json()
    assert "Order" in spec["components"]["schemas"]


def test_openapi_security_schemes_present(client):
    """Both bearer and API-key security schemes are declared."""
    spec = client.get("/api/openapi.json").get_json()
    schemes = spec["components"]["securitySchemes"]
    assert "bearerAuth" in schemes
    assert "apiKeyAuth" in schemes


def test_protected_orders_path_has_security(client):
    """The /orders path carries a non-empty security requirement."""
    spec = client.get("/api/openapi.json").get_json()
    security = spec["paths"]["/orders"]["get"]["security"]
    assert security, "protected /orders must declare a security requirement"


def test_public_tracking_path_has_no_security(client):
    """The public /tracking path declares an empty security requirement."""
    spec = client.get("/api/openapi.json").get_json()
    assert spec["paths"]["/tracking/{ref}"]["get"]["security"] == []


def test_docs_page_renders_swagger_ui(client):
    """GET /api/docs returns HTML that boots Swagger UI against the spec."""
    resp = client.get("/api/docs")
    assert resp.status_code == 200

    body = resp.get_data(as_text=True)
    assert "swagger-ui" in body
    assert "/api/openapi.json" in body
