"""
commerce/openapi_spec.py
-------------------------
The **comprehensive** OpenAPI 3.0.3 specification for the ME-HAAT Fashion
Commerce API (v9.0 Developer Portal).

This module supersedes the compact spec baked into :mod:`commerce.api_docs`.
It documents *every* HTTP surface an integrator touches:

    * ``POST /api/token``               — issue a bearer JWT (public).
    * ``GET  /orders``                  — list orders (protected).
    * ``GET  /orders/{ref}``            — fetch one order (protected).
    * ``POST /orders/update``           — mutate an order (protected).
    * ``GET  /tracking/{ref}``          — public tracking view.
    * ``POST /payments/webhook/{provider}`` — provider webhook sink (public).
    * ``GET  /developers``              — the developer portal landing page.
    * ``GET  /api/docs``                — the Swagger UI page.
    * ``GET  /api/openapi.json``        — this document.

:func:`build_full_spec` is a **pure function** returning a plain ``dict``; it
imports nothing from Flask so it can be serialized, diffed, snapshot-tested or
served from any framework. ``info.version`` is sourced from
:data:`config.version` so the published spec always matches the running build.
"""

from __future__ import annotations

from typing import Any, Dict

from config import config

#: The OpenAPI dialect this document conforms to.
OPENAPI_VERSION = "3.0.3"

#: Human-visible key prefix illustrating the ``X-API-Key`` shape.
_API_KEY_EXAMPLE = "mh_live_abcd1234_s3cr3t-tail"


# --------------------------------------------------------------------------
# Component schemas
# --------------------------------------------------------------------------

def _order_item_schema() -> Dict[str, Any]:
    """Schema for a single order line item (mirrors ``_item_to_dict``)."""
    return {
        "type": "object",
        "description": "A single line item within an order.",
        "properties": {
            "id": {"type": "integer", "example": 42},
            "product_retailer_id": {"type": "string", "nullable": True,
                                    "example": "SKU-KURTA-001"},
            "product_id": {"type": "string", "nullable": True, "example": "gid://1234"},
            "variant_id": {"type": "string", "nullable": True, "example": "gid://5678"},
            "product_name": {"type": "string", "example": "Hand-block Cotton Kurta"},
            "variant": {"type": "string", "nullable": True, "example": "M / Indigo"},
            "quantity": {"type": "integer", "example": 2},
            "unit_price": {"type": "number", "format": "float", "example": 1299.0},
            "currency": {"type": "string", "example": "INR"},
            "line_total": {"type": "number", "format": "float", "example": 2598.0},
        },
    }


def _tracking_event_schema() -> Dict[str, Any]:
    """Schema for a single tracking history event (mirrors ``_tracking_to_dict``)."""
    return {
        "type": "object",
        "description": "A timestamped fulfilment/tracking event.",
        "properties": {
            "id": {"type": "integer", "example": 7},
            "status": {"type": "string", "example": "shipped"},
            "courier": {"type": "string", "nullable": True, "example": "Delhivery"},
            "tracking_number": {"type": "string", "nullable": True,
                                "example": "DLV123456789"},
            "location": {"type": "string", "nullable": True, "example": "Jaipur Hub"},
            "note": {"type": "string", "nullable": True,
                     "example": "Picked up by courier"},
            "created_at": {"type": "string", "format": "date-time",
                           "nullable": True, "example": "2026-07-13T09:30:00+00:00"},
        },
    }


