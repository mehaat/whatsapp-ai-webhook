"""
shopify/client.py
------------------
Base Shopify Admin API HTTP client for ME-HAAT Fashion AI Bot v3.0.

Resolves access tokens dynamically via ``shopify.auth.token_store`` (OAuth)
instead of a single static environment-variable token, and implements
retry-with-backoff + timeout handling for all outbound Admin API calls.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

import requests

from config import config
from shopify.auth import token_store
from utils.logging import log_execution_time, logger


class ShopifyAPIError(Exception):
    """Raised when a Shopify Admin API call fails after all retries."""


class ShopifyClient:
    """Thin, retrying HTTP client for the Shopify Admin REST API.

    A new client instance is created per-shop (per-request) since the
    access token is resolved dynamically from the OAuth token store.
    """

    def __init__(self, shop: str) -> None:
        """Initialize a client bound to a specific shop domain.

        Args:
            shop: The merchant's `*.myshopify.com` domain.

        Raises:
            ShopifyAPIError: If no access token has been installed for this shop.
        """
        self.shop = shop
        access_token = token_store.get_token(shop)
        if not access_token:
            raise ShopifyAPIError(
                f"No Shopify access token found for shop '{shop}'. "
                f"The merchant must complete OAuth via /shopify/install first."
            )
        self.access_token = access_token
        self.api_version = config.shopify_api_version

    @property
    def base_url(self) -> str:
        return f"https://{self.shop}/admin/api/{self.api_version}"

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "X-Shopify-Access-Token": self.access_token,
            "Content-Type": "application/json",
        }

    @log_execution_time
    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Perform an HTTP request against the Shopify Admin API with retries.

        Args:
            method: HTTP method ("GET", "POST", "PUT", "DELETE").
            path: API path relative to the store's Admin API base, e.g. "products.json".
            params: Optional query string parameters.
            json_body: Optional JSON request body.

        Returns:
            Parsed JSON response as a dict, or None on failure.
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_exception: Optional[Exception] = None

        for attempt in range(1, config.max_retries + 1):
            try:
                response = requests.request(
                    method=method,
                    url=url,
                    headers=self.headers,
                    params=params,
                    json=json_body,
                    timeout=config.request_timeout_seconds,
                )
            except requests.exceptions.Timeout as exc:
                last_exception = exc
                logger.warning(
                    "SHOPIFY_CLIENT | Timeout on attempt %d/%d for %s %s",
                    attempt, config.max_retries, method, path,
                )
                self._backoff(attempt)
                continue
            except requests.exceptions.RequestException as exc:
                last_exception = exc
                logger.warning(
                    "SHOPIFY_CLIENT | Request error on attempt %d/%d for %s %s: %s",
                    attempt, config.max_retries, method, path, exc,
                )
                self._backoff(attempt)
                continue

            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", 1))
                logger.warning(
                    "SHOPIFY_CLIENT | Rate limited by Shopify; retrying after %.1fs", retry_after
                )
                time.sleep(retry_after)
                continue

            if response.status_code == 401:
                logger.error(
                    "SHOPIFY_CLIENT | Unauthorized for shop %s; access token may be revoked",
                    self.shop,
                )
                return None

            if 500 <= response.status_code < 600:
                logger.warning(
                    "SHOPIFY_CLIENT | Server error %d on attempt %d/%d for %s %s",
                    response.status_code, attempt, config.max_retries, method, path,
                )
                self._backoff(attempt)
                continue

            if response.status_code >= 400:
                logger.error(
                    "SHOPIFY_CLIENT | Client error %d for %s %s: %s",
                    response.status_code, method, path, response.text[:500],
                )
                return None

            try:
                return response.json() if response.content else {}
            except ValueError:
                logger.error("SHOPIFY_CLIENT | Invalid JSON response for %s %s", method, path)
                return None

        logger.error(
            "SHOPIFY_CLIENT | Exhausted retries for %s %s: %s", method, path, last_exception
        )
        return None

    @staticmethod
    def _backoff(attempt: int) -> None:
        """Exponential backoff sleep between retries."""
        time.sleep(min(2 ** attempt * 0.25, 4.0))

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """Convenience wrapper for a GET request."""
        return self.request("GET", path, params=params)

    def post(self, path: str, json_body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Convenience wrapper for a POST request."""
        return self.request("POST", path, json_body=json_body)


def get_client_for_shop(shop: Optional[str] = None) -> Optional[ShopifyClient]:
    """Resolve a ``ShopifyClient`` for the given shop, or the default shop.

    Args:
        shop: Optional explicit shop domain. If omitted, uses the configured
            default shop or the first installed shop found in the token store.

    Returns:
        A ``ShopifyClient`` instance, or None if no shop/token is available.
    """
    target_shop = shop or token_store.get_default_shop()
    if not target_shop:
        logger.error("SHOPIFY_CLIENT | No shop available (none installed, no default configured)")
        return None

    try:
        return ShopifyClient(target_shop)
    except ShopifyAPIError as exc:
        logger.error("SHOPIFY_CLIENT | %s", exc)
        return None
