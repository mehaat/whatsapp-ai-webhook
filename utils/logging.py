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
import logging.handlers
import os
import threading
import time
import uuid
from typing import Callable, Optional

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


# --------------------------------------------------------------------------- #
# v10.1: per-component structured log files
# --------------------------------------------------------------------------- #
# Each subsystem can obtain a child logger that writes to its own rotating file
# under LOG_DIR, while still propagating to the shared singleton (which also
# fans records into a combined system.log). Everything here is lazy and guarded:
# on a read-only filesystem we silently skip file handlers and keep console
# logging, so importing this module can never fail.

#: Component log files supported out of the box.
COMPONENT_LOG_FILES = (
    "shopify",
    "oauth",
    "dashboard",
    "ai",
    "database",
    "whatsapp",
    "system",
)

_LOG_MAX_BYTES = 2 * 1024 * 1024  # ~2MB per file before rotation
_LOG_BACKUP_COUNT = 3

_component_lock = threading.Lock()
_system_log_attached = False


def _log_dir() -> str:
    """Return the configured log directory (env LOG_DIR, default ``logs/``)."""
    return os.environ.get("LOG_DIR", "logs").strip() or "logs"


def _build_formatter(log_format: str) -> logging.Formatter:
    """Return a formatter matching the existing text/JSON conventions."""
    if log_format == "json":
        return _JsonFormatter()
    return logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _make_rotating_file_handler(filename: str) -> "Optional[logging.Handler]":
    """Build a guarded RotatingFileHandler; return None if the FS is unwritable.

    Never raises: a read-only or missing LOG_DIR yields ``None`` so callers fall
    back to console-only logging instead of crashing.
    """
    try:
        directory = _log_dir()
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, filename)
        log_format, level = _resolve_settings()
        handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
        handler.setLevel(level)
        handler.setFormatter(_build_formatter(log_format))
        handler.addFilter(_RequestContextFilter())
        return handler
    except Exception:  # noqa: BLE001 - never let logging setup crash the app
        return None


def _ensure_system_log_handler() -> None:
    """Attach a combined ``system.log`` rotating handler to the singleton once.

    So every record that reaches the shared ``logger`` (including component
    records that propagate up) also lands in a single ``system.log`` file.
    """
    global _system_log_attached
    if _system_log_attached:
        return
    with _component_lock:
        if _system_log_attached:
            return
        # Guard against double-adding if this runs more than once.
        for existing in logger.handlers:
            if getattr(existing, "_mehaat_system_log", False):
                _system_log_attached = True
                return
        handler = _make_rotating_file_handler("system.log")
        if handler is not None:
            handler._mehaat_system_log = True  # type: ignore[attr-defined]
            logger.addHandler(handler)
        # Mark attached regardless: if the FS is read-only we stay console-only
        # and never retry-thrash on every call.
        _system_log_attached = True


def get_component_logger(name: str) -> logging.Logger:
    """Return a child logger that writes to its own rotating ``<name>.log``.

    Args:
        name: Component name (e.g. ``"shopify"``, ``"oauth"``, ``"ai"``). Unknown
            names are still accepted and get their own file.

    Returns:
        A ``logging.Logger`` whose records also propagate to the shared singleton
        (and thus into the combined ``system.log``). The ``"system"`` component
        maps to the singleton itself. Never raises; on a read-only filesystem the
        logger simply has no file handler and logs to console only.
    """
    normalised = (name or "system").strip().lower() or "system"

    # Always make sure the combined system.log exists on the singleton.
    _ensure_system_log_handler()

    if normalised == "system":
        return logger

    full_name = f"mehaat_bot.{normalised}"
    with _component_lock:
        child = logging.getLogger(full_name)
        if getattr(child, "_mehaat_component_ready", False):
            return child
        _, level = _resolve_settings()
        child.setLevel(level)
        handler = _make_rotating_file_handler(f"{normalised}.log")
        if handler is not None:
            child.addHandler(handler)
        # Propagate to the singleton so records also reach console + system.log.
        child.propagate = True
        child._mehaat_component_ready = True  # type: ignore[attr-defined]
        return child


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
