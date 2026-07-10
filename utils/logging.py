"""
utils/logging.py
-----------------
Structured logging configuration and an execution-time decorator, shared
across the whole application.

v4.0 (backward compatible):
    - Optional structured JSON logs via ``LOG_FORMAT=json`` (default ``text``
      keeps the exact v3.0 line format).
    - A per-request trace id (``bind_request_context`` / ``new_request_id``)
      that is attached to every log record and surfaced in JSON logs.

Secure logging: this module also exposes ``redact`` to prevent secrets
(tokens, API keys) from ever being written to log output.
"""

from __future__ import annotations

import contextvars
import functools
import json
import logging
import time
import uuid
from typing import Callable

_SENSITIVE_SUBSTRINGS = ("token", "secret", "api_key", "apikey", "password", "authorization")

# Per-request trace id, safe across threads and async contexts.
_request_id_var: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "mehaat_request_id", default="-"
)


def new_request_id() -> str:
    """Generate a short, unique request/trace id."""
    return uuid.uuid4().hex[:12]


def bind_request_context(request_id: str) -> None:
    """Bind a request/trace id to the current context (for logging)."""
    _request_id_var.set(request_id or "-")


def clear_request_context() -> None:
    """Reset the request/trace id after a request completes."""
    _request_id_var.set("-")


def current_request_id() -> str:
    """Return the request/trace id bound to the current context."""
    return _request_id_var.get()


class _RequestContextFilter(logging.Filter):
    """Inject the current request id into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get()
        return True


class _JsonFormatter(logging.Formatter):
    """Minimal structured JSON log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _resolve_settings() -> "tuple[str, int]":
    """Resolve (log_format, level) from config, defaulting safely."""
    log_format = "text"
    level = logging.INFO
    try:
        from config import config

        log_format = config.log_format or "text"
        level = getattr(logging, config.log_level, logging.INFO)
    except Exception:  # noqa: BLE001 - config may not be importable extremely early
        pass
    return log_format, level


def configure_logging(name: str = "mehaat_bot") -> logging.Logger:
    """Configure and return a structured logger for the application.

    Args:
        name: Logger name, typically the module or app name.

    Returns:
        A configured ``logging.Logger`` instance.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    log_format, level = _resolve_settings()
    logger.setLevel(level)

    handler = logging.StreamHandler()
    if log_format == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    handler.addFilter(_RequestContextFilter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger


logger = configure_logging()


def redact(value: str, keep: int = 4) -> str:
    """Redact a sensitive string for safe logging, keeping only a short suffix.

    Args:
        value: The sensitive string (token, key, secret).
        keep: Number of trailing characters to keep visible.

    Returns:
        A redacted string like "****ab12".
    """
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]


def is_sensitive_key(key: str) -> bool:
    """Return True if a dict/param key name looks like it holds a secret."""
    lowered = key.lower()
    return any(marker in lowered for marker in _SENSITIVE_SUBSTRINGS)


def log_execution_time(func: Callable) -> Callable:
    """Decorator that logs the execution time of the wrapped function."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info("PERF | %s executed in %.2fms", func.__name__, elapsed_ms)

    return wrapper
