"""Checks on the Prometheus and Grafana configuration.

These are config files rather than code, but a dashboard that queries a metric
the gateway does not export is broken in a way nothing else would catch. These
tests tie the config to the metric names the gateway actually registers.
"""

import json
import re
from pathlib import Path

import yaml

from gateway.metrics.prometheus import REGISTRY

ROOT = Path(__file__).resolve().parent.parent


def exported_metric_names() -> set[str]:
    """Base names the gateway registers, plus histogram derived series."""
    names = set()
    for metric in REGISTRY.collect():
        names.add(metric.name)
        if metric.type == "histogram":
            names.update(
                {
                    f"{metric.name}_bucket",
                    f"{metric.name}_count",
                    f"{metric.name}_sum",
                }
            )
        if metric.type == "counter":
            names.add(f"{metric.name}_total")
    return names


def metrics_referenced_in(text: str) -> set[str]:
    return set(re.findall(r"\bgateway_[a-z_]+\b", text))


class TestPrometheusConfig:
    def test_config_parses(self):
        config = yaml.safe_load((ROOT / "prometheus" / "prometheus.yml").read_text())
        assert config["global"]["scrape_interval"]

    def test_gateway_is_a_scrape_target(self):
        config = yaml.safe_load((ROOT / "prometheus" / "prometheus.yml").read_text())
        jobs = {job["job_name"]: job for job in config["scrape_configs"]}
        assert "api-gateway" in jobs
        assert jobs["api-gateway"]["metrics_path"] == "/metrics"

    def test_scrape_target_matches_the_compose_service(self):
        config = yaml.safe_load((ROOT / "prometheus" / "prometheus.yml").read_text())
        targets = config["scrape_configs"][0]["static_configs"][0]["targets"]
        assert "gateway:8000" in targets

        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
        assert "gateway" in compose["services"]

    def test_alert_rules_are_loaded(self):
        config = yaml.safe_load((ROOT / "prometheus" / "prometheus.yml").read_text())
        assert "/etc/prometheus/alerts.yml" in config["rule_files"]


class TestAlertRules:
    def test_rules_parse(self):
        rules = yaml.safe_load((ROOT / "prometheus" / "alerts.yml").read_text())
        assert rules["groups"]

    def test_every_alert_has_a_severity_and_summary(self):
        rules = yaml.safe_load((ROOT / "prometheus" / "alerts.yml").read_text())
        for group in rules["groups"]:
            for rule in group["rules"]:
                assert rule["labels"]["severity"] in {"warning", "critical"}
                assert rule["annotations"]["summary"]

    def test_alerts_only_reference_metrics_the_gateway_exports(self):
        """An alert on a metric that does not exist never fires, which is the
        worst possible failure mode for an alert."""
        text = (ROOT / "prometheus" / "alerts.yml").read_text()
        exported = exported_metric_names()
        for name in metrics_referenced_in(text):
            assert name in exported, f"alert references unknown metric {name}"

    def test_breaker_alert_uses_the_documented_encoding(self):
        rules = yaml.safe_load((ROOT / "prometheus" / "alerts.yml").read_text())
        rule = next(
            r
            for group in rules["groups"]
            for r in group["rules"]
            if r["alert"] == "CircuitBreakerOpen"
        )
        # 1 is open in the gauge encoding.
        assert "== 1" in rule["expr"]


class TestGrafanaDashboard:
    def test_dashboard_parses(self):
        dashboard = json.loads((ROOT / "grafana" / "dashboards" / "gateway.json").read_text())
        assert dashboard["title"]
        assert dashboard["panels"]

    def test_panel_ids_are_unique(self):
        dashboard = json.loads((ROOT / "grafana" / "dashboards" / "gateway.json").read_text())
        ids = [panel["id"] for panel in dashboard["panels"]]
        assert len(ids) == len(set(ids))

    def test_every_panel_has_a_query(self):
        dashboard = json.loads((ROOT / "grafana" / "dashboards" / "gateway.json").read_text())
        for panel in dashboard["panels"]:
            assert panel["targets"], f"panel {panel['title']} has no query"
            for t in panel["targets"]:
                assert t["expr"].strip()

    def test_panels_only_query_metrics_the_gateway_exports(self):
        """The check that matters: a dashboard querying a metric that does not
        exist renders an empty graph and looks like an outage."""
        dashboard = (ROOT / "grafana" / "dashboards" / "gateway.json").read_text()
        exported = exported_metric_names()
        for name in metrics_referenced_in(dashboard):
            assert name in exported, f"dashboard references unknown metric {name}"

    def test_latency_panel_uses_histogram_quantile(self):
        dashboard = json.loads((ROOT / "grafana" / "dashboards" / "gateway.json").read_text())
        latency = next(p for p in dashboard["panels"] if "Latency" in p["title"])
        for t in latency["targets"]:
            assert "histogram_quantile" in t["expr"]
            assert "_bucket" in t["expr"]

    def test_error_rate_is_a_ratio_not_a_raw_count(self):
        """A raw count grows with traffic and says nothing on its own."""
        dashboard = json.loads((ROOT / "grafana" / "dashboards" / "gateway.json").read_text())
        errors = next(p for p in dashboard["panels"] if "Error rate" in p["title"])
        assert "/" in errors["targets"][0]["expr"]
        assert "gateway_requests_total" in errors["targets"][0]["expr"]


class TestGrafanaProvisioning:
    def test_datasource_points_at_prometheus(self):
        config = yaml.safe_load(
            (ROOT / "grafana" / "provisioning" / "datasources" / "prometheus.yml").read_text()
        )
        source = config["datasources"][0]
        assert source["type"] == "prometheus"
        assert source["url"] == "http://prometheus:9090"

    def test_dashboard_provider_path_matches_the_compose_mount(self):
        config = yaml.safe_load(
            (ROOT / "grafana" / "provisioning" / "dashboards" / "dashboards.yml").read_text()
        )
        path = config["providers"][0]["options"]["path"]

        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
        mounts = compose["services"]["grafana"]["volumes"]
        assert any(mount.split(":")[1] == path for mount in mounts)


class TestComposeStack:
    def test_observability_services_are_defined(self):
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
        assert "prometheus" in compose["services"]
        assert "grafana" in compose["services"]

    def test_grafana_waits_for_prometheus(self):
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
        assert "prometheus" in compose["services"]["grafana"]["depends_on"]

    def test_data_is_persisted(self):
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
        assert "prometheus-data" in compose["volumes"]
        assert "grafana-data" in compose["volumes"]
