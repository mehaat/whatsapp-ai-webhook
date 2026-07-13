"""
commerce/dev_portal.py
-----------------------
Public **Developer Portal** landing page for the ME-HAAT Fashion Commerce API
(v8.0). A single, self-contained HTML page at ``GET /developers`` that orients
integrators: base URL, authentication (Bearer JWT or ``X-API-Key``), a
quick-start curl example, rate-limit note, links to the interactive Swagger UI
(``/api/docs``) and raw spec (``/api/openapi.json``), and a list of the key
endpoints.

The blueprint registers an **absolute** route (no ``url_prefix``) so the host
app can mount it directly alongside :data:`commerce.api_docs.api_docs_bp`::

    from commerce.dev_portal import dev_portal_bp
    app.register_blueprint(dev_portal_bp)

The page is gated on :data:`config.developer_portal_enabled`; when the portal is
disabled the route returns ``404`` so it is effectively invisible.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, abort, render_template

from config import config
from utils.logging import logger

dev_portal_bp = Blueprint("dev_portal", __name__)


@dev_portal_bp.route("/developers", methods=["GET"])
def developers() -> Any:
    """Serve the public developer portal landing page.

    Returns ``404`` when the developer portal is disabled via
    ``config.developer_portal_enabled``.
    """
    if not getattr(config, "developer_portal_enabled", False):
        abort(404)

    version = ""
    try:
        version = config.version
    except Exception as exc:  # noqa: BLE001 - version lookup must not 500 the page
        logger.debug("DEVPORTAL | version lookup failed: %s", exc)

    return render_template("dev_portal.html", version=version)
