"""
commerce/tenancy.py
--------------------
The v8.0 multi-store / multi-tenant layer.

A *tenant* is a single store/brand in a shared deployment. Every inbound
signal that can identify a store — the WhatsApp ``phone_number_id`` on a
webhook, the Shopify shop domain, an ``X-Tenant`` API header, the request host
or an explicit slug — is resolved to a tenant here. When
``config.multi_tenant_enabled`` is *false* (the default) the whole surface
collapses to a single implicit "default" tenant, so existing single-store
deployments behave exactly as before.

Design rules (mirroring :mod:`commerce.service` / :mod:`admin.rbac`):
    * Every public function returns plain, detached ``dict`` structures (or
      ``None`` / ``list``); callers never hold a live ORM session.
    * Nothing here raises. Failures are logged and degrade to a sensible
      value (``{}``, ``None`` or the default tenant) so tenant resolution can
      never take down a webhook or an admin page.
    * The request-scoped "current tenant" is stored in a
      :class:`contextvars.ContextVar`, so it is correct under threads and
      async without leaking between requests.
"""

from __future__ import annotations

import contextvars
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from config import config
from database.db import session_scope
from database.models import Tenant
from utils.logging import logger

# Request-scoped current tenant (a serialized dict, or None). Declared at module
# scope so every worker thread / async task gets its own isolated view.
_current: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "current_tenant", default=None
)


# --------------------------------------------------------------------------
# Serialization helpers
# --------------------------------------------------------------------------

def _iso(dt: Optional[datetime]) -> Optional[str]:
    """Return an ISO-8601 string for a datetime, or ``None``."""
    return dt.isoformat() if isinstance(dt, datetime) else None


def _load_config(raw: Optional[str]) -> Dict[str, Any]:
    """Parse a tenant's stored JSON config into a dict (never raises)."""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001 - malformed JSON must not break resolution
        return {}


def _to_dict(tenant: Tenant) -> Dict[str, Any]:
    """Serialize a :class:`Tenant` ORM row to a plain, detached ``dict``."""
    return {
        "id": tenant.id,
        "slug": tenant.slug,
        "name": tenant.name,
        "active": bool(tenant.active),
        "shopify_domain": tenant.shopify_domain,
        "whatsapp_phone_number_id": tenant.whatsapp_phone_number_id,
        "catalog_id": tenant.catalog_id,
        "host": tenant.host,
        "config": _load_config(tenant.config),
        "created_at": _iso(tenant.created_at),
        "updated_at": _iso(tenant.updated_at),
    }


def _dump_config(value: Optional[Union[Dict[str, Any], str]]) -> str:
    """Coerce a config value (dict or JSON string) into a JSON string."""
    if value is None:
        return "{}"
    if isinstance(value, str):
        # Already-serialized JSON; validate it round-trips, else wrap empty.
        return value if _load_config(value) or value.strip() in ("{}", "") else "{}"
    try:
        return json.dumps(value)
    except Exception:  # noqa: BLE001
        return "{}"


# --------------------------------------------------------------------------
# Internal default-tenant handling
# --------------------------------------------------------------------------

def _ensure_default(db) -> Tenant:
    """Fetch (creating if missing) the default tenant ORM row within ``db``.

    The default tenant is always kept ``active`` so resolution fallbacks and
    single-store mode always yield a usable tenant.

    Args:
        db: An open SQLAlchemy session.

    Returns:
        The default :class:`Tenant` ORM instance.
    """
    slug = config.default_tenant_slug or "default"
    tenant = db.query(Tenant).filter(Tenant.slug == slug).first()
    if tenant is None:
        tenant = Tenant(slug=slug, name="Default Store", active=True, config="{}")
        db.add(tenant)
        db.flush()
        logger.info("TENANCY | Created default tenant slug=%s", slug)
    elif not tenant.active:
        tenant.active = True
        db.flush()
    return tenant


