"""
admin/rbac.py
--------------
Role-based access control (RBAC) for the ME-HAAT Fashion AI Bot v6.1 admin
dashboard: a small role hierarchy, a user-management service backed by the
:class:`~database.models.AdminUser` table, and a ``role_required`` view
decorator that composes with the existing :func:`admin.security.login_required`.

Design notes:
    * Roles form a totally-ordered hierarchy (``ADMIN_ROLES``); a check is
      "role X is at least role Y" by comparing their indices.
    * The built-in environment superuser (``ADMIN_USERNAME``/``ADMIN_PASSWORD``)
      keeps full access: if a session is authenticated (``admin_user`` set) but
      carries no explicit ``admin_role``, it is treated as ``owner``.
    * All database access goes through :func:`database.db.session_scope`; every
      value returned to a caller is a plain detached ``dict`` (never a live ORM
      instance) so callers never touch a closed session.
    * Every mutation is best-effort audited via ``commerce.service.order_service``
      (lazy import; auditing never breaks the operation).

None of this touches the existing WhatsApp / Shopify / AI code paths.
"""

from __future__ import annotations

import functools
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from flask import abort, jsonify, make_response, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from config import config
from database.db import session_scope
from database.models import ADMIN_ROLES, AdminUser
from utils.logging import logger

# Ordered role hierarchy (index 0 = least privileged, last = most privileged).
ROLE_ORDER: List[str] = list(ADMIN_ROLES)

# Session key holding the logged-in user's role (set by the login flow).
_SESSION_ROLE_KEY = "admin_role"
# Session key marking an authenticated session (mirrors admin.security).
_SESSION_USER_KEY = "admin_user"


# --------------------------------------------------------------------------
# Role hierarchy
# --------------------------------------------------------------------------

def role_at_least(role: str, minimum: str) -> bool:
    """Return True when ``role`` is at least as privileged as ``minimum``.

    Comparison is by position in :data:`ROLE_ORDER` (higher index = more
    privilege). An unknown ``role`` or ``minimum`` yields ``False``.

    Args:
        role: The role being checked (e.g. the current user's role).
        minimum: The minimum role required.

    Returns:
        ``True`` if ``role``'s rank is >= ``minimum``'s rank, else ``False``.
    """
    try:
        return ROLE_ORDER.index(role) >= ROLE_ORDER.index(minimum)
    except ValueError:
        return False


# --------------------------------------------------------------------------
# Serialization helpers
# --------------------------------------------------------------------------

def _to_dict(user: AdminUser, *, include_hash: bool = False) -> Dict[str, Any]:
    """Serialize an :class:`AdminUser` to a plain, detached ``dict``.

    Args:
        user: The ORM instance to serialize (read while its session is open).
        include_hash: When ``True``, include ``password_hash`` (internal use).

    Returns:
        A JSON-friendly dict of the user's public columns.
    """
    data: Dict[str, Any] = {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "full_name": user.full_name,
        "email": user.email,
        "active": bool(user.active),
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }
    if include_hash:
        data["password_hash"] = user.password_hash
    return data


def _utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _audit(actor: str, action: str, entity_id: Any, detail: str) -> None:
    """Best-effort audit of a mutation (never raises).

    Uses ``commerce.service.order_service.audit`` (lazy import) so RBAC does
    not create a hard import dependency on the commerce package.

    Args:
        actor: Who performed the action.
        action: Dotted action name, e.g. ``"user.create"``.
        entity_id: The affected user's id.
        detail: Human-readable detail string.
    """
    try:
        from commerce.service import order_service

        order_service.audit(
            actor=actor or "system",
            action=action,
            entity="admin_user",
            entity_id=str(entity_id) if entity_id is not None else None,
            detail=detail,
        )
    except Exception as exc:  # noqa: BLE001 - auditing must never break the op
        logger.debug("RBAC | audit %s failed: %s", action, exc)