def _order_schema() -> Dict[str, Any]:
    """Schema for a serialized order (mirrors ``commerce.service._order_to_dict``)."""
    return {
        "type": "object",
        "description": "A customer order with totals, status and (optionally) "
                       "items and tracking history.",
        "properties": {
            "id": {"type": "integer", "example": 1001},
            "order_number": {"type": "string", "example": "MH-2026-0001"},
            "wa_number": {"type": "string", "nullable": True, "example": "919812345678"},
            "customer_name": {"type": "string", "nullable": True, "example": "Aditi Sharma"},
            "language": {"type": "string", "nullable": True, "example": "en"},
            "wa_order_id": {"type": "string", "nullable": True},
            "catalog_id": {"type": "string", "nullable": True},
            "currency": {"type": "string", "example": "INR"},
            "subtotal": {"type": "number", "format": "float", "example": 2598.0},
            "discount": {"type": "number", "format": "float", "example": 0.0},
            "shipping": {"type": "number", "format": "float", "example": 99.0},
            "tax": {"type": "number", "format": "float", "example": 129.9},
            "total_amount": {"type": "number", "format": "float", "example": 2826.9},
            "status": {
                "type": "string",
                "description": "Fulfilment pipeline status.",
                "example": "shipped",
                "enum": [
                    "received", "confirmed", "packed", "shipped",
                    "out_for_delivery", "delivered", "cancelled", "refunded",
                ],
            },
            "payment_status": {
                "type": "string",
                "example": "paid",
                "enum": ["pending", "paid", "failed", "refunded"],
            },
            "fulfillment_status": {"type": "string", "example": "shipped"},
            "shopify_draft_order_id": {"type": "string", "nullable": True},
            "shopify_order_id": {"type": "string", "nullable": True},
            "checkout_url": {"type": "string", "nullable": True, "format": "uri"},
            "invoice_url": {"type": "string", "nullable": True, "format": "uri"},
            "courier": {"type": "string", "nullable": True, "example": "Delhivery"},
            "tracking_number": {"type": "string", "nullable": True,
                                "example": "DLV123456789"},
            "city": {"type": "string", "nullable": True, "example": "Jaipur"},
            "state": {"type": "string", "nullable": True, "example": "Rajasthan"},
            "notes": {"type": "string", "nullable": True},
            "created_at": {"type": "string", "format": "date-time", "nullable": True},
            "updated_at": {"type": "string", "format": "date-time", "nullable": True},
            "items": {
                "type": "array",
                "description": "Present when items are included.",
                "items": {"$ref": "#/components/schemas/OrderItem"},
            },
            "tracking": {
                "type": "array",
                "description": "Present when tracking history is included.",
                "items": {"$ref": "#/components/schemas/TrackingEvent"},
            },
        },
    }


def _tracking_response_schema() -> Dict[str, Any]:
    """Schema for the public tracking view payload."""
    return {
        "type": "object",
        "description": "Public tracking summary for an order: current status, "
                       "the canonical fulfilment pipeline and raw events.",
        "properties": {
            "order_number": {"type": "string", "example": "MH-2026-0001"},
            "status": {"type": "string", "example": "shipped"},
            "payment_status": {"type": "string", "example": "paid"},
            "courier": {"type": "string", "nullable": True, "example": "Delhivery"},
            "tracking_number": {"type": "string", "nullable": True,
                                "example": "DLV123456789"},
            "stages": {
                "type": "array",
                "description": "Canonical fulfilment pipeline annotated with state.",
                "items": {
                    "type": "object",
                    "properties": {
                        "stage": {"type": "string", "example": "shipped"},
                        "state": {
                            "type": "string",
                            "enum": ["done", "current", "pending"],
                            "example": "current",
                        },
                    },
                },
            },
            "tracking": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/TrackingEvent"},
            },
        },
    }


