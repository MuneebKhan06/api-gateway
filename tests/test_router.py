import pytest

from gateway.router import RouteNotFound, RouteTable

ROUTES = """
routes:
  - path_prefix: /api/orders
    upstream: http://service-a:8001
    rate_limit:
      algorithm: token_bucket
      requests: 100
      window_seconds: 60
  - path_prefix: /api/orders/export
    upstream: http://service-a:8001
    timeout_seconds: 120
  - path_prefix: /health
    upstream: null
    auth_required: false
"""


@pytest.fixture
def table(tmp_path):
    config = tmp_path / "routes.yaml"
    config.write_text(ROUTES)
    table = RouteTable(config)
    table.load()
    return table


def test_matches_exact_prefix(table):
    assert table.match("/api/orders").upstream == "http://service-a:8001"


def test_matches_nested_path(table):
    assert table.match("/api/orders/42").path_prefix == "/api/orders"


def test_longest_prefix_wins(table):
    # /api/orders/export is more specific and must not fall through to /api/orders
    assert table.match("/api/orders/export").timeout_seconds == 120


def test_partial_segment_does_not_match(table):
    with pytest.raises(RouteNotFound):
        table.match("/api/ordersXYZ")


def test_unknown_path_raises(table):
    with pytest.raises(RouteNotFound):
        table.match("/nope")


def test_route_without_upstream_disables_breaker(table):
    health = table.match("/health")
    assert health.upstream is None
    assert health.circuit_breaker is False


def test_duplicate_prefix_is_rejected(tmp_path):
    config = tmp_path / "routes.yaml"
    config.write_text(
        "routes:\n"
        "  - path_prefix: /api/a\n"
        "    upstream: http://a:1\n"
        "  - path_prefix: /api/a\n"
        "    upstream: http://b:2\n"
    )
    table = RouteTable(config)
    with pytest.raises(ValueError):
        table.load()


def test_reload_keeps_old_table_when_new_config_is_broken(table):
    table.path.write_text("routes: [{path_prefix: 'missing-slash'}]")
    assert table.reload() is False
    # The good table from the fixture is still serving traffic.
    assert table.match("/api/orders").upstream == "http://service-a:8001"
