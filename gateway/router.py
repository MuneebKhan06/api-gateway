"""Route table.

Routes are declared in a YAML file and loaded into memory once at startup.
Keeping them in memory means matching a request costs a prefix comparison and
no database round trip. Reloading is done by calling `reload()`, which main.py
wires to SIGHUP so routing can change without a restart.
"""

import logging
import threading
from pathlib import Path

import yaml

from gateway.schemas.gateway import RouteConfig, RouteTableConfig

logger = logging.getLogger(__name__)


class RouteNotFound(Exception):
    """Raised when no configured prefix matches the request path."""


class RouteTable:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._routes: list[RouteConfig] = []

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        """Read the YAML file and replace the in-memory table.

        The new table is built completely before it is swapped in, so a broken
        config file leaves the currently running table untouched.
        """
        raw = yaml.safe_load(self._path.read_text()) or {}
        parsed = RouteTableConfig(**raw)
        routes = _sort_by_specificity(parsed.routes)
        _reject_duplicates(routes)

        with self._lock:
            self._routes = routes
        logger.info("Loaded %d routes from %s", len(routes), self._path)

    def reload(self) -> bool:
        """Reload the file, keeping the old table if the new one does not parse."""
        try:
            self.load()
            return True
        except Exception:
            logger.exception("Route reload failed, keeping the previous table")
            return False

    def match(self, path: str) -> RouteConfig:
        """Return the most specific route whose prefix matches `path`."""
        with self._lock:
            routes = self._routes

        for route in routes:
            if _prefix_matches(path, route.path_prefix):
                return route
        raise RouteNotFound(path)

    def all(self) -> list[RouteConfig]:
        with self._lock:
            return list(self._routes)


def _prefix_matches(path: str, prefix: str) -> bool:
    """Match on path segments so /api/ordersXYZ does not match /api/orders."""
    if prefix == "/":
        return True
    if not path.startswith(prefix):
        return False
    return len(path) == len(prefix) or path[len(prefix)] == "/"


def _sort_by_specificity(routes: list[RouteConfig]) -> list[RouteConfig]:
    """Longest prefix first, so /api/orders/export wins over /api/orders."""
    return sorted(routes, key=lambda r: len(r.path_prefix), reverse=True)


def _reject_duplicates(routes: list[RouteConfig]) -> None:
    seen: set[str] = set()
    for route in routes:
        if route.path_prefix in seen:
            raise ValueError(f"duplicate path_prefix in route table: {route.path_prefix}")
        seen.add(route.path_prefix)


def build_route_table(path: str | Path) -> RouteTable:
    table = RouteTable(path)
    table.load()
    return table
