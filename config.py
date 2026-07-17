"""
config.py
---------
Central configuration for ME-HAAT Fashion AI Bot v10.1 Stable Edition
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

APP_VERSION = "10.1"


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

    # --- v6.1: background jobs, inventory reservation, RBAC ---
    # Run order side effects (draft order, invoice, notifications) on a
    # background worker pool so the webhook returns fast. Falls back to
    # synchronous execution when disabled.
    jobs_enabled: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("JOBS_ENABLED", "true"), True)
    )
    jobs_workers: int = field(default_factory=lambda: int(os.environ.get("JOBS_WORKERS", "2")))
    jobs_max_attempts: int = field(
        default_factory=lambda: int(os.environ.get("JOBS_MAX_ATTEMPTS", "3"))
    )
    # Maintain a local reservation ledger for ordered quantities. When
    # INVENTORY_SYNC_ENABLED is also true (and the shop grants write_inventory),
    # reservations are mirrored to Shopify inventory levels.
    inventory_reservation_enabled: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("INVENTORY_RESERVATION_ENABLED", "true"), True)
    )
    inventory_sync_enabled: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("INVENTORY_SYNC_ENABLED", "false"), False)
    )
    # Default role assigned to newly created dashboard users.
    default_admin_role: str = field(
        default_factory=lambda: os.environ.get("DEFAULT_ADMIN_ROLE", "staff").strip().lower()
    )

    # --- v7.0 Enterprise ---
    # Commerce depth toggles.
    coupons_enabled: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("COUPONS_ENABLED", "true"), True)
    )
    abandoned_cart_hours: int = field(
        default_factory=lambda: int(os.environ.get("ABANDONED_CART_HOURS", "6"))
    )
    # Shipping courier (adapter pattern; "manual" needs no account).
    shipping_provider: str = field(
        default_factory=lambda: os.environ.get("SHIPPING_PROVIDER", "manual").strip().lower()
    )
    shiprocket_email: str = field(default_factory=lambda: os.environ.get("SHIPROCKET_EMAIL", ""))
    shiprocket_password: str = field(
        default_factory=lambda: os.environ.get("SHIPROCKET_PASSWORD", "")
    )
    delhivery_token: str = field(default_factory=lambda: os.environ.get("DELHIVERY_TOKEN", ""))
    pickup_pincode: str = field(default_factory=lambda: os.environ.get("PICKUP_PINCODE", ""))
    # Low-stock threshold for restock alerts.
    low_stock_threshold: int = field(
        default_factory=lambda: int(os.environ.get("LOW_STOCK_THRESHOLD", "3"))
    )
    # Admin security: 2FA + IP allowlist (comma-separated CIDRs/IPs; empty = allow all).
    admin_2fa_enabled: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("ADMIN_2FA_ENABLED", "false"), False)
    )
    admin_ip_allowlist: str = field(
        default_factory=lambda: os.environ.get("ADMIN_IP_ALLOWLIST", "")
    )
    # Observability.
    sentry_dsn: str = field(default_factory=lambda: os.environ.get("SENTRY_DSN", ""))
    metrics_enabled: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("METRICS_ENABLED", "true"), True)
    )
    # Optional Redis URL to back the job queue / rate limits (future-proofing).
    redis_url: str = field(default_factory=lambda: os.environ.get("REDIS_URL", ""))

    # --- v8.0 Enterprise scale ---
    # Background queue backend: "inprocess" (default threads) or "celery".
    # Celery requires REDIS_URL (or CELERY_BROKER_URL) to be set.
    queue_backend: str = field(
        default_factory=lambda: os.environ.get("QUEUE_BACKEND", "inprocess").strip().lower()
    )
    celery_broker_url: str = field(
        default_factory=lambda: os.environ.get("CELERY_BROKER_URL", "")
        or os.environ.get("REDIS_URL", "")
    )
    celery_result_backend: str = field(
        default_factory=lambda: os.environ.get("CELERY_RESULT_BACKEND", "")
        or os.environ.get("REDIS_URL", "")
    )
    # Multi-tenant. OFF by default => single implicit "default" tenant, so
    # existing single-store deployments behave exactly as before.
    multi_tenant_enabled: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("MULTI_TENANT_ENABLED", "false"), False)
    )
    default_tenant_slug: str = field(
        default_factory=lambda: os.environ.get("DEFAULT_TENANT_SLUG", "default")
    )
    # Developer portal (API key management + Swagger).
    developer_portal_enabled: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("DEVELOPER_PORTAL_ENABLED", "true"), True)
    )
    # Compliance: data retention (0 = keep forever) + PII export dir.
    data_retention_days: int = field(
        default_factory=lambda: int(os.environ.get("DATA_RETENTION_DAYS", "0"))
    )
    compliance_export_dir: str = field(
        default_factory=lambda: os.environ.get("COMPLIANCE_EXPORT_DIR", "exports")
    )

    # --- v9.0 caching / HA ---
    # Redis-backed cache + rate limiting (falls back to in-process when no Redis).
    cache_enabled: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("CACHE_ENABLED", "true"), True)
    )
    cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.environ.get("CACHE_TTL_SECONDS", "300"))
    )
    # Comma-separated "host:port" Sentinel endpoints for Redis HA (optional).
    redis_sentinels: str = field(default_factory=lambda: os.environ.get("REDIS_SENTINELS", ""))
    redis_sentinel_master: str = field(
        default_factory=lambda: os.environ.get("REDIS_SENTINEL_MASTER", "mymaster")
    )
    redis_cluster: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("REDIS_CLUSTER", "false"), False)
    )

    # --- v9.0 Sentry (expanded) ---
    sentry_environment: str = field(
        default_factory=lambda: os.environ.get("SENTRY_ENVIRONMENT", "production")
    )
    sentry_traces_sample_rate: float = field(
        default_factory=lambda: float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1") or 0.1)
    )

    # --- v9.0 Advanced AI Commerce ---
    recommendations_enabled: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("RECOMMENDATIONS_ENABLED", "true"), True)
    )
    visual_search_enabled: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("VISUAL_SEARCH_ENABLED", "true"), True)
    )
    ai_stylist_enabled: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("AI_STYLIST_ENABLED", "true"), True)
    )
    # Visual embedder backend: "histogram" (offline, always works) or "gemini".
    visual_embedder: str = field(
        default_factory=lambda: os.environ.get("VISUAL_EMBEDDER", "histogram").strip().lower()
    )
    gemini_vision_model: str = field(
        default_factory=lambda: os.environ.get("GEMINI_VISION_MODEL", "gemini-2.5-flash")
    )

    # v10.1: development mode surfaces full tracebacks for tracking/DB failures
    # (logger.exception) instead of terse production logging. Enable with
    # DEV_MODE=true or FLASK_ENV=development.
    dev_mode: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("DEV_MODE", ""), False)
        or os.environ.get("FLASK_ENV", "").strip().lower() == "development"
    )

    # --- v10.0 AI agents / orchestration ---
    # Master switch for the multi-agent orchestrator (API + admin console).
    agents_enabled: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("AGENTS_ENABLED", "true"), True)
    )
    # Route inbound WhatsApp text through the orchestrator. OFF by default so the
    # existing message pipeline is unchanged; turn on to let agents drive chat.
    agents_whatsapp: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("AGENTS_WHATSAPP", "false"), False)
    )
    # RAG knowledge base (document-grounded answers).
    rag_enabled: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("RAG_ENABLED", "true"), True)
    )
    rag_top_k: int = field(default_factory=lambda: int(os.environ.get("RAG_TOP_K", "4")))
    # MCP tool server (expose internal tools via Model Context Protocol).
    mcp_enabled: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("MCP_ENABLED", "true"), True)
    )
    # Voice agent (inbound WhatsApp audio -> transcribe -> orchestrate).
    voice_enabled: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("VOICE_ENABLED", "true"), True)
    )
    # Human approval workflow: sensitive actions above these thresholds require
    # explicit admin approval before execution.
    approval_required: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("APPROVAL_REQUIRED", "true"), True)
    )
    approval_refund_over: float = field(
        default_factory=lambda: float(os.environ.get("APPROVAL_REFUND_OVER", "0") or 0)
    )
    approval_broadcast_over: int = field(
        default_factory=lambda: int(os.environ.get("APPROVAL_BROADCAST_OVER", "50"))
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
    # v10.1: when True, a "critical" startup validation issue aborts boot
    # (SystemExit). Default False preserves the existing boot-with-missing-vars
    # behaviour, so tests and forgiving deployments are unaffected.
    strict_startup: bool = field(
        default_factory=lambda: _as_bool(os.environ.get("STRICT_STARTUP", "false"), False)
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

    def validate_startup(self) -> "List[dict]":
        """Return a list of startup configuration issues (v10.1, additive).

        Each issue is a dict ``{"var", "severity", "message"}`` where severity is
        ``"critical"`` (breaks a core function) or ``"warning"`` (optional but
        recommended). This method NEVER raises and performs no I/O, so it is safe
        to call anywhere; :func:`enforce_startup_validation` decides what to do
        with the result. Nothing is called at import time.
        """
        # (var, present_value, severity, remediation message)
        admin_secret = os.environ.get("ADMIN_SECRET_KEY", "").strip()
        admin_user = os.environ.get("ADMIN_USERNAME", "").strip()
        admin_pass = os.environ.get("ADMIN_PASSWORD", "").strip()
        specs = [
            (
                "SHOPIFY_APP_URL", self.shopify_app_url, "critical",
                "Public app URL is required to build the Shopify OAuth redirect "
                "(install/callback). Set SHOPIFY_APP_URL to your https base URL.",
            ),
            (
                "VERIFY_TOKEN", self.verify_token, "critical",
                "WhatsApp webhook verification will fail without VERIFY_TOKEN. "
                "Set it to the token configured in the Meta webhook settings.",
            ),
            (
                "PHONE_NUMBER_ID", self.phone_number_id, "critical",
                "Outbound WhatsApp sends require PHONE_NUMBER_ID from the Meta "
                "WhatsApp Cloud API dashboard.",
            ),
            (
                "WHATSAPP_TOKEN", self.whatsapp_token, "critical",
                "A WHATSAPP_TOKEN (permanent/system-user token) is required to "
                "call the WhatsApp Cloud API.",
            ),
            (
                "GEMINI_API_KEY", self.gemini_api_key, "critical",
                "GEMINI_API_KEY is required for AI replies; without it the bot "
                "cannot generate responses.",
            ),
            (
                "DATABASE_URL", self.database_url, "warning",
                "DATABASE_URL is unset; falling back to a local sqlite file. Set "
                "it explicitly (and to a persistent path) for durable storage.",
            ),
            (
                "TOKEN_ENCRYPTION_KEY", self.token_encryption_key, "warning",
                "TOKEN_ENCRYPTION_KEY is not set; Shopify access tokens are stored "
                "unencrypted at rest. Provide a Fernet key to encrypt them.",
            ),
            (
                "ADMIN_SECRET_KEY", admin_secret, "critical",
                "ADMIN_SECRET_KEY signs admin dashboard sessions. Set a strong "
                "random value to secure the admin console.",
            ),
            (
                "ADMIN_USERNAME", admin_user, "warning",
                "ADMIN_USERNAME is unset; the admin dashboard will use its built-in "
                "default. Set an explicit username.",
            ),
            (
                "ADMIN_PASSWORD", admin_pass, "critical",
                "ADMIN_PASSWORD is unset; the admin dashboard would be unprotected "
                "or use an insecure default. Set a strong password.",
            ),
        ]
        issues: List[dict] = []
        for var, value, severity, message in specs:
            if not value:
                issues.append({"var": var, "severity": severity, "message": message})
        return issues


config = Config()


def validate_startup() -> "List[dict]":
    """Module-level convenience wrapper over :meth:`Config.validate_startup`."""
    return config.validate_startup()


def enforce_startup_validation() -> "List[dict]":
    """Log startup issues and, in strict mode, fail fast on critical problems.

    Behaviour (v10.1, additive — NOT called at import time):
        * Every issue is logged with a helpful remediation message (critical =>
          error, warning => warning).
        * If ``config.strict_startup`` is True AND at least one "critical" issue
          exists, raises :class:`SystemExit` with a clear combined message.
        * Otherwise it only warns, preserving the current boot-with-missing-vars
          behaviour (and keeping existing tests passing).

    Returns the list of issues (also useful for callers/tests).
    """
    try:
        from utils.logging import logger as _logger
    except Exception:  # noqa: BLE001 - logging must never block startup checks
        import logging as _logging

        _logger = _logging.getLogger("mehaat_bot")

    issues = config.validate_startup()
    criticals = [i for i in issues if i.get("severity") == "critical"]

    for issue in issues:
        line = "STARTUP_VALIDATION | %s | %s: %s"
        if issue.get("severity") == "critical":
            _logger.error(line, issue["severity"].upper(), issue["var"], issue["message"])
        else:
            _logger.warning(line, issue["severity"].upper(), issue["var"], issue["message"])

    if not issues:
        _logger.info("STARTUP_VALIDATION | all checked configuration present")

    if config.strict_startup and criticals:
        names = ", ".join(i["var"] for i in criticals)
        raise SystemExit(
            "Startup aborted (STRICT_STARTUP=true): missing/invalid critical "
            f"configuration: {names}. Fix these environment variables and restart. "
            "See logs above for per-variable remediation."
        )

    if criticals:
        _logger.warning(
            "STARTUP_VALIDATION | %d critical issue(s) present but STRICT_STARTUP is "
            "off; continuing (set STRICT_STARTUP=true to fail fast).", len(criticals)
        )
    return issues