def ensure_default_tenant() -> Dict[str, Any]:
    """Idempotently create and return the default tenant.

    Called once at startup. Creates a tenant with ``slug`` =
    ``config.default_tenant_slug`` and name ``"Default Store"`` if it does not
    already exist; otherwise returns the existing one. Never raises.

    Returns:
        The default tenant as a dict (``{}`` if the database is unavailable).
    """
    try:
        with session_scope() as db:
            return _to_dict(_ensure_default(db))
    except Exception as exc:  # noqa: BLE001 - startup must never crash on this
        logger.error("TENANCY | ensure_default_tenant failed: %s", exc)
        return {}


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------

def create_tenant(
    slug: str,
    name: str,
    *,
    shopify_domain: Optional[str] = None,
    whatsapp_phone_number_id: Optional[str] = None,
    catalog_id: Optional[str] = None,
    host: Optional[str] = None,
    config: Optional[Union[Dict[str, Any], str]] = None,
    actor: str = "admin",
) -> Dict[str, Any]:
    """Create a new tenant (or return the existing one on a slug clash).

    Args:
        slug: Unique, URL-safe store identifier.
        name: Human-readable store name.
        shopify_domain: The store's ``*.myshopify.com`` domain (optional).
        whatsapp_phone_number_id: WhatsApp Business phone-number id (optional).
        catalog_id: WhatsApp/Meta catalog id (optional).
        host: HTTP host that maps to this tenant (optional).
        config: Per-tenant JSON overrides (dict or JSON string).
        actor: Who is performing the creation (for auditing).

    Returns:
        The created (or pre-existing) tenant as a dict, or ``{}`` on error.
    """
    clean_slug = (slug or "").strip().lower()
    clean_name = (name or "").strip()
    if not clean_slug or not clean_name:
        logger.warning("TENANCY | create_tenant needs slug and name")
        return {}
    try:
        with session_scope() as db:
            existing = db.query(Tenant).filter(Tenant.slug == clean_slug).first()
            if existing is not None:
                logger.info("TENANCY | create_tenant: slug %s exists", clean_slug)
                return _to_dict(existing)
            tenant = Tenant(
                slug=clean_slug,
                name=clean_name,
                active=True,
                shopify_domain=(shopify_domain or None),
                whatsapp_phone_number_id=(whatsapp_phone_number_id or None),
                catalog_id=(catalog_id or None),
                host=(host or None),
                config=_dump_config(config),
            )
            db.add(tenant)
            db.flush()
            result = _to_dict(tenant)
        _audit(actor, "tenant.create", result["id"], f"Created tenant {clean_slug!r}")
        logger.info("TENANCY | tenant created slug=%s by=%s", clean_slug, actor)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("TENANCY | create_tenant failed: %s", exc)
        return {}


def list_tenants(active: Optional[bool] = None) -> List[Dict[str, Any]]:
    """Return tenants as a list of dicts (newest first).

    Args:
        active: When ``True``/``False``, filter by active state; ``None``
            returns all tenants.

    Returns:
        A list of tenant dicts (empty on error).
    """
    try:
        with session_scope() as db:
            q = db.query(Tenant)
            if active is not None:
                q = q.filter(Tenant.active == bool(active))
            rows = q.order_by(Tenant.id.desc()).all()
            return [_to_dict(t) for t in rows]
    except Exception as exc:  # noqa: BLE001
        logger.error("TENANCY | list_tenants failed: %s", exc)
        return []


def get_tenant(id_or_slug: Union[int, str]) -> Optional[Dict[str, Any]]:
    """Return a tenant by primary-key id or by slug.

    Args:
        id_or_slug: An integer id or a string slug.

    Returns:
        The tenant dict, or ``None`` if not found / on error.
    """
    try:
        with session_scope() as db:
            tenant = _lookup(db, id_or_slug)
            return _to_dict(tenant) if tenant is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.error("TENANCY | get_tenant failed: %s", exc)
        return None


