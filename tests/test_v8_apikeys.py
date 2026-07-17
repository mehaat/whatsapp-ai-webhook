"""
tests/test_v8_apikeys.py
-------------------------
Tests for the v8.0 developer API-key layer (:mod:`commerce.apikeys`) and the
public developer portal (:mod:`commerce.dev_portal`).

No network / no mocks: the commerce DB (which includes the ``api_keys`` table
via ``Base.metadata``) is bootstrapped through :func:`commerce.bootstrap`, then
the key service is exercised directly. The developer portal blueprint is
registered on a fresh, minimal Flask app whose template folder points at the
project's top-level ``templates/`` directory.
"""

from __future__ import annotations

import os

# Self-contained SQLite DB for the run (config reads DATABASE_URL at import time,
# so it must be set before any project module is imported).
os.environ.setdefault("DATABASE_URL", "sqlite:///test_v8_apikeys.db")

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

import commerce  # noqa: E402
from commerce import apikeys  # noqa: E402
from commerce.dev_portal import dev_portal_bp  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module", autouse=True)
def _bootstrap() -> None:
    """Ensure the commerce schema (incl. api_keys) exists once for the module."""
    commerce.bootstrap()


@pytest.fixture()
def portal_client():
    """A test client for a fresh app with only the dev-portal blueprint."""
    app = Flask(__name__, template_folder=os.path.join(_ROOT, "templates"))
    app.register_blueprint(dev_portal_bp)
    return app.test_client()


# --------------------------------------------------------------------------
# apikeys service
# --------------------------------------------------------------------------

def test_issue_key_returns_plaintext_once() -> None:
    """issue_key returns a full plaintext key prefixed mh_live_."""
    result = apikeys.issue_key("Test key", scopes="read,write")
    assert result, "issue_key must return a non-empty dict"
    assert result["api_key"].startswith("mh_live_")
    assert result["prefix"] and result["prefix"] in result["api_key"]
    assert result["scopes"] == ["read", "write"]
    assert result["rate_limit_per_min"] == 120
    assert isinstance(result["id"], int)


def test_verify_key_roundtrip() -> None:
    """verify_key authenticates the exact key that was issued."""
    issued = apikeys.issue_key("Roundtrip key", scopes="read")
    record = apikeys.verify_key(issued["api_key"])
    assert record is not None
    assert record["prefix"] == issued["prefix"]
    assert record["id"] == issued["id"]
    assert record["scopes"] == ["read"]
    assert record["rate_limit_per_min"] == 120


def test_verify_key_rejects_wrong_key() -> None:
    """A key that was never issued does not authenticate."""
    assert apikeys.verify_key("wrong") is None
    assert apikeys.verify_key("mh_live_deadbeef_not-a-real-secret") is None


def test_revoked_key_no_longer_verifies() -> None:
    """After revocation the key is rejected."""
    issued = apikeys.issue_key("Revoke me")
    assert apikeys.verify_key(issued["api_key"]) is not None
    outcome = apikeys.revoke_key(issued["id"], actor="tester")
    assert outcome.get("ok") is True
    assert apikeys.verify_key(issued["api_key"]) is None


def test_check_rate_limit_enforces_ceiling() -> None:
    """check_rate_limit returns False once the tiny limit is exceeded."""
    apikeys._reset_rate_limits()
    prefix = "ratepref"
    assert apikeys.check_rate_limit(prefix, 2) is True
    assert apikeys.check_rate_limit(prefix, 2) is True
    assert apikeys.check_rate_limit(prefix, 2) is False


# --------------------------------------------------------------------------
# dev portal
# --------------------------------------------------------------------------

def test_developer_portal_renders(portal_client) -> None:
    """GET /developers returns 200 and documents the X-API-Key header."""
    resp = portal_client.get("/developers")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "X-API-Key" in body
    assert "/api/docs" in body
