"""
config.py
---------
Central configuration for ME-HAAT Fashion AI Bot v3.0.

All environment variables are read once here and exposed as a typed
``Config`` object, so the rest of the codebase never calls ``os.environ``
directly. This makes required-variable validation and testing easier.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _split_scopes(raw: str) -> List[str]:
    """Parse a comma-separated scopes string into a clean list."""
    return [scope.strip() for scope in raw.split(",") if scope.strip()]


@dataclass(frozen=True)
class Config:
    """Immutable application configuration loaded from environment variables."""

    # --- WhatsApp Cloud API ---
    verify_token: str = field(default_factory=lambda: os.environ.get("VERIFY_TOKEN", ""))
    whatsapp_token: str = field(default_factory=lambda: os.environ.get("WHATSAPP_TOKEN", ""))
    phone_number_id: str = field(default_factory=lambda: os.environ.get("PHONE_NUMBER_ID", ""))
    whatsapp_api_version: str = field(
        default_factory=lambda: os.environ.get("WHATSAPP_API_VERSION", "v23.0")
    )

    # --- Gemini AI ---
    gemini_api_key: str = field(default_factory=lambda: os.environ.get("GEMINI_API_KEY", ""))
    gemini_model: str = field(
        default_factory=lambda: os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    )

    # --- Shopify OAuth (new Dev Dashboard app model) ---
    shopify_api_key: str = field(default_factory=lambda: os.environ.get("SHOPIFY_API_KEY", ""))
    shopify_api_secret: str = field(
        default_factory=lambda: os.environ.get("SHOPIFY_API_SECRET", "")
    )
    shopify_app_url: str = field(
        default_factory=lambda: os.environ.get("SHOPIFY_APP_URL", "").rstrip("/")
    )
    shopify_scopes: List[str] = field(
        default_factory=lambda: _split_scopes(
            os.environ.get(
                "SHOPIFY_SCOPES",
                "read_products,read_orders,read_inventory,read_customers,write_draft_orders",
            )
        )
    )
    shopify_webhook_secret: str = field(
        default_factory=lambda: os.environ.get("SHOPIFY_WEBHOOK_SECRET", "")
    )
    shopify_api_version: str = field(
        default_factory=lambda: os.environ.get("SHOPIFY_API_VERSION", "2024-10")
    )
    # Optional: for single-store deployments that want to pin a default shop
    # (e.g. the merchant has already installed the app once).
    default_shop_domain: str = field(
        default_factory=lambda: os.environ.get("DEFAULT_SHOP_DOMAIN", "")
    )

    # --- Storage / persistence ---
    token_store_path: str = field(
        default_factory=lambda: os.environ.get("TOKEN_STORE_PATH", "shop_tokens.json")
    )

    # --- Server ---
    port: int = field(default_factory=lambda: int(os.environ.get("PORT", "5000")))

    # --- Networking ---
    request_timeout_seconds: int = field(
        default_factory=lambda: int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "15"))
    )
    max_retries: int = field(default_factory=lambda: int(os.environ.get("MAX_RETRIES", "3")))

    def required_vars_present(self) -> List[str]:
        """Return a list of names of required variables that are missing."""
        required = {
            "VERIFY_TOKEN": self.verify_token,
            "WHATSAPP_TOKEN": self.whatsapp_token,
            "PHONE_NUMBER_ID": self.phone_number_id,
            "GEMINI_API_KEY": self.gemini_api_key,
            "SHOPIFY_API_KEY": self.shopify_api_key,
            "SHOPIFY_API_SECRET": self.shopify_api_secret,
            "SHOPIFY_APP_URL": self.shopify_app_url,
        }
        return [name for name, value in required.items() if not value]


config = Config()
