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


def snap_payload(op, specs):
    """specs: list of (valve_id, initial, target, expected_revision)."""
    return {
        "operation_id": op,
        "valves": [
            {"valve_id": vid, "initial_opening": init, "target_opening": tgt,
             "expected_revision": rev}
            for vid, init, tgt, rev in specs
        ],
    }


def test_valves_endpoint_exposes_server_confirmed_revisions(client):
    c, _ = client
    assert c.post("/api/switches", json=payload("op-rev", n=2)).status_code == 201
    valves = {v["valve_id"]: v for v in c.get("/api/devices/valves").json()}
    assert valves["V01"]["revision"] == 1
    assert valves["V02"]["revision"] == 1

    acts = c.get("/api/devices/executed-actions",
                 params={"operation_id": "op-rev"}).json()
    assert [(a["valve_id"], a["expected_revision"], a["actual_revision"])
            for a in acts] == [("V01", 0, 1), ("V02", 0, 1)]


def test_stale_snapshot_rejected_over_http_and_replayable(client):
    """Two consoles load revision 0; the second submit must fence, not
    overwrite the first console's confirmed openings."""
    c, _ = client
    body = snap_payload("op-http-a", [("V01", 1, 51, 0), ("V02", 2, 52, 0)])
    assert c.post("/api/switches", json=body).status_code == 201

    stale = snap_payload("op-http-b", [("V01", 1, 70, 0), ("V02", 2, 70, 0)])
    r = c.post("/api/switches", json=stale)
    assert r.status_code == 201
    s = r.json()
    assert s["phase"] == "REVISION_CONFLICT"
    assert s["terminal"] and not s["success"] and not s["resumable"]
    conflict = s["conflict"]
    assert conflict["valve_id"] == "V01" and conflict["phase"] == "FORWARD"
    assert conflict["expected_revision"] == 0
    assert conflict["actual_revision"] == 1
    assert [(u["valve_id"], u["phase"]) for u in conflict["unexecuted"]] == [
        ("V01", "FORWARD"), ("V02", "FORWARD"),
    ]
    # Nothing on the devices changed for the stale request.
    cur = {v["valve_id"]: (v["opening"], v["revision"])
           for v in c.get("/api/devices/valves").json()}
    assert cur == {"V01": (51, 1), "V02": (52, 1)}
    assert c.get("/api/devices/executed-actions",
                 params={"operation_id": "op-http-b"}).json() == []

    # Same-id retransmission returns the original conclusion (200).
    r2 = c.post("/api/switches", json=stale)
    assert r2.status_code == 200
    assert r2.json()["phase"] == "REVISION_CONFLICT"
    assert r2.json()["conflict"] == conflict

    # The conclusion is queryable afterwards.
    g = c.get("/api/switches/op-http-b").json()
    assert g["phase"] == "REVISION_CONFLICT"
    assert g["conflict"]["valve_id"] == "V01"


def test_compensation_fence_over_http(client):
    """An earlier switch's late compensation must not revert the opening a
    newer switch already confirmed; it converges to REVISION_CONFLICT."""
    c, _ = client
    c.post("/api/test/failures",
           json={"forward": ["V03"], "compensate": ["V02"]})
    s1 = c.post("/api/switches", json=payload("op-http-s1", n=3)).json()
    assert s1["phase"] == "COMPENSATION_FAILED"

    # Other console confirms V01 51 -> 80 on top of revision 1.
    s2 = snap_payload("op-http-s2", [("V01", 51, 80, 1), ("V09", 9, 19, 0)])
    assert c.post("/api/switches", json=s2).json()["phase"] == "COMPLETED"

    c.post("/api/test/failures", json={"forward": ["V03"], "compensate": []})
    r = c.post("/api/switches/op-http-s1/resume")
    s = r.json()
    assert s["phase"] == "REVISION_CONFLICT"
    assert s["conflict"]["valve_id"] == "V01"
    assert s["conflict"]["phase"] == "COMPENSATE"
    assert s["conflict"]["expected_revision"] == 1
    assert s["conflict"]["actual_revision"] == 2
    assert [(u["valve_id"], u["phase"]) for u in s["conflict"]["unexecuted"]] == [
        ("V01", "COMPENSATE"),
    ]
    # The confirmed opening stands; V02 was restored (its revision matched).
    cur = {v["valve_id"]: v["opening"]
           for v in c.get("/api/devices/valves").json()}
    assert cur["V01"] == 80 and cur["V02"] == 2