def _validate_role(role: str) -> str:
    """Normalize and validate a role name.

    Args:
        role: Candidate role.

    Returns:
        The normalized role string.

    Raises:
        ValueError: If ``role`` is not a known role.
    """
    normalized = (role or "").strip().lower()
    if normalized not in ROLE_ORDER:
        raise ValueError(f"Unknown role: {role!r}")
    return normalized


# --------------------------------------------------------------------------
# User management service
# --------------------------------------------------------------------------

def create_user(
    username: str,
    password: str,
    role: str = "staff",
    full_name: Optional[str] = None,
    email: Optional[str] = None,
    actor: str = "system",
) -> Dict[str, Any]:
    """Create a new dashboard user with a hashed password.

    Args:
        username: Unique login name.
        password: Plaintext password (hashed with pbkdf2 before storage).
        role: One of :data:`ROLE_ORDER` (defaults to ``"staff"``).
        full_name: Optional display name.
        email: Optional email address.
        actor: Who is performing the creation (for auditing).

    Returns:
        The created user as a dict (without the password hash).

    Raises:
        ValueError: If the username/password is empty, the role is invalid, or
            the username already exists.
    """
    uname = (username or "").strip()
    if not uname:
        raise ValueError("Username is required.")
    if not password:
        raise ValueError("Password is required.")
    role = _validate_role(role)

    with session_scope() as db:
        exists = db.query(AdminUser).filter(AdminUser.username == uname).first()
        if exists is not None:
            raise ValueError(f"Username already exists: {uname!r}")
        user = AdminUser(
            username=uname,
            password_hash=generate_password_hash(password, method="pbkdf2:sha256"),
            role=role,
            full_name=(full_name or None),
            email=(email or None),
            active=True,
        )
        db.add(user)
        db.flush()  # populate user.id
        result = _to_dict(user)

    _audit(actor, "user.create", result["id"], f"Created user {uname!r} role={role}")
    logger.info("RBAC | user created username=%s role=%s by=%s", uname, role, actor)
    return result


def verify_login(username: str, password: str) -> Optional[Dict[str, Any]]:
    """Verify a username/password pair against the ``admin_users`` table.

    On success the user's ``last_login_at`` is stamped.

    Args:
        username: Login name.
        password: Plaintext candidate password.

    Returns:
        A dict ``{id, username, role, full_name, active}`` when the user exists,
        is active, and the password matches; otherwise ``None``.
    """
    uname = (username or "").strip()
    if not uname or not password:
        return None

    with session_scope() as db:
        user = db.query(AdminUser).filter(AdminUser.username == uname).first()
        if user is None or not user.active:
            return None
        if not check_password_hash(user.password_hash, password):
            return None
        user.last_login_at = _utcnow()
        db.flush()
        return {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "full_name": user.full_name,
            "active": bool(user.active),
        }


def list_users() -> List[Dict[str, Any]]:
    """Return every dashboard user as a list of dicts (newest first)."""
    with session_scope() as db:
        users = db.query(AdminUser).order_by(AdminUser.id.desc()).all()
        return [_to_dict(u) for u in users]


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    """Return a single user by id, or ``None`` if not found.

    Args:
        user_id: The user's primary key.

    Returns:
        The user dict (without hash) or ``None``.
    """
    with session_scope() as db:
        user = db.get(AdminUser, user_id)
        return _to_dict(user) if user is not None else None


def set_role(user_id: int, role: str, actor: str) -> Optional[Dict[str, Any]]:
    """Change a user's role.

    Args:
        user_id: Target user id.
        role: New role (must be valid).
        actor: Who is performing the change (for auditing).

    Returns:
        The updated user dict, or ``None`` if the user does not exist.

    Raises:
        ValueError: If ``role`` is invalid.
    """
    role = _validate_role(role)
    with session_scope() as db:
        user = db.get(AdminUser, user_id)
        if user is None:
            return None
        user.role = role
        db.flush()
        result = _to_dict(user)

    _audit(actor, "user.role", user_id, f"Set role={role} for user #{user_id}")
    logger.info("RBAC | role set user_id=%s role=%s by=%s", user_id, role, actor)
    return result


