"""
config.py
---------
Central configuration for ME-HAAT Fashion AI Bot v5

All environment variables are read once here and exposed as a typed
``Config`` object, so the rest of the codebase never calls ``os.environ``
directly. This makes required-variable validation and testing easier.

v5 adds several *optional* settings (WhatsApp catalog id, database, log
format, token encryption, product recommendations). Every new setting has a
safe default, so an existing v4.2 ``.env`` keeps working unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()

APP_VERSION = "5"


def _split_scopes(raw: str) -> List[str]:
    """Parse a comma-separated scopes string into a clean list."""
    return [scope.strip() for scope in raw.split(",") if scope.strip()]


def _as_bool(raw: str, default: bool = False) -> bool:
    """Parse a truthy environment string (1/true/yes/on) into a bool."""
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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

    # Optional: native WhatsApp/Meta Commerce catalog id. When set, the bot
    # attempts native Product Messages before falling back to text cards.
    whatsapp_catalog_id: str = field(
        default_factory=lambda: os.environ.get("WHATSAPP_CATALOG_ID", "")
    )

    # --- Gemini AI ---
    gemini_api_key: str = field(default_factory=lambda: os.environ.get("GEMINI_API_KEY", ""))
    gemini_model: str = field(
        default_factory=lambda: os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    )
    # When True, after sending product cards the bot also sends a short Gemini
    # recommendation. Set to "false" to strictly send only cards (v3.1 Task 1).
    product_reco_enabled: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("PRODUCT_RECO_ENABLED", "true"), True)
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
    # Optional Fernet key used to encrypt access tokens at rest (v4.0). When
    # unset, tokens are stored as-is (unchanged v3.0 behaviour).
    token_encryption_key: str = field(
        default_factory=lambda: os.environ.get("TOKEN_ENCRYPTION_KEY", "")
    )

    # --- Database (optional, opt-in; SQLite by default, Postgres supported) ---
    use_database: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("USE_DATABASE", "false"), False)
    )
    database_url: str = field(
        default_factory=lambda: os.environ.get("DATABASE_URL", "sqlite:///mehaat.db")
    )

    # --- Observability ---
    # "text" (default, v3.0 behaviour) or "json" (structured logs).
    log_format: str = field(
        default_factory=lambda: os.environ.get("LOG_FORMAT", "text").strip().lower()
    )
    log_level: str = field(
        default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    )

    # --- Server ---
    port: int = field(default_factory=lambda: int(os.environ.get("PORT", "5000")))

    @property
    def version(self) -> str:
        """Application version string."""
        return APP_VERSION

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
