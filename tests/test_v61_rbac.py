"""
tests/test_v61_rbac.py
-----------------------
Tests for the v6.1 multi-user admin RBAC layer (``admin/rbac.py``).

No network / no mocks: the commerce DB (which includes the ``admin_users``
table via ``Base.metadata``) is bootstrapped through :func:`commerce.bootstrap`,
then the user-management service and role helpers are exercised directly.
"""

from __future__ import annotations

import os
import uuid

# Ensure a self-contained SQLite database for the test run (config reads this at
# import time, so it must be set before any project module is imported).
os.environ.setdefault("DATABASE_URL", "sqlite:///test_v61_rbac.db")

import pytest  # noqa: E402

import commerce  # noqa: E402
from admin import rbac  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _bootstrap() -> None:
    """Ensure the commerce + RBAC schema exists once for the module."""
    commerce.bootstrap()


def _unique_username(prefix: str = "user") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def test_role_at_least_ordering() -> None:
    """Role hierarchy compares by privilege rank."""
    assert rbac.role_at_least("admin", "manager") is True
    assert rbac.role_at_least("manager", "admin") is False
    assert rbac.role_at_least("owner", "owner") is True
    # Unknown roles are never "at least" anything.
    assert rbac.role_at_least("wizard", "viewer") is False
    assert rbac.role_at_least("viewer", "wizard") is False


def test_create_and_verify_login() -> None:
    """A created active user verifies with the correct password."""
    username = _unique_username()
    created = rbac.create_user(username, "s3cret-pass", role="manager",
                               full_name="Test Manager", email="t@example.com")
    assert created["username"] == username
    assert created["role"] == "manager"
    assert "password_hash" not in created

    ok = rbac.verify_login(username, "s3cret-pass")
    assert ok is not None
    assert ok["username"] == username
    assert ok["role"] == "manager"
    assert ok["active"] is True


def test_verify_login_wrong_password() -> None:
    """A wrong password returns None."""
    username = _unique_username()
    rbac.create_user(username, "correct-horse")
    assert rbac.verify_login(username, "wrong-password") is None


def test_deactivated_user_cannot_login() -> None:
    """A deactivated user fails verification even with the right password."""
    username = _unique_username()
    created = rbac.create_user(username, "battery-staple")
    assert rbac.verify_login(username, "battery-staple") is not None

    rbac.set_active(created["id"], False, actor="test")
    assert rbac.verify_login(username, "battery-staple") is None


def test_duplicate_username_raises() -> None:
    """Creating a user with an existing username raises ValueError."""
    username = _unique_username()
    rbac.create_user(username, "first-pass")
    with pytest.raises(ValueError):
        rbac.create_user(username, "second-pass")


def test_invalid_role_raises() -> None:
    """An unknown role is rejected at creation time."""
    with pytest.raises(ValueError):
        rbac.create_user(_unique_username(), "pw", role="superuser")