def set_active(user_id: int, active: bool, actor: str) -> Optional[Dict[str, Any]]:
    """Activate or deactivate a user.

    Args:
        user_id: Target user id.
        active: New active flag.
        actor: Who is performing the change (for auditing).

    Returns:
        The updated user dict, or ``None`` if the user does not exist.
    """
    with session_scope() as db:
        user = db.get(AdminUser, user_id)
        if user is None:
            return None
        user.active = bool(active)
        db.flush()
        result = _to_dict(user)

    _audit(actor, "user.active", user_id,
           f"Set active={bool(active)} for user #{user_id}")
    logger.info("RBAC | active set user_id=%s active=%s by=%s",
                user_id, bool(active), actor)
    return result


def update_password(user_id: int, new_password: str, actor: str) -> Optional[Dict[str, Any]]:
    """Reset a user's password.

    Args:
        user_id: Target user id.
        new_password: New plaintext password (hashed before storage).
        actor: Who is performing the reset (for auditing).

    Returns:
        The updated user dict, or ``None`` if the user does not exist.

    Raises:
        ValueError: If ``new_password`` is empty.
    """
    if not new_password:
        raise ValueError("Password is required.")
    with session_scope() as db:
        user = db.get(AdminUser, user_id)
        if user is None:
            return None
        user.password_hash = generate_password_hash(new_password, method="pbkdf2:sha256")
        db.flush()
        result = _to_dict(user)

    _audit(actor, "user.password", user_id, f"Reset password for user #{user_id}")
    logger.info("RBAC | password reset user_id=%s by=%s", user_id, actor)
    return result


def delete_user(user_id: int, actor: str) -> bool:
    """Delete a user.

    Args:
        user_id: Target user id.
        actor: Who is performing the deletion (for auditing).

    Returns:
        ``True`` if a user was deleted, ``False`` if none matched.
    """
    with session_scope() as db:
        user = db.get(AdminUser, user_id)
        if user is None:
            return False
        username = user.username
        db.delete(user)

    _audit(actor, "user.delete", user_id, f"Deleted user {username!r} (#{user_id})")
    logger.info("RBAC | user deleted user_id=%s username=%s by=%s",
                user_id, username, actor)
    return True


# --------------------------------------------------------------------------
# Decorator
# --------------------------------------------------------------------------

def _current_role() -> str:
    """Resolve the current session's effective role.

    Reads ``session['admin_role']``. As a safety net for the built-in
    environment superuser, an authenticated session (``admin_user`` set) that
    carries no explicit role is treated as ``"owner"`` so it retains full
    access.

    Returns:
        The effective role string (possibly empty for anonymous callers).
    """
    role = session.get(_SESSION_ROLE_KEY, "")
    if not role and session.get(_SESSION_USER_KEY):
        return "owner"
    return role


def _wants_json() -> bool:
    """Heuristic: does the caller expect a JSON error rather than HTML?"""
    if request.path.startswith("/admin/api/") or request.path.endswith("/api"):
        return True
    xhr = request.headers.get("X-Requested-With", "") == "XMLHttpRequest"
    accept = request.headers.get("Accept", "")
    return xhr or ("application/json" in accept and "text/html" not in accept)


def role_required(minimum: str) -> Callable[[Callable], Callable]:
    """Decorator enforcing a minimum role on a view.

    Intended to compose *after* :func:`admin.security.login_required` (which
    guarantees an authenticated session). The current role is read from
    ``session['admin_role']`` via :func:`_current_role`; if it is not at least
    ``minimum`` the request is aborted with HTTP 403 — a JSON ``{"error":
    "forbidden"}`` body for XHR/API callers, or a plain 403 otherwise.

    Args:
        minimum: The minimum role required (one of :data:`ROLE_ORDER`).

    Returns:
        A view decorator.
    """

    def decorator(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            role = _current_role()
            if not role_at_least(role, minimum):
                logger.warning(
                    "RBAC | forbidden: role=%r < required=%r for %s",
                    role, minimum, request.path,
                )
                if _wants_json():
                    abort(make_response(jsonify({"error": "forbidden"}), 403))
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator
