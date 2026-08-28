"""Prometheus scrape endpoint.

Serves the gateway's metrics in the text exposition format Prometheus reads.

Deliberately unauthenticated, matching routes.yaml. Metrics say how much
traffic each upstream is taking and how it is failing, which is operational
detail rather than user data, and requiring a token here would mean giving
Prometheus a credential to rotate. In a real deployment this endpoint would be
reachable only from the monitoring network, not the public internet, which is
where that restriction belongs.
"""

import logging

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from gateway.metrics.prometheus import REGISTRY

logger = logging.getLogger(__name__)

router = APIRouter(tags=["gateway"])


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Render the current values of every registered metric.

    generate_latest walks the registry and formats it; there is no state to
    reset afterwards, since counters are monotonic and Prometheus computes
    rates from successive scrapes.
    """
    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
