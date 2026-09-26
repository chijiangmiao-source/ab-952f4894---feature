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


def payload(op="op-http", n=4, revisions=None):
    body = {
        "operation_id": op,
        "valves": [
            {"valve_id": f"V{i:02d}", "initial_opening": i,
             "target_opening": 50 + i}
            for i in range(1, n + 1)
        ],
    }
    if revisions is not None:
        body["revisions"] = revisions
    return body


def valve_map(c):
    return {v["valve_id"]: v for v in c.get("/api/devices/valves").json()}


def test_health_and_ui(client):
    c, _ = client
    assert c.get("/health").json()["status"] == "healthy"
    page = c.get("/")
    assert page.status_code == 200 and "阀组切换" in page.text
    # The console displays the server-confirmed revision snapshot.
    assert "修订号" in page.text


def test_full_switch_lifecycle(client):
    c, _ = client
    r = c.post("/api/switches", json=payload())
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["phase"] == "COMPLETED"
    assert [v["current_opening"] for v in s["valves"]] == [51, 52, 53, 54]
    assert [v["current_revision"] for v in s["valves"]] == [1, 1, 1, 1]

    # GET after refresh returns the server-confirmed final state.
    g = c.get("/api/switches/op-http").json()
    assert g["phase"] == "COMPLETED" and g["terminal"] and g["success"]

    # The device endpoint publishes the confirmed revision per valve.
    cur = valve_map(c)
    assert cur["V01"]["revision"] == 1 and cur["V01"]["opening"] == 51


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
    # Receipts persist the expected/actual revision interval.
    assert [(a["expected_revision"], a["actual_revision"]) for a in comp] == [
        (1, 2), (1, 2)
    ]


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
    cur = valve_map(c)
    assert cur["V01"]["opening"] == 51


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


def test_validation_rejects_snapshot_not_matching_valves(client):
    c, _ = client
    body = payload("op-badrev", n=2, revisions={"V01": 0})  # V02 missing
    r = c.post("/api/switches", json=body)
    assert r.status_code == 422
    body = payload("op-badrev2", n=2, revisions={"V01": 0, "V02": 0, "V09": 0})
    assert c.post("/api/switches", json=body).status_code == 422


# ---------------------------------------------------------------------
# Revision fence over HTTP
# ---------------------------------------------------------------------

def test_stale_snapshot_rejected_via_revision_fence(client):
    """Two consoles hold the same r0 snapshot; the second submit is fenced
    off instead of overwriting the first switch's confirmed openings."""
    c, _ = client
    snap = {f"V{i:02d}": 0 for i in range(1, 5)}

    r1 = c.post("/api/switches", json=payload("op-e1", revisions=snap))
    assert r1.status_code == 201 and r1.json()["phase"] == "COMPLETED"

    # Second console submits its own switch with the now-stale snapshot.
    r2 = c.post("/api/switches",
                json=payload("op-e2", revisions=snap))
    assert r2.status_code == 201
    s = r2.json()
    assert s["phase"] == "REVISION_CONFLICT"
    assert s["terminal"] and not s["success"] and not s["resumable"]
    assert [(x["valve_id"], x["phase"], x["expected_revision"],
             x["actual_revision"]) for x in s["conflicts"]] == [
        ("V01", "FORWARD", 0, 1)
    ]
    assert [(u["valve_id"], u["phase"]) for u in s["unexecuted"]] == [
        ("V02", "FORWARD"), ("V03", "FORWARD"), ("V04", "FORWARD")
    ]
    by_id = {v["valve_id"]: v for v in s["valves"]}
    assert by_id["V01"]["forward"] == "FENCED"
    assert by_id["V01"]["current_revision"] == 1

    # The first switch's confirmed openings/revisions are untouched.
    cur = valve_map(c)
    assert {k: (v["opening"], v["revision"]) for k, v in cur.items()} == {
        "V01": (51, 1), "V02": (52, 1), "V03": (53, 1), "V04": (54, 1),
    }

    # The conclusion is stably queryable ...
    g = c.get("/api/switches/op-e2").json()
    assert g["phase"] == "REVISION_CONFLICT"
    assert g["conflicts"] == s["conflicts"]
    # ... and a same-id retransmission only returns the original conclusion.
    r3 = c.post("/api/switches", json=payload("op-e2", revisions=snap))
    assert r3.status_code == 200
    assert r3.json()["phase"] == "REVISION_CONFLICT"
    assert r3.json()["conflicts"] == s["conflicts"]


def test_compensation_fence_does_not_revert_other_switch(client):
    """S2's stuck compensation must not roll back S1's later confirmed
    openings once S1 advanced the revisions."""
    c, _ = client
    c.post("/api/test/failures",
           json={"forward": ["V03"], "compensate": ["V02"]})
    s = c.post("/api/switches", json=payload("op-s2")).json()
    assert s["phase"] == "COMPENSATION_FAILED"
    assert valve_map(c)["V01"]["revision"] == 1

    # Another console confirms new openings on V01/V02 (fresh snapshot r1).
    other = {
        "operation_id": "op-s1",
        "valves": [
            {"valve_id": "V01", "initial_opening": 51, "target_opening": 71},
            {"valve_id": "V02", "initial_opening": 52, "target_opening": 72},
        ],
        "revisions": {"V01": 1, "V02": 1},
    }
    r1 = c.post("/api/switches", json=other)
    assert r1.status_code == 201 and r1.json()["phase"] == "COMPLETED"

    # Fault clears; S2 resumes and its compensation hits the fence.
    c.post("/api/test/failures", json={"forward": ["V03"], "compensate": []})
    r2 = c.post("/api/switches/op-s2/resume")
    assert r2.status_code == 200
    s2 = r2.json()
    assert s2["phase"] == "REVISION_CONFLICT"
    assert [(x["valve_id"], x["phase"], x["expected_revision"],
             x["actual_revision"]) for x in s2["conflicts"]] == [
        ("V02", "COMPENSATE", 1, 2)
    ]
    assert ("V01", "COMPENSATE") in [(u["valve_id"], u["phase"])
                                     for u in s2["unexecuted"]]

    # S1's confirmed openings were NOT reverted.
    cur = valve_map(c)
    assert (cur["V01"]["opening"], cur["V01"]["revision"]) == (71, 2)
    assert (cur["V02"]["opening"], cur["V02"]["revision"]) == (72, 2)

    # Same-id replay of S2 returns the stored conclusion, no device activity.
    acts_before = c.get("/api/devices/executed-actions").json()
    r3 = c.post("/api/switches", json=payload("op-s2"))
    assert r3.status_code == 200
    assert r3.json()["phase"] == "REVISION_CONFLICT"
    assert c.get("/api/devices/executed-actions").json() == acts_before


def test_legacy_request_without_snapshot_still_works(client):
    c, _ = client
    assert c.post("/api/switches", json=payload("op-a", n=2)).status_code == 201
    # Legacy page (no revisions key) on the same valves: accepted with an
    # acceptance-time snapshot.
    r = c.post("/api/switches", json=payload("op-b", n=2))
    assert r.status_code == 201
    s = r.json()
    assert s["phase"] == "COMPLETED"
    by_id = {v["valve_id"]: v for v in s["valves"]}
    assert by_id["V01"]["forward_expected_revision"] == 1
    assert by_id["V01"]["forward_actual_revision"] == 2
