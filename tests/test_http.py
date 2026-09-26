"""HTTP-level tests against the FastAPI app via in-process ASGI transport."""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VALVE_DB_DIR", str(tmp_path))
    monkeypatch.setenv("VALVE_ALLOW_RESET", "1")
    import app.main as main
    importlib.reload(main)  # pick up env-driven db paths
    with TestClient(main.app) as c:
        c.headers.update({"Content-Type": "application/json"})
        yield c, main


def payload(op="op-http", n=4):
    return {
        "operation_id": op,
        "valves": [
            {"valve_id": f"V{i:02d}", "initial_opening": i,
             "target_opening": 50 + i}
            for i in range(1, n + 1)
        ],
    }


def test_health_and_ui(client):
    c, _ = client
    assert c.get("/health").json()["status"] == "healthy"
    page = c.get("/")
    assert page.status_code == 200 and "阀组切换" in page.text


def test_full_switch_lifecycle(client):
    c, _ = client
    r = c.post("/api/switches", json=payload())
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["phase"] == "COMPLETED"
    assert [v["current_opening"] for v in s["valves"]] == [51, 52, 53, 54]

    # GET after refresh returns the server-confirmed final state.
    g = c.get("/api/switches/op-http").json()
    assert g["phase"] == "COMPLETED" and g["terminal"] and g["success"]


def test_reverse_compensation_via_http(client):
    c, _ = client
    assert c.post("/api/test/failures",
                  json={"forward": ["V03"], "compensate": []}).status_code == 200
    r = c.post("/api/switches", json=payload("op-comp"))
    assert r.status_code == 201
    s = r.json()
    assert s["phase"] == "COMPENSATED"
    assert [v["current_opening"] for v in s["valves"]] == [1, 2, 3, 4]

    acts = c.get("/api/devices/executed-actions",
                 params={"operation_id": "op-comp"}).json()
    comp = [a for a in acts if a["phase"] == "COMPENSATE"]
    assert [a["valve_id"] for a in comp] == ["V02", "V01"]


def test_idempotent_replay_200_same_result(client):
    c, _ = client
    body = payload("op-idem")
    assert c.post("/api/switches", json=body).status_code == 201
    r2 = c.post("/api/switches", json=body)
    assert r2.status_code == 200
    assert r2.json()["phase"] == "COMPLETED"


def test_conflicting_payload_409_touches_nothing(client):
    c, _ = client
    assert c.post("/api/switches", json=payload("op-409", n=3)).status_code == 201
    changed = payload("op-409", n=3)
    changed["valves"][0]["target_opening"] = 99
    r = c.post("/api/switches", json=changed)
    assert r.status_code == 409
    assert "different payload" in r.json()["detail"]

    # Original target still in place; conflict call changed nothing.
    cur = {v["valve_id"]: v["opening"]
           for v in c.get("/api/devices/valves").json()}
    assert cur["V01"] == 51


def test_compensation_failure_then_resume(client):
    c, _ = client
    c.post("/api/test/failures",
           json={"forward": ["V03"], "compensate": ["V02"]})
    s = c.post("/api/switches", json=payload("op-resume")).json()
    assert s["phase"] == "COMPENSATION_FAILED"
    assert s["resumable"] is True

    # Fault clears; resume finishes the rollback.
    c.post("/api/test/failures", json={"forward": ["V03"], "compensate": []})
    s2 = c.post("/api/switches/op-resume/resume").json()
    assert s2["phase"] == "COMPENSATED"
    assert [v["current_opening"] for v in s2["valves"]] == [1, 2, 3, 4]


def test_validation_rejects_bad_count_and_opening(client):
    c, _ = client
    bad_count = payload("op-bad", n=2)
    bad_count["valves"] = bad_count["valves"][:1]
    assert c.post("/api/switches", json=bad_count).status_code == 422
    bad_opening = payload("op-bad2", n=2)
    bad_opening["valves"][0]["target_opening"] = 101
    assert c.post("/api/switches", json=bad_opening).status_code == 422


def v2valves(s):
    return s["valves"]