def _token_response_schema() -> Dict[str, Any]:
    """Schema for the ``POST /api/token`` success body."""
    return {
        "type": "object",
        "description": "A freshly minted bearer JWT and its lifetime.",
        "properties": {
            "token": {"type": "string",
                      "description": "HS256 JWT to send as a Bearer token.",
                      "example": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."},
            "expires_in": {"type": "integer",
                           "description": "Token time-to-live in seconds.",
                           "example": 3600},
        },
        "required": ["token", "expires_in"],
    }


def _order_update_request_schema() -> Dict[str, Any]:
    """Schema for the ``POST /orders/update`` request body."""
    return {
        "type": "object",
        "description": "An order mutation: identify the order by id or "
                       "order_number, then optionally drive a status transition "
                       "and/or update editable fields.",
        "required": ["order"],
        "properties": {
            "order": {"type": "string",
                      "description": "Order id (all-digit) or order_number.",
                      "example": "MH-2026-0001"},
            "status": {"type": "string",
                       "description": "New fulfilment status; records a tracking "
                                      "event.",
                       "example": "shipped"},
            "courier": {"type": "string", "example": "Delhivery"},
            "tracking_number": {"type": "string", "example": "DLV123456789"},
            "location": {"type": "string", "example": "Jaipur Hub"},
            "note": {"type": "string", "example": "Handed to courier"},
            "customer_name": {"type": "string", "example": "Aditi Sharma"},
            "city": {"type": "string", "example": "Jaipur"},
            "state": {"type": "string", "example": "Rajasthan"},
            "notes": {"type": "string"},
            "discount": {"type": "number", "format": "float", "example": 0.0},
            "shipping": {"type": "number", "format": "float", "example": 99.0},
            "tax": {"type": "number", "format": "float", "example": 129.9},
        },
    }


def _error_schema() -> Dict[str, Any]:
    """Schema for a JSON error body."""
    return {
        "type": "object",
        "description": "A machine-readable error response.",
        "properties": {
            "error": {"type": "string", "example": "not_found"},
        },
        "required": ["error"],
    }


# --------------------------------------------------------------------------
# Reusable response / parameter fragments
# --------------------------------------------------------------------------

def _error_response(description: str, example: str) -> Dict[str, Any]:
    """Build a JSON error response body for the given description."""
    return {
        "description": description,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/Error"},
                "example": {"error": example},
            }
        },
    }


def _ref_param() -> Dict[str, Any]:
    """The shared ``ref`` path parameter (numeric id or order_number)."""
    return {
        "name": "ref",
        "in": "path",
        "required": True,
        "description": "Order reference: an all-digit primary key or an "
                       "order_number string.",
        "schema": {"type": "string"},
        "example": "MH-2026-0001",
    }


# --------------------------------------------------------------------------
# Path definitions
# --------------------------------------------------------------------------

