"""
utils/logging.py
-----------------
Structured logging configuration and an execution-time decorator, shared
across the whole application.

Secure logging: this module also exposes ``redact`` to prevent secrets
(tokens, API keys) from ever being written to log output.
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Callable

_SENSITIVE_SUBSTRINGS = ("token", "secret", "api_key", "apikey", "password", "authorization")


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

    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
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
