"""
admin/users_routes.py
-----------------------
Admin "Users" area for the v6.1 multi-user RBAC system: a list of dashboard
users plus create / edit / password-reset / delete flows. Every route is
protected by :func:`admin.security.login_required` *and*
:func:`admin.rbac.role_required` at the ``admin`` level (only owners and admins
may manage users).

This is an additive blueprint mounted at ``/admin/users``. It never mutates the
existing ``/admin`` blueprint and shares its session, CSRF token and template
layout (``admin/base.html``). State-changing routes additionally enforce
:func:`admin.security.csrf_protect`.
"""

from __future__ import annotations

from typing import Any, Dict

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from admin import rbac
from admin.rbac import ROLE_ORDER, role_required
from admin.security import (
    clean_query,
    csrf_protect,
    current_user,
    get_csrf_token,
    is_authenticated,
    login_required,
)
from utils.logging import logger

admin_users_bp = Blueprint(
    "admin_users",
    __name__,
    url_prefix="/admin/users",
    template_folder="templates",
)


# --------------------------------------------------------------------------
# Template context (mirrors the admin blueprint's globals)
# --------------------------------------------------------------------------

@admin_users_bp.context_processor
def _inject_globals() -> Dict[str, Any]:
    """Expose the CSRF token + current user to this blueprint's templates."""
    return {
        "csrf_token": get_csrf_token() if is_authenticated() else "",
        "admin_user": current_user(),
        "app_version": _app_version(),
        "nav_active": request.endpoint,
        "roles": ROLE_ORDER,
    }


def _app_version() -> str:
    try:
        from config import config

        return config.version
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------
# List
# --------------------------------------------------------------------------

@admin_users_bp.route("/", methods=["GET"])
@login_required
@role_required("admin")
def users_list() -> Any:
    """Render the table of dashboard users."""
    return render_template(
        "admin/users_list.html",
        users=rbac.list_users(),
        roles=ROLE_ORDER,
        nav_active="admin_users.users_list",
    )


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------

@admin_users_bp.route("/new", methods=["GET", "POST"])
@login_required
@role_required("admin")
@csrf_protect
def user_new() -> Any:
    """Show and process the create-user form."""
    if request.method == "GET":
        return render_template(
            "admin/user_form.html",
            mode="create",
            user=None,
            roles=ROLE_ORDER,
            nav_active="admin_users.users_list",
        )

    username = clean_query(request.form.get("username", ""), 128)
    password = request.form.get("password", "")
    role = clean_query(request.form.get("role", "staff"), 16)
    full_name = clean_query(request.form.get("full_name", ""), 255) or None
    email = clean_query(request.form.get("email", ""), 255) or None

    try:
        user = rbac.create_user(
            username=username,
            password=password,
            role=role,
            full_name=full_name,
            email=email,
            actor=current_user() or "admin",
        )
        flash(f"User {user['username']!r} created.", "success")
        return redirect(url_for("admin_users.users_list"))
    except ValueError as exc:
        flash(str(exc), "error")
    except Exception as exc:  # noqa: BLE001 - never 500 the admin on a form
        logger.error("ADMIN | create user failed: %s", exc)
        flash(f"Could not create user: {exc}", "error")

    # Re-render the form preserving the submitted values.
    return render_template(
        "admin/user_form.html",
        mode="create",
        user={
            "username": username,
            "role": role,
            "full_name": full_name,
            "email": email,
            "active": True,
        },
        roles=ROLE_ORDER,
        nav_active="admin_users.users_list",
    )


# --------------------------------------------------------------------------
# Edit
# --------------------------------------------------------------------------

@admin_users_bp.route("/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("admin")
@csrf_protect
def user_edit(user_id: int) -> Any:
    """Edit a user's role, full name, email and active flag."""
    user = rbac.get_user(user_id)
    if user is None:
        flash("User not found.", "error")
        return redirect(url_for("admin_users.users_list"))

    if request.method == "GET":
        return render_template(
            "admin/user_form.html",
            mode="edit",
            user=user,
            roles=ROLE_ORDER,
            nav_active="admin_users.users_list",
        )

    role = clean_query(request.form.get("role", user["role"]), 16)
    full_name = clean_query(request.form.get("full_name", ""), 255) or None
    email = clean_query(request.form.get("email", ""), 255) or None
    active = request.form.get("active") == "on"
    actor = current_user() or "admin"

    try:
        rbac.set_role(user_id, role, actor=actor)
        rbac.set_active(user_id, active, actor=actor)
        # full_name / email are updated directly via the service layer.
        _update_profile(user_id, full_name, email, actor)
        flash("User updated.", "success")
        return redirect(url_for("admin_users.users_list"))
    except ValueError as exc:
        flash(str(exc), "error")
    except Exception as exc:  # noqa: BLE001
        logger.error("ADMIN | edit user #%s failed: %s", user_id, exc)
        flash(f"Could not update user: {exc}", "error")

    merged = {**user, "role": role, "full_name": full_name,
              "email": email, "active": active}
    return render_template(
        "admin/user_form.html",
        mode="edit",
        user=merged,
        roles=ROLE_ORDER,
        nav_active="admin_users.users_list",
    )


def _update_profile(user_id: int, full_name: Any, email: Any, actor: str) -> None:
    """Persist full_name/email edits via a scoped session (audited)."""
    from database.db import session_scope
    from database.models import AdminUser

    with session_scope() as db:
        user = db.get(AdminUser, user_id)
        if user is None:
            return
        user.full_name = full_name
        user.email = email
    logger.info("ADMIN | user profile updated user_id=%s by=%s", user_id, actor)


# --------------------------------------------------------------------------
# Password reset
# --------------------------------------------------------------------------

@admin_users_bp.route("/<int:user_id>/password", methods=["POST"])
@login_required
@role_required("admin")
@csrf_protect
def user_password(user_id: int) -> Any:
    """Reset a user's password."""
    new_password = request.form.get("password", "")
    try:
        updated = rbac.update_password(
            user_id, new_password, actor=current_user() or "admin"
        )
        if updated is None:
            flash("User not found.", "error")
        else:
            flash(f"Password reset for {updated['username']!r}.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    except Exception as exc:  # noqa: BLE001
        logger.error("ADMIN | password reset #%s failed: %s", user_id, exc)
        flash(f"Could not reset password: {exc}", "error")
    return redirect(url_for("admin_users.users_list"))


# --------------------------------------------------------------------------
# Delete
# --------------------------------------------------------------------------

@admin_users_bp.route("/<int:user_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
@csrf_protect
def user_delete(user_id: int) -> Any:
    """Delete a user."""
    try:
        if rbac.delete_user(user_id, actor=current_user() or "admin"):
            flash("User deleted.", "success")
        else:
            flash("User not found.", "error")
    except Exception as exc:  # noqa: BLE001
        logger.error("ADMIN | delete user #%s failed: %s", user_id, exc)
        flash(f"Could not delete user: {exc}", "error")
    return redirect(url_for("admin_users.users_list"))
