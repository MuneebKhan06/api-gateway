import uuid

from fastapi.testclient import TestClient

from gateway.config import Settings
from gateway.main import create_app
from gateway.middleware.correlation import get_request_id


def make_client() -> TestClient:
    return TestClient(create_app(Settings(routes_file="routes.yaml", environment="test")))


def test_response_always_carries_a_request_id():
    with make_client() as client:
        response = client.get("/health")
        assert response.headers["x-request-id"]


def test_generated_ids_are_unique_per_request():
    with make_client() as client:
        first = client.get("/health").headers["x-request-id"]
        second = client.get("/health").headers["x-request-id"]
        assert first != second


def test_client_supplied_id_is_preserved():
    supplied = uuid.uuid4().hex
    with make_client() as client:
        response = client.get("/health", headers={"X-Request-ID": supplied})
        assert response.headers["x-request-id"] == supplied


def test_garbage_client_id_is_replaced():
    # Control characters and newlines would end up in log files verbatim.
    with make_client() as client:
        response = client.get("/health", headers={"X-Request-ID": "bad id;drop"})
        assert response.headers["x-request-id"] != "bad id;drop"


def test_overlong_client_id_is_replaced():
    with make_client() as client:
        response = client.get("/health", headers={"X-Request-ID": "a" * 500})
        assert len(response.headers["x-request-id"]) <= 128


def test_id_is_readable_from_the_handler():
    seen = {}
    settings = Settings(routes_file="routes.yaml", environment="test")
    app = create_app(settings)

    @app.get("/_probe")
    async def probe() -> dict:
        seen["from_contextvar"] = get_request_id()
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/_probe")
        assert seen["from_contextvar"] == response.headers["x-request-id"]


def test_context_is_cleared_between_requests():
    with make_client() as client:
        client.get("/health")
    # Back outside any request, the default placeholder is in effect again.
    assert get_request_id() == "-"
