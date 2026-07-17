"""
commerce/api_docs.py
--------------------
OpenAPI 3.0.3 specification and Swagger UI for the ME-HAAT Fashion
Commerce API (v6.1).

This module exposes a single Flask blueprint, :data:`api_docs_bp`, with
**absolute** route paths (no ``url_prefix``) so the host application can
register it directly alongside :data:`commerce.api.commerce_api_bp`::

    from commerce.api_docs import api_docs_bp
    app.register_blueprint(api_docs_bp)

Routes
    * ``GET /api/openapi.json`` — the machine-readable OpenAPI 3.0.3 spec
      describing every endpoint of the commerce API.
    * ``GET /api/docs``         — a self-contained HTML page that renders
      Swagger UI (from the unpkg CDN) against the spec above.

The spec is assembled in pure Python and serialized with
:func:`flask.jsonify`; ``info.version`` is sourced dynamically from
:data:`config.APP_VERSION` so the documentation always matches the running
build.
"""

from __future__ import annotations

from typing import Any, Dict

from flask import Blueprint, Response, jsonify

from config import APP_VERSION

api_docs_bp = Blueprint("api_docs", __name__)

#: The OpenAPI dialect this document conforms to.
OPENAPI_VERSION = "3.0.3"

#: URL the Swagger UI page fetches its spec from.
_OPENAPI_JSON_PATH = "/api/openapi.json"

#: Pinned Swagger UI distribution served from the unpkg CDN.
_SWAGGER_UI_VERSION = "5"
_SWAGGER_UI_CSS = (
    f"https://unpkg.com/swagger-ui-dist@{_SWAGGER_UI_VERSION}/swagger-ui.css"
)
_SWAGGER_UI_BUNDLE = (
    f"https://unpkg.com/swagger-ui-dist@{_SWAGGER_UI_VERSION}/swagger-ui-bundle.js"
)


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
    """Schema for a serialized order (mirrors ``_order_to_dict``)."""
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
# Reusable response fragments
# --------------------------------------------------------------------------

def _error_response(description: str) -> Dict[str, Any]:
    """Build a JSON error response body for the given description."""
    return {
        "description": description,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/Error"},
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
                "description": "Exchange admin credentials for a short-lived "
                               "HS256 JWT usable as a Bearer token.",
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
                                    "password": {"type": "string", "format": "password"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Token issued.",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "token": {"type": "string"},
                                        "expires_in": {"type": "integer",
                                                       "description": "Token TTL "
                                                                      "in seconds.",
                                                       "example": 3600},
                                    },
                                }
                            }
                        },
                    },
                    "401": _error_response("Invalid credentials."),
                    "503": _error_response("Token signing unavailable."),
                },
            }
        },
        "/orders": {
            "get": {
                "tags": ["Orders"],
                "summary": "List orders",
                "description": "Return orders matching the supplied filters. "
                               "Requires authentication.",
                "security": [{"bearerAuth": []}, {"apiKeyAuth": []}],
                "parameters": [
                    {"name": "status", "in": "query", "required": False,
                     "schema": {"type": "string"},
                     "description": "Filter by fulfilment status."},
                    {"name": "payment_status", "in": "query", "required": False,
                     "schema": {"type": "string"},
                     "description": "Filter by payment status."},
                    {"name": "q", "in": "query", "required": False,
                     "schema": {"type": "string"},
                     "description": "Free-text search over order_number, "
                                    "wa_number and customer_name."},
                    {"name": "date_from", "in": "query", "required": False,
                     "schema": {"type": "string", "format": "date"},
                     "description": "Inclusive lower bound (YYYY-MM-DD)."},
                    {"name": "date_to", "in": "query", "required": False,
                     "schema": {"type": "string", "format": "date"},
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
                                                                 "the filters."},
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
                    "401": _error_response("Authentication required."),
                    "500": _error_response("Internal error."),
                },
            }
        },
        "/orders/{ref}": {
            "get": {
                "tags": ["Orders"],
                "summary": "Fetch one order",
                "description": "Return a single order (resolved by id or "
                               "order_number) with items and tracking history. "
                               "Requires authentication.",
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
                    "401": _error_response("Authentication required."),
                    "404": _error_response("Order not found."),
                    "500": _error_response("Internal error."),
                },
            }
        },
        "/orders/update": {
            "post": {
                "tags": ["Orders"],
                "summary": "Update an order",
                "description": "Drive a status transition and/or update editable "
                               "fields on an order. Requires authentication.",
                "security": [{"bearerAuth": []}, {"apiKeyAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["order"],
                                "properties": {
                                    "order": {"type": "string",
                                              "description": "Order id or "
                                                             "order_number.",
                                              "example": "MH-2026-0001"},
                                    "status": {"type": "string",
                                               "description": "New fulfilment "
                                                              "status; records a "
                                                              "tracking event.",
                                               "example": "shipped"},
                                    "courier": {"type": "string",
                                                "example": "Delhivery"},
                                    "tracking_number": {"type": "string",
                                                        "example": "DLV123456789"},
                                    "location": {"type": "string",
                                                 "example": "Jaipur Hub"},
                                    "note": {"type": "string",
                                             "example": "Handed to courier"},
                                    "customer_name": {"type": "string"},
                                    "city": {"type": "string"},
                                    "state": {"type": "string"},
                                    "notes": {"type": "string"},
                                    "discount": {"type": "number", "format": "float"},
                                    "shipping": {"type": "number", "format": "float"},
                                    "tax": {"type": "number", "format": "float"},
                                },
                            }
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
                    "400": _error_response("Missing order reference."),
                    "401": _error_response("Authentication required."),
                    "404": _error_response("Order not found."),
                    "500": _error_response("Internal error."),
                },
            }
        },
        "/tracking/{ref}": {
            "get": {
                "tags": ["Tracking"],
                "summary": "Public order tracking",
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
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "order_number": {"type": "string"},
                                        "status": {"type": "string"},
                                        "payment_status": {"type": "string"},
                                        "courier": {"type": "string",
                                                    "nullable": True},
                                        "tracking_number": {"type": "string",
                                                            "nullable": True},
                                        "stages": {
                                            "type": "array",
                                            "description": "Canonical fulfilment "
                                                           "pipeline.",
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "stage": {"type": "string",
                                                              "example": "shipped"},
                                                    "state": {
                                                        "type": "string",
                                                        "enum": ["done", "current",
                                                                 "pending"],
                                                    },
                                                },
                                            },
                                        },
                                        "tracking": {
                                            "type": "array",
                                            "items": {"$ref":
                                                      "#/components/schemas/"
                                                      "TrackingEvent"},
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "404": _error_response("Order not found."),
                    "500": _error_response("Internal error."),
                },
            }
        },
        "/payments/webhook/{provider}": {
            "post": {
                "tags": ["Payments"],
                "summary": "Payment provider webhook",
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
                                        "ok": {"type": "boolean"},
                                        "status": {"type": "string"},
                                    },
                                }
                            }
                        },
                    }
                },
            }
        },
    }


