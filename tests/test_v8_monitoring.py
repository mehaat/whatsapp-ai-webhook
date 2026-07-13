"""
tests/test_v8_monitoring.py
---------------------------
v8.0 monitoring config tests. These validate the Prometheus + Grafana
provisioning artifacts under ``deploy/`` without needing the stack running:

- The Grafana dashboard JSON is well-formed, has enough panels, and every
  panel targets a ``mehaat_`` metric.
- The Prometheus config and both Grafana provisioning YAMLs parse cleanly and
  the scrape job is wired to the app's ``/metrics`` endpoint.
"""

from __future__ import annotations

import json
import os

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DASHBOARD = os.path.join(_ROOT, "deploy", "grafana", "dashboards", "mehaat-overview.json")
PROMETHEUS = os.path.join(_ROOT, "deploy", "prometheus.yml")
DATASOURCE = os.path.join(
    _ROOT, "deploy", "grafana", "provisioning", "datasources", "datasource.yml"
)
DASHBOARDS_PROVIDER = os.path.join(
    _ROOT, "deploy", "grafana", "provisioning", "dashboards", "dashboards.yml"
)


def _iter_exprs(panel):
    for target in panel.get("targets", []):
        expr = target.get("expr")
        if expr:
            yield expr


def test_dashboard_is_valid_json_with_panels():
    with open(DASHBOARD, encoding="utf-8") as fh:
        dashboard = json.load(fh)

    panels = dashboard.get("panels", [])
    assert len(panels) >= 6, "dashboard should have at least 6 panels"

    for panel in panels:
        targets = panel.get("targets")
        assert isinstance(targets, list) and targets, (
            f"panel {panel.get('title')!r} must have a non-empty targets list"
        )
        exprs = list(_iter_exprs(panel))
        assert exprs, f"panel {panel.get('title')!r} must have exprs"
        assert any("mehaat_" in expr for expr in exprs), (
            f"panel {panel.get('title')!r} must reference a mehaat_ metric"
        )


def test_dashboard_has_valid_datasource_and_gridpos():
    with open(DASHBOARD, encoding="utf-8") as fh:
        dashboard = json.load(fh)

    assert dashboard.get("schemaVersion", 0) >= 36
    for panel in dashboard["panels"]:
        assert panel.get("datasource"), f"panel {panel.get('title')!r} needs a datasource"
        grid = panel.get("gridPos", {})
        assert {"h", "w", "x", "y"} <= set(grid), "panel needs a full gridPos"


def test_prometheus_config_scrapes_mehaat_metrics():
    with open(PROMETHEUS, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    assert cfg["global"]["scrape_interval"] == "15s"

    scrape_configs = cfg.get("scrape_configs")
    assert isinstance(scrape_configs, list) and scrape_configs

    jobs = {job["job_name"]: job for job in scrape_configs}
    assert "mehaat" in jobs, "expected a scrape job named 'mehaat'"
    assert jobs["mehaat"]["metrics_path"] == "/metrics"

    targets = jobs["mehaat"]["static_configs"][0]["targets"]
    assert "app:5000" in targets


def test_grafana_datasource_yaml_parses():
    with open(DATASOURCE, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    ds = cfg["datasources"][0]
    assert ds["name"] == "Prometheus"
    assert ds["type"] == "prometheus"
    assert ds["url"] == "http://prometheus:9090"
    assert ds["isDefault"] is True


def test_grafana_dashboards_provider_yaml_parses():
    with open(DASHBOARDS_PROVIDER, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    provider = cfg["providers"][0]
    assert provider["type"] == "file"
    assert provider["options"]["path"] == "/var/lib/grafana/dashboards"
