"""Smoke tests for the mock upstreams.

These are not testing the gateway. They check that the services the gateway
will proxy to behave the way the proxy tests are going to assume.
"""

import importlib

import pytest
from fastapi.testclient import TestClient

MODULES = [
    "upstream.service_a.main",
    "upstream.service_b.main",
    "upstream.service_c.main",
]


@pytest.mark.parametrize("module_path", MODULES)
def test_health_endpoint(module_path):
    module = importlib.import_module(module_path)
    with TestClient(module.app) as client:
        body = client.get("/health").json()
        assert body["status"] == "healthy"
        assert body["service"] == module.SERVICE_NAME


@pytest.mark.parametrize("module_path", MODULES)
def test_responses_carry_the_service_header(module_path):
    module = importlib.import_module(module_path)
    with TestClient(module.app) as client:
        response = client.get("/")
        assert response.headers["X-Upstream-Service"] == module.SERVICE_NAME


@pytest.mark.parametrize("module_path", MODULES)
def test_fault_injection_returns_503(module_path):
    module = importlib.import_module(module_path)
    with TestClient(module.app) as client:
        client.post("/_control/fail", params={"enabled": True})
        try:
            assert client.get("/").status_code == 503
        finally:
            client.post("/_control/fail", params={"enabled": False})
        assert client.get("/").status_code == 200


def test_orders_create_and_fetch():
    module = importlib.import_module("upstream.service_a.main")
    with TestClient(module.app) as client:
        created = client.post("/", json={"customer": "sara", "total": 55.0}).json()
        fetched = client.get(f"/{created['id']}").json()
        assert fetched["customer"] == "sara"


def test_inventory_rejects_oversell():
    module = importlib.import_module("upstream.service_c.main")
    with TestClient(module.app) as client:
        response = client.patch("/SKU-1003", json={"delta": -1})
        assert response.status_code == 409
