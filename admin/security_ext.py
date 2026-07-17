"""
admin/security_ext.py
----------------------
v7.0 security hardening building blocks for the Admin Dashboard:

    * Login-event recording + history (``login_events`` table) so operators can
      audit who signed in, from where, and whether the attempt succeeded.
    * An IP allowlist check (plain IPs + CIDR ranges) usable as a gate in the
      login flow. Empty allowlist means "allow all"; malformed configuration
      fails *open* (logged) so a typo never locks everyone out.
    * TOTP (RFC 6238) two-factor authentication helpers backed by ``pyotp`` and
      ``qrcode``: secret generation, provisioning URI + QR data-URL for the
      authenticator app, code verification, and enable/disable persistence on
      :class:`~database.models.AdminUser`.

Every function is defensive: database helpers open their own
:func:`database.db.session_scope` and never raise to the caller (they log and
degrade), so wiring these into the login path can never 500 the login handler.

Nothing here imports or touches the WhatsApp / Shopify / AI code paths.
"""

from __future__ import annotations

import base64
import ipaddress
import io
from typing import Any, Dict, List, Optional

import pyotp
import qrcode

from config import config
from database.db import session_scope
from database.models import AdminUser, LoginEvent
from utils.logging import logger


# ==========================================================================
# Login events (audit trail)
# ==========================================================================

def record_login_event(
    username: str,
    *,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    success: bool = True,
) -> None:
    """Persist a single admin login attempt. Never raises.

    Args:
        username: The username the attempt was made for.
        ip: Best-effort client IP (may be ``None``).
        user_agent: The requesting browser's User-Agent header (capped).
        success: ``True`` for a successful login, ``False`` for a failure.
    """
    try:
        with session_scope() as db:
            db.add(
                LoginEvent(
                    username=(username or "")[:128] or "unknown",
                    ip=(str(ip)[:64] if ip else None),
                    user_agent=(str(user_agent)[:255] if user_agent else None),
                    success=bool(success),
                )
            )
    except Exception as exc:  # noqa: BLE001 - auditing must never break login
        logger.error("SECURITY | record_login_event failed: %s", exc)


def list_login_events(
    username: Optional[str] = None, limit: int = 100, offset: int = 0
) -> List[Dict[str, Any]]:
    """Return recent login events (newest first) as plain dicts. Never raises.

    Args:
        username: Optional filter to a single username.
        limit: Maximum rows to return.
        offset: Row offset for pagination.

    Returns:
        A list of ``{id, username, ip, user_agent, success, created_at}`` dicts.
    """
    try:
        with session_scope() as db:
            q = db.query(LoginEvent)
            if username:
                q = q.filter(LoginEvent.username == username)
            q = q.order_by(LoginEvent.created_at.desc(), LoginEvent.id.desc())
            q = q.limit(max(1, int(limit))).offset(max(0, int(offset)))
            return [
                {
                    "id": e.id,
                    "username": e.username,
                    "ip": e.ip,
                    "user_agent": e.user_agent,
                    "success": bool(e.success),
                    "created_at": e.created_at.isoformat() if e.created_at else None,
                }
                for e in q.all()
            ]
    except Exception as exc:  # noqa: BLE001
        logger.error("SECURITY | list_login_events failed: %s", exc)
        return []


# ==========================================================================
# IP allowlist
# ==========================================================================

def _parse_allowlist(raw: str) -> List[Any]:
    """Parse a comma-separated allowlist into ip_network objects.

    Malformed entries are logged and skipped (fail-open) rather than raising.
    """
    networks: List[Any] = []
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            logger.warning("SECURITY | Ignoring malformed IP allowlist entry: %r", token)
    return networks


def ip_allowed(ip: str) -> bool:
    """Return True if ``ip`` may access the admin, per the configured allowlist.

    Rules:
        * An empty ``ADMIN_IP_ALLOWLIST`` allows every IP (returns ``True``).
        * A non-empty list allows only IPs inside one of its entries; any other
          IP is rejected (returns ``False``).
        * Malformed configuration or a malformed ``ip`` argument fails *open*
          (logged, returns ``True``) so a config typo never locks operators out.

    Args:
        ip: The candidate client IP string.

    Returns:
        Whether the IP is permitted.
    """
    try:
        networks = _parse_allowlist(config.admin_ip_allowlist)
        if not networks:
            return True  # empty (or entirely malformed) list => allow all
        try:
            addr = ipaddress.ip_address((ip or "").strip())
        except ValueError:
            logger.warning("SECURITY | Malformed client IP %r; allowing (fail-open)", ip)
            return True
        return any(addr in net for net in networks)
    except Exception as exc:  # noqa: BLE001 - a gate must never crash the request
        logger.error("SECURITY | ip_allowed check failed (%s); allowing", exc)
        return True


