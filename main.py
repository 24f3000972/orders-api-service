import base64
from collections import defaultdict
import math
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After", "Idempotency-Key", "X-Client-Id"],
)

# Configuration from assigned values
TOTAL_ORDERS = 51
RATE_LIMIT_R = 16
WINDOW_SECONDS = 10.0

# Fixed catalog of orders: IDs 1 through 51
CATALOG = [
    {
        "id": i,
        "name": f"Product Order #{i}",
        "status": "completed",
        "created_at": f"2026-01-{i:02d}T10:00:00Z" if i <= 31 else f"2026-02-{(i - 31):02d}T10:00:00Z",
    }
    for i in range(1, TOTAL_ORDERS + 1)
]

# Stores for idempotency & rate-limiting
IDEMPOTENCY_CACHE: Dict[str, Dict[str, Any]] = {}
CLIENT_TIMESTAMPS: Dict[str, List[float]] = defaultdict(list)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    # Only enforce per-client rate limit if X-Client-Id header is provided
    client_id = request.headers.get("x-client-id")
    if client_id:
        now = time.time()
        # Keep timestamps within the last 10 seconds
        timestamps = [t for t in CLIENT_TIMESTAMPS[client_id] if now - t < WINDOW_SECONDS]
        CLIENT_TIMESTAMPS[client_id] = timestamps

        if len(timestamps) >= RATE_LIMIT_R:
            # Rate limit exceeded -> return 429 with Retry-After header
            oldest = timestamps[0]
            retry_after = max(1, math.ceil(WINDOW_SECONDS - (now - oldest)))
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "Too Many Requests"},
                headers={"Retry-After": str(retry_after)},
            )

        # Allow request and record timestamp
        CLIENT_TIMESTAMPS[client_id].append(now)

    response = await call_next(request)
    return response


# ── 1. Idempotent POST /orders ────────────────────────────────────────────────
@app.post("/orders")
async def create_order(request: Request):
    idempotency_key = request.headers.get("idempotency-key")

    # If key already seen, return the exact same created order
    if idempotency_key and idempotency_key in IDEMPOTENCY_CACHE:
        cached = IDEMPOTENCY_CACHE[idempotency_key]
        return JSONResponse(
            status_code=cached["status_code"],
            content=cached["data"],
        )

    # First call: create new order
    try:
        body = await request.json()
    except Exception:
        body = {}

    order_id = f"ord_{uuid.uuid4().hex[:10]}"
    new_order = {
        "id": order_id,
        "status": "created",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "data": body,
    }

    if idempotency_key:
        IDEMPOTENCY_CACHE[idempotency_key] = {
            "status_code": status.HTTP_201_CREATED,
            "data": new_order,
        }

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=new_order,
    )


# ── 2. Cursor Pagination GET /orders ──────────────────────────────────────────
@app.get("/orders")
def get_orders(
    limit: int = Query(default=10, ge=1),
    cursor: Optional[str] = Query(default=None),
):
    if not cursor:
        start_idx = 0
    else:
        try:
            # Cursor is the last returned order ID
            last_id = int(cursor)
            # Since IDs are 1..51, ID `last_id` was at index `last_id - 1`
            start_idx = last_id
        except ValueError:
            start_idx = 0

    items = CATALOG[start_idx : start_idx + limit]

    if start_idx + len(items) < len(CATALOG):
        next_cursor = str(items[-1]["id"])
    else:
        next_cursor = None

    return {
        "items": items,
        "orders": items,  # alias
        "next_cursor": next_cursor,
        "next": next_cursor,  # alias
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"service": "Production Orders API"}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
