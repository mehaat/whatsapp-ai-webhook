"""
tests/test_v10_1_observability.py
----------------------------------
Tests for the v10.1 observability hardening:

    * richer (but still cheap) /health report,
    * per-component structured log files, and
    * fail-fast startup validation.

These tests never touch the network (no Gemini / Shopify / WhatsApp calls) and
must not regress the existing v4 health contract.

Run:  pytest -q
"""

from __future__ import annotations

import logging
import os

import pytest


# --------------------------------------------------------------------------
# 1) Richer /health report
# --------------------------------------------------------------------------

def test_build_health_report_has_new_and_legacy_keys():
    from utils.health import build_health_report

    report = build_health_report()

    # Legacy contract preserved.
    assert "version" in report
    assert report["service"] == "ME-HAAT Fashion AI Bot"
    assert "components" in report
    assert "shops_connected" in report
    assert report["status"] == "ok"

    # New richer view present.
    for key in ("database", "oauth", "shopify", "whatsapp", "gemini",
                "dashboard", "conversation_memory"):
        assert key in report, f"missing new health key: {key}"

    # Specific shapes required by the spec.
    assert os.path.isabs(report["database"]["path"])
    assert isinstance(report["oauth"]["token_count"], int)
    assert isinstance(report["whatsapp"]["configured"], bool)
    assert isinstance(report["gemini"]["configured"], bool)
    assert "model" in report["gemini"]
    assert "size_bytes" in report["database"]
    assert "size_mb" in report["database"]


def test_health_probes_never_raise():
    # build_health_report is fully guarded; calling repeatedly must be safe.
    from utils.health import build_health_report, liveness, readiness

    build_health_report()
    live = liveness()
    ready = readiness()
    assert live["status"] == "alive"
    assert "ready" in ready
    assert "components" in ready


# --------------------------------------------------------------------------
# 2) Per-component structured log files
# --------------------------------------------------------------------------

def test_get_component_logger_returns_logger_and_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    from utils.logging import get_component_logger

    clog = get_component_logger("shopify")
    assert isinstance(clog, logging.Logger)
    # Logging through it must not raise.
    clog.info("test shopify component log line")
    clog.warning("another line %s", 42)


def test_get_component_logger_covers_all_components(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    from utils.logging import COMPONENT_LOG_FILES, get_component_logger

    for name in COMPONENT_LOG_FILES:
        lg = get_component_logger(name)
        assert isinstance(lg, logging.Logger)
        lg.info("hello from %s", name)


def test_get_component_logger_survives_readonly_dir(monkeypatch):
    # An unwritable LOG_DIR must degrade to console-only, never crash.
    monkeypatch.setenv("LOG_DIR", "/proc/nonexistent/cannot_write_here")
    from utils.logging import get_component_logger

    lg = get_component_logger("database")
    assert isinstance(lg, logging.Logger)
    lg.info("should not raise even when file handler is unavailable")


def test_singleton_logger_unchanged_and_importable():
    from utils.logging import logger, configure_logging, redact

    assert isinstance(logger, logging.Logger)
    assert logger is configure_logging()  # idempotent singleton
    assert redact("shpat_secrettoken") .endswith("oken")


# --------------------------------------------------------------------------
# 3) Fail-fast startup validation
# --------------------------------------------------------------------------

def test_validate_startup_returns_list_of_issue_dicts():
    from config import validate_startup

    issues = validate_startup()
    assert isinstance(issues, list)
    for issue in issues:
        assert set(("var", "severity", "message")).issubset(issue.keys())
        assert issue["severity"] in ("critical", "warning")


def _config_shim(issues, strict):
    """A stand-in for the frozen Config exposing just what enforce() needs."""
    import types

    return types.SimpleNamespace(
        validate_startup=lambda: issues,
        strict_startup=strict,
    )


def test_enforce_does_not_raise_when_not_strict(monkeypatch):
    import config as config_module

    # strict off + no criticals => must not raise.
    monkeypatch.setattr(config_module, "config", _config_shim([], strict=False))
    result = config_module.enforce_startup_validation()
    assert result == []


def test_enforce_does_not_raise_when_not_strict_even_with_criticals(monkeypatch):
    import config as config_module

    fake_issues = [
        {"var": "GEMINI_API_KEY", "severity": "critical", "message": "missing"},
        {"var": "ADMIN_USERNAME", "severity": "warning", "message": "missing"},
    ]
    monkeypatch.setattr(config_module, "config", _config_shim(fake_issues, strict=False))
    # No SystemExit because strict mode is off (preserves current boot behaviour).
    result = config_module.enforce_startup_validation()
    assert result == fake_issues


def test_enforce_raises_systemexit_when_strict_and_critical(monkeypatch):
    import config as config_module

    fake_issues = [
        {"var": "SHOPIFY_APP_URL", "severity": "critical", "message": "missing"},
    ]
    monkeypatch.setattr(config_module, "config", _config_shim(fake_issues, strict=True))
    with pytest.raises(SystemExit):
        config_module.enforce_startup_validation()


def test_strict_startup_flag_exists_and_is_bool():
    from config import config

    assert isinstance(config.strict_startup, bool)