# ==========================================================================
# Two-factor authentication (TOTP)
# ==========================================================================

def generate_totp_secret() -> str:
    """Return a fresh base32 TOTP secret."""
    return pyotp.random_base32()


def provisioning_uri(username: str, secret: str) -> str:
    """Return an ``otpauth://`` provisioning URI for an authenticator app.

    Args:
        username: The account name shown in the authenticator.
        secret: The base32 TOTP secret.
    """
    issuer = config.business_name or "ME-HAAT Admin"
    return pyotp.TOTP(secret).provisioning_uri(name=username or "admin", issuer_name=issuer)


def qr_data_uri(uri: str) -> str:
    """Render a provisioning URI to a PNG ``data:`` URL for inline display.

    Never raises: on any rendering failure an empty string is returned so the
    setup page can fall back to showing the secret as text.
    """
    try:
        img = qrcode.make(uri)
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception as exc:  # noqa: BLE001
        logger.error("SECURITY | qr_data_uri failed: %s", exc)
        return ""


def verify_totp(secret: str, code: str) -> bool:
    """Return True if ``code`` is a currently-valid TOTP for ``secret``.

    A one-step window is allowed on each side to tolerate clock drift. Never
    raises (a malformed secret/code simply yields ``False``).
    """
    if not secret or not code:
        return False
    try:
        return bool(pyotp.TOTP(secret).verify(str(code).strip(), valid_window=1))
    except Exception as exc:  # noqa: BLE001
        logger.error("SECURITY | verify_totp failed: %s", exc)
        return False


def enable_totp(user_id: int, secret: str, actor: str = "admin") -> Dict[str, Any]:
    """Persist and enable a TOTP secret for a dashboard user. Never raises.

    Args:
        user_id: The :class:`AdminUser` id.
        secret: The verified base32 secret to store.
        actor: Who performed the change (for logging).

    Returns:
        ``{"ok": True, "user_id": ...}`` on success, else ``{"error": ...}``.
    """
    try:
        with session_scope() as db:
            user = db.get(AdminUser, user_id)
            if user is None:
                return {"error": "user_not_found"}
            user.totp_secret = secret
            user.totp_enabled = True
        logger.info("SECURITY | 2FA enabled for user #%s by=%s", user_id, actor)
        return {"ok": True, "user_id": user_id, "enabled": True}
    except Exception as exc:  # noqa: BLE001
        logger.error("SECURITY | enable_totp failed for #%s: %s", user_id, exc)
        return {"error": "enable_failed", "detail": str(exc)}


def disable_totp(user_id: int, actor: str = "admin") -> Dict[str, Any]:
    """Disable TOTP and clear the stored secret for a user. Never raises.

    Args:
        user_id: The :class:`AdminUser` id.
        actor: Who performed the change (for logging).

    Returns:
        ``{"ok": True, ...}`` on success, else ``{"error": ...}``.
    """
    try:
        with session_scope() as db:
            user = db.get(AdminUser, user_id)
            if user is None:
                return {"error": "user_not_found"}
            user.totp_secret = None
            user.totp_enabled = False
        logger.info("SECURITY | 2FA disabled for user #%s by=%s", user_id, actor)
        return {"ok": True, "user_id": user_id, "enabled": False}
    except Exception as exc:  # noqa: BLE001
        logger.error("SECURITY | disable_totp failed for #%s: %s", user_id, exc)
        return {"error": "disable_failed", "detail": str(exc)}


def user_totp(user_id: int) -> Optional[Dict[str, Any]]:
    """Return a user's 2FA status ``{enabled, has_secret}`` or ``None``.

    Args:
        user_id: The :class:`AdminUser` id.

    Returns:
        ``{"enabled": bool, "has_secret": bool}`` when the user exists, else
        ``None`` (also ``None`` on any error — never raises).
    """
    try:
        with session_scope() as db:
            user = db.get(AdminUser, user_id)
            if user is None:
                return None
            return {
                "enabled": bool(user.totp_enabled),
                "has_secret": bool(user.totp_secret),
            }
    except Exception as exc:  # noqa: BLE001
        logger.error("SECURITY | user_totp failed for #%s: %s", user_id, exc)
        return None
