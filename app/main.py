"""FastAPI application: valve-bank switch service."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .device import DeviceBank
from .models import SwitchRequest, SwitchStatus
from .saga import PayloadConflict, SagaEngine
from .store import SwitchStore


class FailureConfig(BaseModel):
    forward: list[str] = []
    compensate: list[str] = []

DB_DIR = os.environ.get("VALVE_DB_DIR", "/data")
os.makedirs(DB_DIR, exist_ok=True)
DEVICE_DB = os.path.join(DB_DIR, "devices.db")
SWITCH_DB = os.path.join(DB_DIR, "switches.db")

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Vacuum Valve Bank Switch Service", version="1.0.0")

store = SwitchStore(SWITCH_DB)
devices = DeviceBank(DEVICE_DB)
engine = SagaEngine(store, devices)


@app.get("/health")
def health() -> dict:
    """Liveness/readiness probe: both durable stores reachable."""
    store.get("\x00__probe__")
    devices.executed_actions()
    return {"status": "healthy"}


@app.post("/api/switches", response_model=SwitchStatus)
def submit_switch(req: SwitchRequest) -> Response:
    try:
        status, created = engine.submit(req)
    except PayloadConflict:
        raise HTTPException(
            status_code=409,
            detail=(
                f"operation_id {req.operation_id!r} already exists with a "
                "different payload; no device was touched"
            ),
        )
    return JSONResponse(
        status_code=201 if created else 200,
        content=status.model_dump(),
    )


@app.get("/api/switches/{operation_id}", response_model=SwitchStatus)
def get_switch(operation_id: str) -> SwitchStatus:
    status = engine.status(operation_id)
    if status is None:
        raise HTTPException(status_code=404, detail="operation_id not found")
    return status


@app.post("/api/switches/{operation_id}/resume", response_model=SwitchStatus)
def resume_switch(operation_id: str) -> SwitchStatus:
    status = engine.resume(operation_id)
    if status is None:
        raise HTTPException(status_code=404, detail="operation_id not found")
    return status


@app.get("/api/devices/executed-actions")
def get_executed_actions(operation_id: Optional[str] = None) -> list[dict]:
    return [
        {
            "operation_id": a.operation_id,
            "valve_id": a.valve_id,
            "phase": a.phase,
            "opening": a.opening,
            "expected_revision": a.expected_revision,
            "actual_revision": a.actual_revision,
            "executed_at": a.executed_at,
        }
        for a in devices.executed_actions(operation_id)
    ]


@app.get("/api/devices/valves")
def get_valves() -> list[dict]:
    """Current opening AND server-confirmed revision per valve — this is the
    snapshot the console displays and carries with the next switch."""
    return [
        {"valve_id": v.valve_id, "opening": v.opening, "revision": v.revision}
        for v in devices.list_valves()
    ]


# ------------------------------------------------------------------ test hooks

@app.post("/api/test/reset")
def test_reset() -> dict:
    """Reset both stores and the device bank (test support only)."""
    if os.environ.get("VALVE_ALLOW_RESET", "0") != "1":
        raise HTTPException(status_code=403, detail="reset disabled")
    store.reset()
    devices.reset()
    return {"status": "reset"}


@app.post("/api/test/failures", response_model=FailureConfig)
def test_failures(cfg: FailureConfig) -> FailureConfig:
    if os.environ.get("VALVE_ALLOW_RESET", "0") != "1":
        raise HTTPException(status_code=403, detail="reset disabled")
    devices.set_failures(cfg.forward, cfg.compensate)
    return cfg


# ----------------------------------------------------------------- static UI

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