def _paths() -> Dict[str, Any]:
    """Assemble the OpenAPI ``paths`` object for every endpoint."""
    return {
        "/api/token": {
            "post": {
                "tags": ["Auth"],
                "summary": "Issue a bearer JWT",
                "operationId": "issueToken",
                "description": "Exchange admin credentials for a short-lived HS256 "
                               "JWT usable as a Bearer token. This endpoint is "
                               "public (no prior credential required).",
                "security": [],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["username", "password"],
                                "properties": {
                                    "username": {"type": "string", "example": "admin"},
                                    "password": {"type": "string", "format": "password",
                                                 "example": "s3cr3t"},
                                },
                            },
                            "example": {"username": "admin", "password": "s3cr3t"},
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Token issued.",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/TokenResponse"},
                                "example": {"token": "eyJhbGciOi...", "expires_in": 3600},
                            }
                        },
                    },
                    "401": _error_response("Invalid credentials.", "invalid_credentials"),
                    "500": _error_response("Internal error.", "internal_error"),
                    "503": _error_response("Token signing unavailable.", "token_unavailable"),
                },
            }
        },
        "/orders": {
            "get": {
                "tags": ["Orders"],
                "summary": "List orders",
                "operationId": "listOrders",
                "description": "Return orders matching the supplied filters. "
                               "Requires a Bearer JWT or an X-API-Key.",
                "security": [{"bearerAuth": []}, {"apiKeyAuth": []}],
                "parameters": [
                    {"name": "status", "in": "query", "required": False,
                     "schema": {"type": "string"},
                     "example": "shipped",
                     "description": "Filter by fulfilment status."},
                    {"name": "payment_status", "in": "query", "required": False,
                     "schema": {"type": "string"},
                     "example": "paid",
                     "description": "Filter by payment status."},
                    {"name": "q", "in": "query", "required": False,
                     "schema": {"type": "string"},
                     "description": "Free-text search over order_number, "
                                    "wa_number and customer_name."},
                    {"name": "date_from", "in": "query", "required": False,
                     "schema": {"type": "string", "format": "date"},
                     "example": "2026-07-01",
                     "description": "Inclusive lower bound (YYYY-MM-DD)."},
                    {"name": "date_to", "in": "query", "required": False,
                     "schema": {"type": "string", "format": "date"},
                     "example": "2026-07-31",
                     "description": "Inclusive upper bound (YYYY-MM-DD)."},
                    {"name": "limit", "in": "query", "required": False,
                     "schema": {"type": "integer", "default": 50,
                                "minimum": 1, "maximum": 200},
                     "description": "Max rows to return (default 50, max 200)."},
                    {"name": "offset", "in": "query", "required": False,
                     "schema": {"type": "integer", "default": 0, "minimum": 0},
                     "description": "Rows to skip for pagination."},
                ],
                "responses": {
                    "200": {
                        "description": "A page of orders plus the total count.",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "count": {"type": "integer",
                                                  "description": "Total matching "
                                                                 "the filters.",
                                                  "example": 128},
                                        "results": {
                                            "type": "array",
                                            "items": {"$ref":
                                                      "#/components/schemas/Order"},
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "401": _error_response("Authentication required.", "unauthorized"),
                    "429": _error_response("Rate limit exceeded.", "rate_limited"),
                    "500": _error_response("Internal error.", "internal_error"),
                },
            }
        },
        "/orders/{ref}": {
            "get": {
                "tags": ["Orders"],
                "summary": "Fetch one order",
                "operationId": "getOrder",
                "description": "Return a single order (resolved by id or "
                               "order_number) with items and tracking history. "
                               "Requires a Bearer JWT or an X-API-Key.",
                "security": [{"bearerAuth": []}, {"apiKeyAuth": []}],
                "parameters": [_ref_param()],
                "responses": {
                    "200": {
                        "description": "The order.",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Order"},
                            }
                        },
                    },
                    "401": _error_response("Authentication required.", "unauthorized"),
                    "404": _error_response("Order not found.", "not_found"),
                    "429": _error_response("Rate limit exceeded.", "rate_limited"),
                    "500": _error_response("Internal error.", "internal_error"),
                },
            }
        },
        "/orders/update": {
            "post": {
                "tags": ["Orders"],
                "summary": "Update an order",
                "operationId": "updateOrder",
                "description": "Drive a status transition and/or update editable "
                               "fields on an order. Requires a Bearer JWT or an "
                               "X-API-Key.",
                "security": [{"bearerAuth": []}, {"apiKeyAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref":
                                       "#/components/schemas/OrderUpdateRequest"},
                            "example": {
                                "order": "MH-2026-0001",
                                "status": "shipped",
                                "courier": "Delhivery",
                                "tracking_number": "DLV123456789",
                            },
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "The refreshed order.",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Order"},
                            }
                        },
                    },
                    "400": _error_response("Missing order reference.", "missing_order_ref"),
                    "401": _error_response("Authentication required.", "unauthorized"),
                    "404": _error_response("Order not found.", "not_found"),
                    "429": _error_response("Rate limit exceeded.", "rate_limited"),
                    "500": _error_response("Internal error.", "internal_error"),
                },
            }
        },
        "/tracking/{ref}": {
            "get": {
                "tags": ["Tracking"],
                "summary": "Public order tracking",
                "operationId": "trackOrder",
                "description": "Public (unauthenticated) tracking view: summarized "
                               "status, the canonical stage pipeline and raw "
                               "tracking events.",
                "security": [],
                "parameters": [_ref_param()],
                "responses": {
                    "200": {
                        "description": "The tracking summary.",
                        "content": {
                            "application/json": {
                                "schema": {"$ref":
                                           "#/components/schemas/TrackingResponse"},
                            }
                        },
                    },
                    "404": _error_response("Order not found.", "not_found"),
                    "500": _error_response("Internal error.", "internal_error"),
                },
            }
        },
        "/payments/webhook/{provider}": {
            "post": {
                "tags": ["Payments"],
                "summary": "Payment provider webhook",
                "operationId": "paymentWebhook",
                "description": "Public webhook sink for payment providers. Each "
                               "provider adapter verifies its own signature; the "
                               "endpoint always acks with HTTP 200 to avoid retry "
                               "storms.",
                "security": [],
                "parameters": [
                    {
                        "name": "provider",
                        "in": "path",
                        "required": True,
                        "description": "Payment provider identifier.",
                        "schema": {
                            "type": "string",
                            "enum": ["razorpay", "stripe", "cashfree",
                                     "phonepe", "manual_upi"],
                        },
                        "example": "razorpay",
                    }
                ],
                "requestBody": {
                    "required": False,
                    "description": "Raw provider payload (schema varies by "
                                   "provider).",
                    "content": {"application/json": {"schema": {"type": "object"}}},
                },
                "responses": {
                    "200": {
                        "description": "Acknowledgement (always 200).",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "ok": {"type": "boolean", "example": True},
                                        "status": {"type": "string", "example": "paid"},
                                    },
                                },
                                "example": {"ok": True, "status": "paid"},
                            }
                        },
                    }
                },
            }
        },
        "/developers": {
            "get": {
                "tags": ["Developer"],
                "summary": "Developer portal landing page",
                "operationId": "developerPortal",
                "description": "Human-facing developer portal (HTML): quick start, "
                               "authentication, code samples and endpoint reference.",
                "security": [],
                "responses": {
                    "200": {
                        "description": "The developer portal HTML page.",
                        "content": {"text/html": {"schema": {"type": "string"}}},
                    },
                    "404": {"description": "Developer portal disabled."},
                },
            }
        },
        "/api/docs": {
            "get": {
                "tags": ["Developer"],
                "summary": "Swagger UI",
                "operationId": "swaggerUi",
                "description": "Interactive Swagger UI rendered against "
                               "/api/openapi.json.",
                "security": [],
                "responses": {
                    "200": {
                        "description": "The Swagger UI HTML page.",
                        "content": {"text/html": {"schema": {"type": "string"}}},
                    }
                },
            }
        },
        "/api/openapi.json": {
            "get": {
                "tags": ["Developer"],
                "summary": "OpenAPI specification",
                "operationId": "openapiJson",
                "description": "This machine-readable OpenAPI 3.0.3 document.",
                "security": [],
                "responses": {
                    "200": {
                        "description": "The OpenAPI 3.0.3 document.",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                },
            }
        },
    }


