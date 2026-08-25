"""Mock orders service.

Stands in for a real backend so the gateway has something to proxy to. The
/_control endpoints let tests force failures and latency, which is how the
circuit breaker gets exercised without taking a container down.
"""

import asyncio
import uuid

from fastapi import FastAPI, HTTPException, Request, Response

SERVICE_NAME = "service-a"

app = FastAPI(title="Orders Service", version="0.1.0")

ORDERS = [
    {"id": "a1f0", "customer": "ayesha", "total": 240.00, "status": "shipped"},
    {"id": "b2c9", "customer": "daniyal", "total": 89.50, "status": "pending"},
    {"id": "c3d7", "customer": "hina", "total": 1320.75, "status": "delivered"},
]

# Failure injection state, driven by the control endpoints below.
_state = {"fail": False, "latency_ms": 0}


@app.middleware("http")
async def inject_faults(request, call_next):
    if request.url.path.startswith(("/_control", "/_echo")):
        return await call_next(request)

    if _state["latency_ms"]:
        await asyncio.sleep(_state["latency_ms"] / 1000)
    if _state["fail"]:
        return Response(content='{"detail":"upstream is unwell"}', status_code=503,
                        media_type="application/json")

    response = await call_next(request)
    response.headers["X-Upstream-Service"] = SERVICE_NAME
    return response


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "service": SERVICE_NAME}


@app.get("/")
async def list_orders() -> dict:
    return {"service": SERVICE_NAME, "count": len(ORDERS), "orders": ORDERS}


@app.get("/{order_id}")
async def get_order(order_id: str) -> dict:
    for order in ORDERS:
        if order["id"] == order_id:
            return order
    raise HTTPException(status_code=404, detail="order not found")


@app.post("/", status_code=201)
async def create_order(payload: dict) -> dict:
    order = {
        "id": uuid.uuid4().hex[:4],
        "customer": payload.get("customer", "unknown"),
        "total": payload.get("total", 0.0),
        "status": "pending",
    }
    ORDERS.append(order)
    return order


@app.get("/_echo/headers")
async def echo_headers(request: Request) -> dict:
    """Report what the upstream actually received.

    Used to check that the gateway forwards the caller's identity and strips
    anything the client tried to spoof.
    """
    return {"headers": {key.lower(): value for key, value in request.headers.items()}}


@app.post("/_control/fail")
async def set_failing(enabled: bool = True) -> dict:
    _state["fail"] = enabled
    return {"failing": enabled}


@app.post("/_control/latency")
async def set_latency(milliseconds: int = 0) -> dict:
    _state["latency_ms"] = max(0, milliseconds)
    return {"latency_ms": _state["latency_ms"]}
