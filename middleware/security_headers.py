"""
middleware/security_headers.py
-------------------------------
Registers ``before_request`` / ``after_request`` hooks that add a request
trace id and security headers. Everything here is additive and never alters
response bodies, so existing clients and the WhatsApp/Shopify webhooks are
unaffected.
"""

from __future__ import annotations

from flask import Flask, g, request

from utils.logging import bind_request_context, clear_request_context, new_request_id

# Conservative, API-friendly security headers. We deliberately avoid a strict
# CSP here because the OAuth callback and health endpoints return JSON, not HTML.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-XSS-Protection": "1; mode=block",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


def register_middleware(app: Flask) -> None:
    """Attach request-context + security-header hooks to a Flask app."""

    @app.before_request
    def _assign_request_context() -> None:  # pragma: no cover - exercised via requests
        incoming = request.headers.get("X-Request-ID")
        request_id = incoming or new_request_id()
        g.request_id = request_id
        bind_request_context(request_id)

    @app.after_request
    def _apply_security_headers(response):  # pragma: no cover - exercised via requests
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        request_id = getattr(g, "request_id", "")
        if request_id:
            response.headers.setdefault("X-Request-ID", request_id)
        return response

    @app.teardown_request
    def _clear_request_context(_exc=None) -> None:  # pragma: no cover
        clear_request_context()
