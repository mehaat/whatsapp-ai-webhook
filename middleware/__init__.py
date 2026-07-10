"""
middleware
----------
Flask middleware for ME-HAAT Fashion AI Bot v4.0 (additive, backward safe).

    - Assigns a per-request trace id (echoed as the ``X-Request-ID`` response
      header and attached to structured logs).
    - Adds a conservative set of security headers to every response.

None of this changes existing route behaviour or response bodies.
"""

from __future__ import annotations

from .security_headers import register_middleware

__all__ = ["register_middleware"]