def _lookup(db, id_or_slug: Union[int, str]) -> Optional[Tenant]:
    """Resolve an id-or-slug to a :class:`Tenant` ORM row within ``db``."""
    if isinstance(id_or_slug, bool):  # guard: bool is a subclass of int
        return None
    if isinstance(id_or_slug, int):
        return db.get(Tenant, id_or_slug)
    value = str(id_or_slug or "").strip()
    if not value:
        return None
    if value.isdigit():
        found = db.get(Tenant, int(value))
        if found is not None:
            return found
    return db.query(Tenant).filter(Tenant.slug == value.lower()).first()


def update_tenant(tenant_id: int, **fields: Any) -> Optional[Dict[str, Any]]:
    """Update a tenant's mutable fields.

    Only a whitelist of columns may be set: ``name``, ``slug``,
    ``shopify_domain``, ``whatsapp_phone_number_id``, ``catalog_id``, ``host``,
    ``active`` and ``config`` (dict or JSON string).

    Args:
        tenant_id: Target tenant id.
        **fields: Fields to update.

    Returns:
        The updated tenant dict, ``None`` if not found, or ``{}`` on error.
    """
    allowed = {
        "name", "slug", "shopify_domain", "whatsapp_phone_number_id",
        "catalog_id", "host", "active", "config",
    }
    try:
        with session_scope() as db:
            tenant = db.get(Tenant, tenant_id)
            if tenant is None:
                return None
            changed: List[str] = []
            for key, value in fields.items():
                if key not in allowed:
                    continue
                if key == "config":
                    tenant.config = _dump_config(value)
                elif key == "active":
                    tenant.active = bool(value)
                elif key == "slug":
                    tenant.slug = (str(value or "").strip().lower()) or tenant.slug
                elif key == "name":
                    tenant.name = (str(value).strip() if value else tenant.name)
                else:
                    setattr(tenant, key, (value or None))
                changed.append(key)
            db.flush()
            result = _to_dict(tenant)
        _audit("admin", "tenant.update", tenant_id, ",".join(changed))
        logger.info("TENANCY | tenant updated id=%s fields=%s", tenant_id, changed)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("TENANCY | update_tenant failed: %s", exc)
        return {}


def set_active(tenant_id: int, active: bool, actor: str) -> Optional[Dict[str, Any]]:
    """Activate or deactivate a tenant.

    Args:
        tenant_id: Target tenant id.
        active: New active flag.
        actor: Who is performing the change (for auditing).

    Returns:
        The updated tenant dict, ``None`` if not found, or ``{}`` on error.
    """
    try:
        with session_scope() as db:
            tenant = db.get(Tenant, tenant_id)
            if tenant is None:
                return None
            tenant.active = bool(active)
            db.flush()
            result = _to_dict(tenant)
        _audit(actor, "tenant.active", tenant_id, f"active={bool(active)}")
        logger.info("TENANCY | tenant active id=%s active=%s by=%s",
                    tenant_id, bool(active), actor)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("TENANCY | set_active failed: %s", exc)
        return {}


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------