def build_full_spec() -> Dict[str, Any]:
    """Construct the complete OpenAPI 3.0.3 document as a Python dict.

    Pure and side-effect free: no Flask, no I/O. ``info.version`` is sourced
    from :data:`config.version` so the published spec always matches the
    running build.
    """
    try:
        version = config.version
    except Exception:  # noqa: BLE001 - version lookup must never break the spec
        version = "0.0.0"

    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": "ME-HAAT Fashion Commerce API",
            "version": version,
            "description": (
                "JSON order, tracking and payment-webhook API for the ME-HAAT "
                "Fashion AI Bot.\n\n"
                "**Authentication.** Protected endpoints accept either a Bearer "
                "JWT (from `POST /api/token`) or a developer API key sent in the "
                "`X-API-Key` header (keys look like `mh_live_...`). Order "
                "tracking, payment webhooks and token issuance are public.\n\n"
                "**Rate limits.** Each API key carries its own per-minute budget "
                "(default 120 req/min); exceeding it returns `429`."
            ),
            "contact": {"name": "ME-HAAT Developer Support"},
        },
        "servers": [{"url": "/", "description": "This deployment host."}],
        "tags": [
            {"name": "Auth", "description": "Token issuance."},
            {"name": "Orders", "description": "Order listing and mutation."},
            {"name": "Tracking", "description": "Public order tracking."},
            {"name": "Payments", "description": "Provider webhooks."},
            {"name": "Developer", "description": "Portal, docs and spec."},
        ],
        "paths": _paths(),
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                    "description": "HS256 JWT obtained from POST /api/token.",
                },
                "apiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                    "description": "Developer API key sent in the X-API-Key "
                                   f"header, e.g. `{_API_KEY_EXAMPLE}`.",
                },
            },
            "schemas": {
                "Order": _order_schema(),
                "OrderItem": _order_item_schema(),
                "TrackingEvent": _tracking_event_schema(),
                "TrackingResponse": _tracking_response_schema(),
                "Error": _error_schema(),
                "TokenResponse": _token_response_schema(),
                "OrderUpdateRequest": _order_update_request_schema(),
            },
        },
    }
