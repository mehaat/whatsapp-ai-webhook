"""
tests/test_v9_devportal.py
---------------------------
Tests for the v9.0 developer-portal enhancements:

    * :func:`commerce.openapi_spec.build_full_spec` — the comprehensive
      OpenAPI 3.0.3 document.
    * :mod:`commerce.api_usage` — per-key usage recording and reporting.
    * :mod:`commerce.dev_portal` — the enhanced public portal page.

No network / no mocks: the commerce DB (which includes ``api_keys`` and
``api_usage`` via ``Base.metadata``) is bootstrapped through
:func:`commerce.bootstrap`, then the units are exercised directly. The dev
portal blueprint is registered on a fresh, minimal Flask app whose template
folder points at the project's top-level ``templates/`` directory.
"""

from __future__ import annotations

import os

# Self-contained SQLite DB for the run (config reads DATABASE_URL / the portal
# flag at import time, so both must be set before any project module imports).
os.environ.setdefault("DATABASE_URL", "sqlite:///test_v9_devportal.db")
os.environ.setdefault("DEVELOPER_PORTAL_ENABLED", "true")

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

import commerce  # noqa: E402
from commerce import api_usage  # noqa: E402
from commerce.dev_portal import dev_portal_bp  # noqa: E402
from commerce.openapi_spec import build_full_spec  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module", autouse=True)
def _bootstrap() -> None:
    """Ensure the commerce schema (incl. api_usage) exists once for the module."""
    commerce.bootstrap()


@pytest.fixture()
def portal_client():
    """A test client for a fresh app with only the dev-portal blueprint."""
    app = Flask(__name__, template_folder=os.path.join(_ROOT, "templates"))
    app.register_blueprint(dev_portal_bp)
    return app.test_client()


# --------------------------------------------------------------------------
# OpenAPI spec
# --------------------------------------------------------------------------

def test_build_full_spec_is_openapi_303() -> None:
    """The spec declares OpenAPI 3.0.3 with a title and version."""
    spec = build_full_spec()
    assert spec["openapi"] == "3.0.3"
    assert spec["info"]["title"]
    assert spec["info"]["version"]


def test_build_full_spec_documents_every_endpoint() -> None:
    """All core endpoint paths are present in the spec."""
    spec = build_full_spec()
    paths = spec["paths"]
    for expected in (
        "/orders",
        "/orders/{ref}",
        "/tracking/{ref}",
        "/api/token",
        "/payments/webhook/{provider}",
    ):
        assert expected in paths, f"missing path: {expected}"


def test_build_full_spec_has_order_schema() -> None:
    """The components block defines the Order schema."""
    spec = build_full_spec()
    schemas = spec["components"]["schemas"]
    assert "Order" in schemas
    assert schemas["Order"]["type"] == "object"


def test_public_paths_have_empty_security() -> None:
    """Public endpoints carry an explicit empty security list."""
    spec = build_full_spec()
    assert spec["paths"]["/tracking/{ref}"]["get"]["security"] == []
    assert spec["paths"]["/api/token"]["post"]["security"] == []
    assert spec["paths"]["/payments/webhook/{provider}"]["post"]["security"] == []
    # Protected path carries a non-empty security requirement.
    assert spec["paths"]["/orders"]["get"]["security"]


# --------------------------------------------------------------------------
# Usage recording / reporting
# --------------------------------------------------------------------------

def test_record_usage_then_usage_for() -> None:
    """Recording twice yields total==2 across a single daily bucket."""
    prefix = "test09aa"
    api_usage.record_usage(prefix, "GET /orders")
    api_usage.record_usage(prefix, "GET /orders/{ref}")

    report = api_usage.usage_for(prefix, days=30)
    assert report["prefix"] == prefix
    assert report["total"] == 2
    assert len(report["daily"]) == 1
    assert report["daily"][0]["count"] == 2
    assert report["last_endpoint"] == "GET /orders/{ref}"


def test_record_usage_never_raises_on_bad_input() -> None:
    """Empty prefix is a no-op and does not raise."""
    api_usage.record_usage("", "GET /orders")  # must not raise


def test_usage_summary_returns_list() -> None:
    """usage_summary returns a list (busiest first)."""
    prefix = "test09bb"
    api_usage.record_usage(prefix, "GET /orders")

    summary = api_usage.usage_summary(days=30)
    assert isinstance(summary, list)
    assert any(row["prefix"] == prefix for row in summary)


# --------------------------------------------------------------------------
# Public portal page
# --------------------------------------------------------------------------

def test_developers_page_renders(portal_client) -> None:
    """GET /developers returns 200 and contains the auth + code-sample content."""
    resp = portal_client.get("/developers")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "X-API-Key" in body
    assert "curl" in body