def resolve_tenant(
    *,
    phone_number_id: Optional[str] = None,
    shop_domain: Optional[str] = None,
    host: Optional[str] = None,
    slug: Optional[str] = None,
    tenant_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve an inbound signal to a single active tenant.

    When ``config.multi_tenant_enabled`` is ``False`` this *always* returns the
    default tenant, regardless of the arguments (single-store mode).

    Otherwise it matches active tenants in priority order:
        1. ``tenant_id``
        2. ``whatsapp_phone_number_id`` == ``phone_number_id``
        3. ``shopify_domain`` == ``shop_domain``
        4. ``host`` == ``host``
        5. ``slug`` == ``slug``

    If nothing matches, it falls back to the default tenant.

    Returns:
        The resolved tenant dict, or ``None`` only if the database is
        unavailable.
    """
    try:
        with session_scope() as db:
            if not config.multi_tenant_enabled:
                return _to_dict(_ensure_default(db))

            base = db.query(Tenant).filter(Tenant.active.is_(True))
            match: Optional[Tenant] = None
            if tenant_id is not None:
                match = base.filter(Tenant.id == tenant_id).first()
            if match is None and phone_number_id:
                match = base.filter(
                    Tenant.whatsapp_phone_number_id == phone_number_id
                ).first()
            if match is None and shop_domain:
                match = base.filter(Tenant.shopify_domain == shop_domain).first()
            if match is None and host:
                match = base.filter(Tenant.host == host).first()
            if match is None and slug:
                match = base.filter(Tenant.slug == str(slug).lower()).first()
            if match is None:
                match = _ensure_default(db)
            return _to_dict(match)
    except Exception as exc:  # noqa: BLE001
        logger.error("TENANCY | resolve_tenant failed: %s", exc)
        return None


# --------------------------------------------------------------------------
# Request-scoped current tenant (contextvars)
# --------------------------------------------------------------------------

def set_current_tenant(tenant: Optional[Dict[str, Any]]) -> None:
    """Set (or clear) the request-scoped current tenant."""
    _current.set(tenant)


def current_tenant() -> Optional[Dict[str, Any]]:
    """Return the request-scoped current tenant dict, or ``None``."""
    return _current.get()


def current_tenant_id() -> Optional[int]:
    """Return the current tenant's id, or ``None`` if unset."""
    tenant = _current.get()
    return tenant.get("id") if isinstance(tenant, dict) else None


def clear_current_tenant() -> None:
    """Clear the request-scoped current tenant."""
    _current.set(None)


# --------------------------------------------------------------------------
# Convenience resolvers (webhook / request) that also set the current tenant
# --------------------------------------------------------------------------

def resolve_from_wa_webhook(value: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the tenant for a WhatsApp webhook ``value`` payload.

    Reads ``value["metadata"]["phone_number_id"]``, resolves it to a tenant,
    stores it as the current tenant and returns it.

    Args:
        value: The ``entry[].changes[].value`` object from a WhatsApp webhook.

    Returns:
        The resolved tenant dict (``{}`` if resolution fails entirely).
    """
    phone_number_id = None
    try:
        phone_number_id = (value or {}).get("metadata", {}).get("phone_number_id")
    except Exception:  # noqa: BLE001 - defensive against odd payloads
        phone_number_id = None
    tenant = resolve_tenant(phone_number_id=phone_number_id) or {}
    set_current_tenant(tenant or None)
    return tenant


def resolve_from_request(req: Any) -> Dict[str, Any]:
    """Resolve the tenant for an incoming Flask request.

    Inspects, in order, the ``X-Tenant`` header (a slug), the request host and
    a ``?tenant=`` query parameter (a slug), stores the resolved tenant as the
    current tenant and returns it.

    Args:
        req: A Flask ``request``-like object (``headers``, ``host``, ``args``).

    Returns:
        The resolved tenant dict (``{}`` if resolution fails entirely).
    """
    header_slug = None
    host = None
    query_slug = None
    try:
        header_slug = req.headers.get("X-Tenant") if req is not None else None
    except Exception:  # noqa: BLE001
        header_slug = None
    try:
        host = getattr(req, "host", None)
    except Exception:  # noqa: BLE001
        host = None
    try:
        query_slug = req.args.get("tenant") if req is not None else None
    except Exception:  # noqa: BLE001
        query_slug = None
    tenant = resolve_tenant(
        slug=(header_slug or query_slug or None),
        host=host,
    ) or {}
    set_current_tenant(tenant or None)
    return tenant


# --------------------------------------------------------------------------
# Auditing (best-effort; never raises)
# --------------------------------------------------------------------------

def _audit(actor: str, action: str, entity_id: Any, detail: str) -> None:
    """Best-effort audit of a tenant mutation via the order service."""
    try:
        from commerce.service import order_service

        order_service.audit(
            actor=actor or "system",
            action=action,
            entity="tenant",
            entity_id=str(entity_id) if entity_id is not None else None,
            detail=detail,
        )
    except Exception as exc:  # noqa: BLE001 - auditing must never break the op
        logger.debug("TENANCY | audit %s failed: %s", action, exc)
