"""
test_v9_helm.py
---------------
Structural tests for the v9.0 Kubernetes + Helm deployment under
``deploy/helm/mehaat/`` and ``deploy/k8s/``.

These are static/lint-style checks: they do NOT require a cluster, kubectl, or
helm. Helm template files contain Go templating ("{{ ... }}") so they are not
valid plain YAML — for those we only assert string content. Plain files
(Chart.yaml, values.yaml, raw k8s manifests) are parsed with yaml.safe_load.
"""

import os
import glob

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHART_DIR = os.path.join(REPO_ROOT, "deploy", "helm", "mehaat")
TEMPLATES_DIR = os.path.join(CHART_DIR, "templates")
K8S_DIR = os.path.join(REPO_ROOT, "deploy", "k8s")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def test_chart_yaml_valid_and_metadata():
    path = os.path.join(CHART_DIR, "Chart.yaml")
    assert os.path.isfile(path), "Chart.yaml missing"
    data = yaml.safe_load(_read(path))
    assert isinstance(data, dict)
    assert data["name"] == "mehaat"
    assert data["apiVersion"] == "v2"
    assert str(data["appVersion"]) == "9.0"
    assert str(data["version"]) == "0.1.0"


def test_values_yaml_valid_and_keys():
    path = os.path.join(CHART_DIR, "values.yaml")
    assert os.path.isfile(path), "values.yaml missing"
    data = yaml.safe_load(_read(path))
    assert isinstance(data, dict)
    for key in ("image", "autoscaling", "worker", "ingress", "resources"):
        assert key in data, f"values.yaml missing top-level key: {key}"
    # spot-check nested shape
    assert "repository" in data["image"]
    assert "enabled" in data["autoscaling"]
    assert "enabled" in data["worker"]
    assert "enabled" in data["ingress"]


def test_templates_are_go_templated_and_reference_values_or_helper():
    yaml_templates = sorted(glob.glob(os.path.join(TEMPLATES_DIR, "*.yaml")))
    assert yaml_templates, "no .yaml templates found"
    for tpl in yaml_templates:
        text = _read(tpl)
        assert "{{" in text, f"{os.path.basename(tpl)} has no Helm template action"
        assert (
            'include "mehaat.fullname"' in text
            or "mehaat.labels" in text
            or "mehaat.selectorLabels" in text
            or ".Values" in text
        ), f"{os.path.basename(tpl)} references neither a helper nor .Values"


def test_expected_templates_exist_with_expected_kinds():
    expected = {
        "configmap.yaml": "kind: ConfigMap",
        "secret.yaml": "kind: Secret",
        "deployment-web.yaml": "kind: Deployment",
        "deployment-worker.yaml": "kind: Deployment",
        "deployment-beat.yaml": "kind: Deployment",
        "service.yaml": "kind: Service",
        "ingress.yaml": "kind: Ingress",
        "hpa.yaml": "kind: HorizontalPodAutoscaler",
        "pdb.yaml": "kind: PodDisruptionBudget",
        "serviceaccount.yaml": "kind: ServiceAccount",
        "pvc.yaml": "kind: PersistentVolumeClaim",
    }
    for fname, kind in expected.items():
        path = os.path.join(TEMPLATES_DIR, fname)
        assert os.path.isfile(path), f"missing template {fname}"
        text = _read(path)
        assert kind in text, f"{fname} does not declare {kind}"


def test_helpers_and_notes_present():
    assert os.path.isfile(os.path.join(TEMPLATES_DIR, "_helpers.tpl"))
    assert os.path.isfile(os.path.join(TEMPLATES_DIR, "NOTES.txt"))
    helpers = _read(os.path.join(TEMPLATES_DIR, "_helpers.tpl"))
    for helper in ("mehaat.fullname", "mehaat.name", "mehaat.labels", "mehaat.selectorLabels"):
        assert f'define "{helper}"' in helpers, f"helper {helper} not defined"


def test_gated_templates_have_conditionals():
    gates = {
        "deployment-worker.yaml": ".Values.worker.enabled",
        "deployment-beat.yaml": ".Values.beat.enabled",
        "ingress.yaml": ".Values.ingress.enabled",
        "hpa.yaml": ".Values.autoscaling.enabled",
        "pdb.yaml": ".Values.podDisruptionBudget.enabled",
        "serviceaccount.yaml": ".Values.serviceAccount.create",
        "pvc.yaml": ".Values.persistence.enabled",
    }
    for fname, gate in gates.items():
        text = _read(os.path.join(TEMPLATES_DIR, fname))
        assert gate in text, f"{fname} not gated on {gate}"


def test_raw_web_manifest_parses_and_has_deployment():
    path = os.path.join(K8S_DIR, "deployment-web.yaml")
    assert os.path.isfile(path), "raw deployment-web.yaml missing"
    docs = list(yaml.safe_load_all(_read(path)))
    docs = [d for d in docs if d]
    kinds = {d.get("kind") for d in docs}
    assert "Deployment" in kinds, f"no Deployment in raw manifest (got {kinds})"


def test_raw_manifests_all_parse_as_yaml():
    for fname in (
        "namespace.yaml",
        "configmap.yaml",
        "secret.example.yaml",
        "deployment-web.yaml",
        "deployment-worker.yaml",
        "service.yaml",
        "ingress.yaml",
        "hpa.yaml",
    ):
        path = os.path.join(K8S_DIR, fname)
        assert os.path.isfile(path), f"raw manifest {fname} missing"
        docs = [d for d in yaml.safe_load_all(_read(path)) if d]
        assert docs, f"{fname} parsed to nothing"
        for d in docs:
            assert "kind" in d, f"{fname} document has no kind"