def build_openapi_spec() -> Dict[str, Any]:
    """Construct the complete OpenAPI 3.0.3 document as a Python dict.

    ``info.version`` is sourced from :data:`config.APP_VERSION` so the
    published spec always matches the running build.
    """
    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": "ME-HAAT Fashion Commerce API",
            "version": APP_VERSION,
            "description": "JSON order, tracking and payment-webhook API for the "
                           "ME-HAAT Fashion AI Bot. Protected endpoints accept "
                           "either a Bearer JWT (from POST /api/token) or an "
                           "X-API-Key header; tracking and payment webhooks are "
                           "public.",
        },
        "servers": [{"url": "/"}],
        "tags": [
            {"name": "Auth", "description": "Token issuance."},
            {"name": "Orders", "description": "Order listing and mutation."},
            {"name": "Tracking", "description": "Public order tracking."},
            {"name": "Payments", "description": "Provider webhooks."},
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
                    "description": "Static API key sent in the X-API-Key header.",
                },
            },
            "schemas": {
                "Order": _order_schema(),
                "OrderItem": _order_item_schema(),
                "TrackingEvent": _tracking_event_schema(),
                "Error": _error_schema(),
            },
        },
    }


# --------------------------------------------------------------------------
# Swagger UI HTML
# --------------------------------------------------------------------------

def _swagger_ui_html() -> str:
    """Return a self-contained Swagger UI HTML page bound to the spec."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ME-HAAT Fashion Commerce API</title>
  <link rel="stylesheet" href="{_SWAGGER_UI_CSS}">
  <style>
    body {{ margin: 0; background: #fafafa; }}
  </style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="{_SWAGGER_UI_BUNDLE}" crossorigin></script>
  <script>
    window.onload = function () {{
      window.ui = SwaggerUIBundle({{
        url: "{_OPENAPI_JSON_PATH}",
        dom_id: "#swagger-ui",
        deepLinking: true,
        presets: [
          SwaggerUIBundle.presets.apis,
          SwaggerUIBundle.SwaggerUIStandalonePreset
        ],
        layout: "BaseLayout"
      }});
    }};
  </script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@api_docs_bp.route("/api/openapi.json", methods=["GET"])
def openapi_json() -> Response:
    """Serve the OpenAPI 3.0.3 specification as JSON.

    v9.0 serves the comprehensive spec (all endpoints) when available, falling
    back to the original spec builder.
    """
    try:
        from commerce.openapi_spec import build_full_spec

        return jsonify(build_full_spec())
    except Exception:  # noqa: BLE001
        return jsonify(build_openapi_spec())


@api_docs_bp.route("/api/docs", methods=["GET"])
def api_docs() -> Response:
    """Serve the Swagger UI documentation page."""
    return Response(_swagger_ui_html(), mimetype="text/html")
