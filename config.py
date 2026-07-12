"""
config.py
---------
Central configuration for ME-HAAT Fashion AI Bot v6.0 Enterprise Commerce Edition

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

APP_VERSION = "6.0"


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

    # Optional (v5.1): Meta App Secret used to verify the X-Hub-Signature-256
    # header on inbound webhook POSTs. When set, unsigned/forged webhook calls
    # are rejected with 403. When unset, verification is skipped (a warning is
    # logged) so existing deployments keep working unchanged.
    whatsapp_app_secret: str = field(
        default_factory=lambda: os.environ.get("WHATSAPP_APP_SECRET", "")
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
        default_factory=lambda: (
            os.environ.get("DEFAULT_SHOP_DOMAIN")
            or os.environ.get("SHOPIFY_DEFAULT_SHOP", "")
        )
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

    # --- v6.0 Enterprise Commerce (additive, on by default) ---
    # The commerce order store (orders/payments/tracking/invoices) persists via
    # the SQLAlchemy layer regardless of USE_DATABASE, because the WhatsApp
    # Commerce flow needs durable orders. Set COMMERCE_ENABLED=false to fully
    # disable the v6 commerce surface and behave exactly like v5.1.
    commerce_enabled: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("COMMERCE_ENABLED", "true"), True)
    )
    order_number_prefix: str = field(
        default_factory=lambda: os.environ.get("ORDER_NUMBER_PREFIX", "MH")
    )
    default_currency: str = field(
        default_factory=lambda: os.environ.get("DEFAULT_CURRENCY", "INR")
    )
    # Estimated delivery window shown in the "order confirmed" message.
    delivery_estimate: str = field(
        default_factory=lambda: os.environ.get("DELIVERY_ESTIMATE", "3-5 Days")
    )
    # Stock validation before creating a Shopify draft order. Default OFF
    # (fail-open) because WhatsApp catalog retailer-id -> Shopify variant mapping
    # is deployment-specific; enable once your catalog ids map to variant ids.
    stock_validation_enabled: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("STOCK_VALIDATION_ENABLED", "false"), False)
    )
    # Auto-create a Shopify draft order when a catalog order arrives (default on).
    auto_draft_order: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("AUTO_DRAFT_ORDER", "true"), True)
    )
    # WhatsApp number (E.164 digits, no +) that receives admin order alerts.
    admin_whatsapp_number: str = field(
        default_factory=lambda: os.environ.get("ADMIN_WHATSAPP_NUMBER", "")
    )

    # --- Business / invoice identity (used on PDF invoices) ---
    business_name: str = field(
        default_factory=lambda: os.environ.get("BUSINESS_NAME", "ME-HAAT Fashion")
    )
    business_address: str = field(
        default_factory=lambda: os.environ.get("BUSINESS_ADDRESS", "")
    )
    business_gstin: str = field(default_factory=lambda: os.environ.get("BUSINESS_GSTIN", ""))
    business_phone: str = field(default_factory=lambda: os.environ.get("BUSINESS_PHONE", ""))
    business_email: str = field(default_factory=lambda: os.environ.get("BUSINESS_EMAIL", ""))
    business_website: str = field(
        default_factory=lambda: os.environ.get("BUSINESS_WEBSITE", "https://mehaatfaishon.com")
    )
    invoice_logo_path: str = field(
        default_factory=lambda: os.environ.get("INVOICE_LOGO_PATH", "")
    )
    invoice_output_dir: str = field(
        default_factory=lambda: os.environ.get("INVOICE_OUTPUT_DIR", "invoices")
    )
    # Default GST rate applied when an order does not carry its own tax figure.
    tax_rate_percent: float = field(
        default_factory=lambda: float(os.environ.get("TAX_RATE_PERCENT", "0") or 0)
    )
    shipping_flat: float = field(
        default_factory=lambda: float(os.environ.get("SHIPPING_FLAT", "0") or 0)
    )

    # --- Payments (provider adapter pattern; manual UPI always works) ---
    # One of: manual_upi | razorpay | stripe | cashfree | phonepe.
    payment_provider: str = field(
        default_factory=lambda: os.environ.get("PAYMENT_PROVIDER", "manual_upi").strip().lower()
    )
    payment_link_expiry_minutes: int = field(
        default_factory=lambda: int(os.environ.get("PAYMENT_LINK_EXPIRY_MINUTES", "1440"))
    )
    upi_vpa: str = field(default_factory=lambda: os.environ.get("UPI_VPA", ""))
    upi_payee_name: str = field(
        default_factory=lambda: os.environ.get("UPI_PAYEE_NAME", "ME-HAAT Fashion")
    )
    razorpay_key_id: str = field(default_factory=lambda: os.environ.get("RAZORPAY_KEY_ID", ""))
    razorpay_key_secret: str = field(
        default_factory=lambda: os.environ.get("RAZORPAY_KEY_SECRET", "")
    )
    razorpay_webhook_secret: str = field(
        default_factory=lambda: os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
    )
    stripe_secret_key: str = field(
        default_factory=lambda: os.environ.get("STRIPE_SECRET_KEY", "")
    )
    stripe_webhook_secret: str = field(
        default_factory=lambda: os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    )
    cashfree_app_id: str = field(default_factory=lambda: os.environ.get("CASHFREE_APP_ID", ""))
    cashfree_secret_key: str = field(
        default_factory=lambda: os.environ.get("CASHFREE_SECRET_KEY", "")
    )
    cashfree_env: str = field(
        default_factory=lambda: os.environ.get("CASHFREE_ENV", "sandbox").strip().lower()
    )
    phonepe_merchant_id: str = field(
        default_factory=lambda: os.environ.get("PHONEPE_MERCHANT_ID", "")
    )
    phonepe_salt_key: str = field(default_factory=lambda: os.environ.get("PHONEPE_SALT_KEY", ""))
    phonepe_salt_index: str = field(
        default_factory=lambda: os.environ.get("PHONEPE_SALT_INDEX", "1")
    )
    phonepe_env: str = field(
        default_factory=lambda: os.environ.get("PHONEPE_ENV", "sandbox").strip().lower()
    )

    # --- API security (JWT for the programmatic order/tracking API) ---
    jwt_secret: str = field(default_factory=lambda: os.environ.get("JWT_SECRET", ""))
    jwt_expiry_minutes: int = field(
        default_factory=lambda: int(os.environ.get("JWT_EXPIRY_MINUTES", "1440"))
    )
    # Optional simple bearer key accepted by the order API in addition to JWT.
    api_key: str = field(default_factory=lambda: os.environ.get("API_KEY", ""))

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
