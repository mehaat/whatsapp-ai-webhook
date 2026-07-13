"""
tests/test_v7_security.py
--------------------------
Tests for the v7.0 security-hardening building blocks in
:mod:`admin.security_ext`:

    * Login-event recording + history round-trip (``login_events`` table).
    * ``ip_allowed`` semantics: empty allowlist allows all, a non-empty list
      excludes non-members, and CIDR ranges match.
    * TOTP: a freshly generated secret verifies the current code and rejects a
      wrong one.

No-network, no-mock: the DB is bootstrapped via :func:`commerce.bootstrap` and
the config is swapped with a lightweight namespace so the frozen ``Config`` is
never mutated.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite:///mehaat.db")

from types import SimpleNamespace

import pyotp

import commerce
from admin import security_ext


def _unique_user() -> str:
    return "user-" + uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------
# Login events
# --------------------------------------------------------------------------

def test_record_and_list_login_events():
    commerce.bootstrap()
    username = _unique_user()

    security_ext.record_login_event(
        username, ip="203.0.113.7", user_agent="pytest-UA", success=True
    )
    security_ext.record_login_event(
        username, ip="203.0.113.7", user_agent="pytest-UA", success=False
    )

    events = security_ext.list_login_events(username=username, limit=50)
    assert len(events) == 2
    # Newest first; both carry the fields we wrote.
    assert {e["success"] for e in events} == {True, False}
    for e in events:
        assert e["username"] == username
        assert e["ip"] == "203.0.113.7"
        assert e["user_agent"] == "pytest-UA"
        assert e["created_at"]


def test_record_login_event_never_raises_on_bad_input():
    # Must not raise even with odd input; simply records defensively.
    security_ext.record_login_event("", ip=None, user_agent=None, success=True)


# --------------------------------------------------------------------------
# IP allowlist
# --------------------------------------------------------------------------

def _patch_allowlist(monkeypatch, value: str):
    monkeypatch.setattr(
        security_ext, "config", SimpleNamespace(admin_ip_allowlist=value, business_name="X")
    )


def test_ip_allowed_empty_list_allows_all(monkeypatch):
    _patch_allowlist(monkeypatch, "")
    assert security_ext.ip_allowed("8.8.8.8") is True
    assert security_ext.ip_allowed("10.0.0.1") is True


def test_ip_allowed_explicit_list_excludes_others(monkeypatch):
    _patch_allowlist(monkeypatch, "203.0.113.10, 198.51.100.5")
    assert security_ext.ip_allowed("203.0.113.10") is True
    assert security_ext.ip_allowed("198.51.100.5") is True
    assert security_ext.ip_allowed("8.8.8.8") is False


def test_ip_allowed_cidr_match(monkeypatch):
    _patch_allowlist(monkeypatch, "10.0.0.0/24")
    assert security_ext.ip_allowed("10.0.0.55") is True
    assert security_ext.ip_allowed("10.0.1.55") is False


def test_ip_allowed_malformed_ip_fails_open(monkeypatch):
    _patch_allowlist(monkeypatch, "10.0.0.0/24")
    # A malformed client IP fails open (allowed) rather than locking out.
    assert security_ext.ip_allowed("not-an-ip") is True


def test_ip_allowed_all_malformed_entries_allows_all(monkeypatch):
    _patch_allowlist(monkeypatch, "garbage, also-bad")
    assert security_ext.ip_allowed("8.8.8.8") is True


# --------------------------------------------------------------------------
# TOTP
# --------------------------------------------------------------------------

def test_generate_and_verify_totp():
    secret = security_ext.generate_totp_secret()
    assert isinstance(secret, str) and len(secret) >= 16

    totp = pyotp.TOTP(secret)
    valid_code = totp.now()
    assert security_ext.verify_totp(secret, valid_code) is True


def test_verify_totp_rejects_wrong_code():
    secret = security_ext.generate_totp_secret()
    totp = pyotp.TOTP(secret)
    # Build the set of codes accepted within the +/-1 window and pick one outside.
    accepted = {totp.now()}
    import time

    now = int(time.time())
    for step in (-1, 1):
        accepted.add(totp.at(now + step * 30))
    wrong = "000000"
    while wrong in accepted:
        wrong = f"{(int(wrong) + 1) % 1000000:06d}"
    assert security_ext.verify_totp(secret, wrong) is False


def test_verify_totp_empty_inputs_false():
    assert security_ext.verify_totp("", "123456") is False
    assert security_ext.verify_totp("ABCDEF", "") is False


def test_provisioning_uri_and_qr():
    secret = security_ext.generate_totp_secret()
    uri = security_ext.provisioning_uri("alice", secret)
    assert uri.startswith("otpauth://totp/")
    assert secret in uri
    data_uri = security_ext.qr_data_uri(uri)
    assert data_uri.startswith("data:image/png;base64,")
