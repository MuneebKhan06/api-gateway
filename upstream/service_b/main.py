"""Mock users service.

Same shape as the orders service: a small read-mostly API plus fault
injection controls for exercising the gateway's failure handling.
"""

import asyncio

from fastapi import FastAPI, HTTPException, Response

SERVICE_NAME = "service-b"

app = FastAPI(title="Users Service", version="0.1.0")

USERS = [
    {"id": 1, "name": "Ayesha Siddiqui", "email": "ayesha@example.com", "role": "admin"},
    {"id": 2, "name": "Daniyal Raza", "email": "daniyal@example.com", "role": "user"},
    {"id": 3, "name": "Hina Malik", "email": "hina@example.com", "role": "user"},
]

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
async def list_users() -> dict:
    return {"service": SERVICE_NAME, "count": len(USERS), "users": USERS}


@app.get("/{user_id}")
async def get_user(user_id: int) -> dict:
    for user in USERS:
        if user["id"] == user_id:
            return user
    raise HTTPException(status_code=404, detail="user not found")


@app.post("/_control/fail")
async def set_failing(enabled: bool = True) -> dict:
    _state["fail"] = enabled
    return {"failing": enabled}


@app.post("/_control/latency")
async def set_latency(milliseconds: int = 0) -> dict:
    _state["latency_ms"] = max(0, milliseconds)
    return {"latency_ms": _state["latency_ms"]}
