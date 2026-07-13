"""
tests/test_v8_tenancy.py
-------------------------
Tests for the v8.0 multi-store / multi-tenant layer (:mod:`commerce.tenancy`).

No network / no mocks: the commerce DB (which includes the ``tenants`` table
via ``Base.metadata``) is bootstrapped through :func:`commerce.bootstrap`, then
the tenancy service is exercised directly. ``config.multi_tenant_enabled`` is
toggled per-test by swapping the module-level ``tenancy.config`` reference for a
lightweight :class:`types.SimpleNamespace`.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

# Self-contained SQLite DB for the run (config reads DATABASE_URL at import time,
# so it must be set before any project module is imported).
os.environ.setdefault("DATABASE_URL", "sqlite:///test_v8_tenancy.db")

import pytest  # noqa: E402

import commerce  # noqa: E402
from commerce import tenancy  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _bootstrap() -> None:
    """Ensure the commerce schema (incl. tenants) exists once for the module."""
    commerce.bootstrap()


@pytest.fixture()
def restore_config():
    """Restore the real ``tenancy.config`` after a test swaps it out."""
    original = tenancy.config
    yield
    tenancy.config = original
    tenancy.clear_current_tenant()


def _enable(multi: bool) -> None:
    """Swap in a fake config with a chosen multi-tenant flag."""
    tenancy.config = SimpleNamespace(
        multi_tenant_enabled=multi, default_tenant_slug="default"
    )


# --------------------------------------------------------------------------
# ensure_default_tenant
# --------------------------------------------------------------------------

def test_ensure_default_tenant_idempotent(restore_config) -> None:
    """Calling ensure_default_tenant twice yields one row with the same slug."""
    _enable(False)
    first = tenancy.ensure_default_tenant()
    second = tenancy.ensure_default_tenant()

    assert first["slug"] == "default"
    assert second["slug"] == "default"
    assert first["id"] == second["id"]

    defaults = [t for t in tenancy.list_tenants() if t["slug"] == "default"]
    assert len(defaults) == 1


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------

def test_create_and_get_tenant(restore_config) -> None:
    """create_tenant persists a tenant fetchable by both slug and id."""
    _enable(True)
    created = tenancy.create_tenant(
        "acme",
        "Acme Fashion",
        whatsapp_phone_number_id="PN_ACME",
        shopify_domain="acme.myshopify.com",
        config={"theme": "noir"},
    )
    assert created["slug"] == "acme"
    assert created["config"] == {"theme": "noir"}

    by_slug = tenancy.get_tenant("acme")
    by_id = tenancy.get_tenant(created["id"])
    assert by_slug is not None and by_id is not None
    assert by_slug["id"] == created["id"] == by_id["id"]
    assert by_id["whatsapp_phone_number_id"] == "PN_ACME"


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------

def test_resolve_default_when_multi_disabled(restore_config) -> None:
    """With multi-tenant OFF, resolve always returns the default tenant."""
    _enable(False)
    # Ensure a non-default tenant exists so we can prove it is ignored.
    tenancy.create_tenant("othershop", "Other Shop",
                          whatsapp_phone_number_id="PN_OTHER")

    resolved = tenancy.resolve_tenant(
        phone_number_id="PN_OTHER",
        shop_domain="acme.myshopify.com",
        host="anything",
        slug="othershop",
        tenant_id=99999,
    )
    assert resolved is not None
    assert resolved["slug"] == "default"


def test_resolve_by_phone_number_id(restore_config) -> None:
    """With multi-tenant ON, resolve by whatsapp phone id hits the right store."""
    _enable(True)
    tenant = tenancy.create_tenant("wa-store", "WA Store",
                                   whatsapp_phone_number_id="PN_WA_123")

    resolved = tenancy.resolve_tenant(phone_number_id="PN_WA_123")
    assert resolved is not None
    assert resolved["id"] == tenant["id"]
    assert resolved["slug"] == "wa-store"


def test_resolve_unknown_falls_back_to_default(restore_config) -> None:
    """With multi-tenant ON, an unknown phone id falls back to the default."""
    _enable(True)
    resolved = tenancy.resolve_tenant(phone_number_id="PN_DOES_NOT_EXIST")
    assert resolved is not None
    assert resolved["slug"] == "default"


# --------------------------------------------------------------------------
# Current-tenant contextvar roundtrip
# --------------------------------------------------------------------------

def test_current_tenant_roundtrip(restore_config) -> None:
    """set_current_tenant / current_tenant_id roundtrip and clear cleanly."""
    _enable(True)
    assert tenancy.current_tenant() is None
    assert tenancy.current_tenant_id() is None

    tenant = tenancy.create_tenant("ctx-store", "Ctx Store")
    tenancy.set_current_tenant(tenant)
    assert tenancy.current_tenant() == tenant
    assert tenancy.current_tenant_id() == tenant["id"]

    tenancy.clear_current_tenant()
    assert tenancy.current_tenant() is None
    assert tenancy.current_tenant_id() is None
