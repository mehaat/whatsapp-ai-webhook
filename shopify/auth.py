"""
shopify/auth.py
----------------
Shopify OAuth 2.0 authentication for ME-HAAT Fashion AI Bot v3.0.

Replaces the legacy static `SHOPIFY_ACCESS_TOKEN` approach with the
standard Shopify App OAuth flow (Dev Dashboard app model):

    1. GET  /shopify/install   -> builds the authorization URL and redirects
                                   the merchant to Shopify's consent screen.
    2. GET  /shopify/callback  -> validates HMAC + state, exchanges the
                                   authorization code for a permanent
                                   access token, and stores it.

Access tokens are persisted via ``ShopTokenStore`` (JSON-file backed by
default). Swap ``ShopTokenStore`` for a database-backed implementation in
a real multi-tenant production deployment — the public interface
(`get_token` / `set_token`) is intentionally small to make that easy.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Optional
from urllib.parse import urlencode

import requests
from flask import Blueprint, jsonify, redirect, request

from config import config
from utils.logging import logger, redact
from utils.security import (
    generate_oauth_state,
    is_valid_shop_domain,
    validate_oauth_state,
    verify_shopify_hmac,
)

shopify_auth_bp = Blueprint("shopify_auth", __name__, url_prefix="/shopify")


# --------------------------------------------------------------------------
# Token storage
# --------------------------------------------------------------------------

class ShopTokenStore:
    """Persistent storage for per-shop Shopify Admin API access tokens.

    Backed by a local JSON file by default. This is adequate for a
    single-instance deployment; for multi-instance / multi-worker
    production deployments, replace this with a database or a shared
    key-value store (e.g. Postgres, Redis) behind the same interface.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        if not os.path.exists(self.path):
            self._write({})

    def _read(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def set_token(self, shop: str, access_token: str, scopes: str = "") -> None:
        """Store (or update) the access token for a shop."""
        with self._lock:
            data = self._read()
            data[shop] = {"access_token": access_token, "scopes": scopes}
            self._write(data)
        logger.info(
            "SHOPIFY_AUTH | Stored access token for %s (token=%s)",
            shop,
            redact(access_token),
        )

    def get_token(self, shop: str) -> Optional[str]:
        """Retrieve the stored access token for a shop, if any."""
        with self._lock:
            data = self._read()
        record = data.get(shop)
        return record.get("access_token") if record else None

    def get_default_shop(self) -> Optional[str]:
        """Return a configured default shop, or the first installed shop found."""
        if config.default_shop_domain:
            return config.default_shop_domain
        with self._lock:
            data = self._read()
        return next(iter(data.keys()), None)

    def list_shops(self) -> list:
        """List all shops that have completed OAuth installation."""
        with self._lock:
            data = self._read()
        return list(data.keys())

    def remove_token(self, shop: str) -> None:
        """Remove a stored token, e.g. on app/uninstalled webhook."""
        with self._lock:
            data = self._read()
            data.pop(shop, None)
            self._write(data)
        logger.info("SHOPIFY_AUTH | Removed access token for %s", shop)


token_store = ShopTokenStore(config.token_store_path)


# --------------------------------------------------------------------------
# OAuth routes
# --------------------------------------------------------------------------

@shopify_auth_bp.route("/install", methods=["GET"])
def install() -> object:
    """Start the OAuth flow: build the Shopify authorization URL and redirect.

    Query params expected:
        shop: the merchant's `*.myshopify.com` domain.
    """
    shop = request.args.get("shop", "").strip().lower()

    if not is_valid_shop_domain(shop):
        logger.warning("SHOPIFY_AUTH | Invalid shop domain on install: %s", shop)
        return jsonify({"error": "Invalid or missing 'shop' parameter"}), 400

    if not config.shopify_api_key or not config.shopify_app_url:
        logger.error("SHOPIFY_AUTH | App not configured (missing API key or app URL)")
        return jsonify({"error": "Shopify app is not configured"}), 500

    state = generate_oauth_state()
    redirect_uri = f"{config.shopify_app_url}/shopify/callback"

    params = {
        "client_id": config.shopify_api_key,
        "scope": ",".join(config.shopify_scopes),
        "redirect_uri": redirect_uri,
        "state": state,
    }
    auth_url = f"https://{shop}/admin/oauth/authorize?{urlencode(params)}"

    logger.info("SHOPIFY_AUTH | Redirecting %s to Shopify consent screen", shop)
    return redirect(auth_url)


@shopify_auth_bp.route("/callback", methods=["GET"])
def callback() -> object:
    """Handle the OAuth callback: validate, exchange code, store token."""
    args = request.args.to_dict()
    shop = args.get("shop", "").strip().lower()
    code = args.get("code", "")
    state = args.get("state", "")

    if not is_valid_shop_domain(shop):
        logger.warning("SHOPIFY_AUTH | Invalid shop domain on callback: %s", shop)
        return jsonify({"error": "Invalid shop"}), 400

    if not validate_oauth_state(state):
        return jsonify({"error": "Invalid or expired OAuth state"}), 403

    if not verify_shopify_hmac(args, config.shopify_api_secret):
        return jsonify({"error": "HMAC validation failed"}), 403

    if not code:
        return jsonify({"error": "Missing authorization code"}), 400

    access_token, granted_scopes = _exchange_code_for_token(shop, code)
    if not access_token:
        return jsonify({"error": "Failed to exchange authorization code"}), 502

    token_store.set_token(shop, access_token, granted_scopes)

    logger.info("SHOPIFY_AUTH | Installation complete for %s", shop)
    return (
        jsonify(
            {
                "status": "success",
                "message": f"ME-HAAT Fashion AI Bot successfully installed on {shop}.",
            }
        ),
        200,
    )


def _exchange_code_for_token(shop: str, code: str) -> tuple:
    """Exchange a temporary authorization code for a permanent access token.

    Args:
        shop: The merchant's myshopify.com domain.
        code: The temporary authorization code from the callback.

    Returns:
        (access_token, scopes) tuple; (None, "") on failure.
    """
    url = f"https://{shop}/admin/oauth/access_token"
    payload = {
        "client_id": config.shopify_api_key,
        "client_secret": config.shopify_api_secret,
        "code": code,
    }

    try:
        response = requests.post(url, json=payload, timeout=config.request_timeout_seconds)
        response.raise_for_status()
    except requests.exceptions.Timeout:
        logger.error("SHOPIFY_AUTH | Token exchange timed out for %s", shop)
        return None, ""
    except requests.exceptions.RequestException as exc:
        logger.error("SHOPIFY_AUTH | Token exchange failed for %s: %s", shop, exc)
        return None, ""

    try:
        data = response.json()
    except ValueError:
        logger.error("SHOPIFY_AUTH | Invalid JSON in token exchange response for %s", shop)
        return None, ""

    access_token = data.get("access_token")
    scopes = data.get("scope", "")
    if not access_token:
        logger.error("SHOPIFY_AUTH | No access_token in exchange response for %s", shop)
        return None, ""

    return access_token, scopes


def build_install_url(shop: str) -> str:
    """Convenience helper to build the `/shopify/install` link for a shop."""
    return f"{config.shopify_app_url}/shopify/install?{urlencode({'shop': shop})}"
