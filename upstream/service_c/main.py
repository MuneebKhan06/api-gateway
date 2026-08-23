"""Mock inventory service.

The third upstream. Its stock levels change on write, which makes it the
easiest of the three to eyeball when checking that the proxy forwards request
bodies correctly.
"""

import asyncio

from fastapi import FastAPI, HTTPException, Response

SERVICE_NAME = "service-c"

app = FastAPI(title="Inventory Service", version="0.1.0")

STOCK = {
    "SKU-1001": {"sku": "SKU-1001", "name": "Mechanical keyboard", "quantity": 42},
    "SKU-1002": {"sku": "SKU-1002", "name": "27 inch monitor", "quantity": 7},
    "SKU-1003": {"sku": "SKU-1003", "name": "USB-C dock", "quantity": 0},
}

_state = {"fail": False, "latency_ms": 0}


@app.middleware("http")
async def inject_faults(request, call_next):
    if request.url.path.startswith("/_control"):
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
async def list_stock() -> dict:
    return {"service": SERVICE_NAME, "items": list(STOCK.values())}


@app.get("/{sku}")
async def get_item(sku: str) -> dict:
    if sku not in STOCK:
        raise HTTPException(status_code=404, detail="sku not found")
    return STOCK[sku]


@app.patch("/{sku}")
async def adjust_quantity(sku: str, payload: dict) -> dict:
    if sku not in STOCK:
        raise HTTPException(status_code=404, detail="sku not found")

    delta = int(payload.get("delta", 0))
    new_quantity = STOCK[sku]["quantity"] + delta
    if new_quantity < 0:
        raise HTTPException(status_code=409, detail="not enough stock")

    STOCK[sku]["quantity"] = new_quantity
    return STOCK[sku]


@app.post("/_control/fail")
async def set_failing(enabled: bool = True) -> dict:
    _state["fail"] = enabled
    return {"failing": enabled}


@app.post("/_control/latency")
async def set_latency(milliseconds: int = 0) -> dict:
    _state["latency_ms"] = max(0, milliseconds)
    return {"latency_ms": _state["latency_ms"]}
