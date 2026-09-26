"""End-to-end crash/restart tests using a real uvicorn process.

The server process is killed with ``os._exit(77)`` at the precise instant
*after* the simulated device committed an action but *before* the
application stored its receipt (controlled by ``CRASH_AFTER``). A brand new
process is then started against the same data directory and must recognize
the executed action through the device dedupe log instead of moving the
valve twice.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from app.device import CRASH_EXIT_CODE

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_healthy(port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
            if r.status_code == 200:
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.2)
    raise RuntimeError(f"server never became healthy: {last}")


@contextmanager
def server(data_dir: Path, port: int, crash_after: str | None = None):
    env = os.environ.copy()
    env["VALVE_DB_DIR"] = str(data_dir)
    env["VALVE_ALLOW_RESET"] = "1"
    if crash_after:
        env["CRASH_AFTER"] = crash_after
    log = open(data_dir / f"server-{port}.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env,
        stdout=log, stderr=subprocess.STDOUT, text=True,
    )
    try:
        if crash_after:
            # The process is expected to kill itself; wait for exit instead
            # of ordinary readiness.
            yield proc
        else:
            _wait_healthy(port)
            yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.close()


def _payload(op: str, n: int = 4) -> dict:
    return {
        "operation_id": op,
        "valves": [
            {"valve_id": f"V{i:02d}", "initial_opening": i,
             "target_opening": 50 + i}
            for i in range(1, n + 1)
        ],
    }


def _wait_dead(proc: subprocess.Popen, timeout: float = 20.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc = proc.poll()
        if rc is not None:
            return rc
        time.sleep(0.1)
    proc.kill()
    raise AssertionError("crashed server did not exit")


def test_crash_after_forward_device_commit_recognized_on_restart(tmp_path):
    port = _free_port()
    body = _payload("op-crash-forward")

    # ---- first process: crash right after V02 FORWARD commits on device
    with server(tmp_path, port, crash_after="op-crash-forward:V02:FORWARD") as proc:
        _wait_healthy(port)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as api:
            # Submit; the server hard-exits before replying.
            with pytest.raises(httpx.HTTPError):
                api.post("/api/switches", json=body)
        assert _wait_dead(proc) == CRASH_EXIT_CODE

    # ---- console restarts: no crash marker, same durable data
    with server(tmp_path, port) as _:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as api:
            mid = api.get("/api/switches/op-crash-forward")
            assert mid.status_code == 200
            s0 = mid.json()
            assert s0["phase"] == "EXECUTING"  # explicit, explained half-state
            by_id = {v["valve_id"]: v for v in s0["valves"]}
            assert by_id["V01"]["forward"] == "SUCCESS"
            assert by_id["V02"]["forward"] == "PENDING"  # receipt was lost

            # Re-submitting the SAME operation id resumes the switch.
            r = api.post("/api/switches", json=body)
            assert r.status_code == 200
            s = r.json()
            assert s["phase"] == "COMPLETED"
            by_id = {v["valve_id"]: v for v in s["valves"]}
            # V02 was recognized on the device, not driven a second time.
            assert by_id["V02"]["forward"] == "SUCCESS"

            acts = api.get(
                "/api/devices/executed-actions",
                params={"operation_id": "op-crash-forward"},
            ).json()
            forward = [a for a in acts if a["phase"] == "FORWARD"]
            # Exactly one FORWARD action per valve — no double movement.
            assert [(a["valve_id"], a["opening"]) for a in forward] == [
                ("V01", 51), ("V02", 52), ("V03", 53), ("V04", 54)
            ]
            # Each persisted as the conditional change 0 -> 1; the restart
            # recognized V02's committed change and did NOT advance its
            # revision a second time.
            assert [(a["expected_revision"], a["actual_revision"])
                    for a in forward] == [(0, 1)] * 4
            final = {v["valve_id"]: (v["opening"], v["revision"])
                     for v in api.get("/api/devices/valves").json()}
            assert final == {"V01": (51, 1), "V02": (52, 1),
                             "V03": (53, 1), "V04": (54, 1)}


def test_crash_during_compensation_resumes_reverse_rollback(tmp_path):
    port = _free_port()
    body = _payload("op-crash-comp")

    with server(tmp_path, port,
                crash_after="op-crash-comp:V02:COMPENSATE") as proc:
        _wait_healthy(port)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as api:
            api.post("/api/test/failures",
                     json={"forward": ["V03"], "compensate": []})
            with pytest.raises(httpx.HTTPError):
                api.post("/api/switches", json=body)
        assert _wait_dead(proc) == CRASH_EXIT_CODE

    with server(tmp_path, port) as _:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as api:
            s0 = api.get("/api/switches/op-crash-comp").json()
            assert s0["phase"] == "COMPENSATING"

            # Same-id replay continues the reverse rollback: V02's device
            # commit is recognized (deduped), then V01 is restored.
            s = api.post("/api/switches", json=body).json()
            assert s["phase"] == "COMPENSATED"
            assert [v["current_opening"] for v in s["valves"]] == [1, 2, 3, 4]

            acts = api.get(
                "/api/devices/executed-actions",
                params={"operation_id": "op-crash-comp"},
            ).json()
            comp = [(a["valve_id"], a["opening"])
                    for a in acts if a["phase"] == "COMPENSATE"]
            assert comp == [("V02", 2), ("V01", 1)]  # reverse order, once each
            # Each restoration was the conditional change 1 -> 2 (based on
            # the revision its own forward receipt recorded).
            assert [(a["expected_revision"], a["actual_revision"])
                    for a in acts if a["phase"] == "COMPENSATE"] == [(1, 2)] * 2


def test_crash_then_fence_restart_reconciles_and_never_overwrites(tmp_path):
    """Full fence scenario across a real process crash:

    1. process A dies after the device committed V02's FORWARD (receipt lost)
    2. process B confirms a DIFFERENT switch that advances V02's revision
    3. op-x resumes: V02's committed change is recognized from the device
       log (op id + revision range) and not re-applied; V03's injected
       reject drives compensation, which fences on V02 instead of reverting
       the other switch's confirmed opening.
    """
    port = _free_port()
    body = _payload("op-crash-fence")

    # ---- first process: crash right after V02 FORWARD commits on device
    with server(tmp_path, port,
                crash_after="op-crash-fence:V02:FORWARD") as proc:
        _wait_healthy(port)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as api:
            api.post("/api/test/failures",
                     json={"forward": ["V03"], "compensate": []})
            with pytest.raises(httpx.HTTPError):
                api.post("/api/switches", json=body)
        assert _wait_dead(proc) == CRASH_EXIT_CODE

    # ---- console restarts: same durable data, no crash marker
    with server(tmp_path, port) as _:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as api:
            s0 = api.get("/api/switches/op-crash-fence").json()
            assert s0["phase"] == "EXECUTING"
            by_id = {v["valve_id"]: v for v in s0["valves"]}
            assert by_id["V02"]["forward"] == "PENDING"  # receipt was lost

            # Another console confirms V02 52 -> 70 on top of revision 1.
            other = {
                "operation_id": "op-other-console",
                "valves": [
                    {"valve_id": "V02", "initial_opening": 52,
                     "target_opening": 70, "expected_revision": 1},
                    {"valve_id": "V05", "initial_opening": 5,
                     "target_opening": 55, "expected_revision": 0},
                ],
            }
            r_other = api.post("/api/switches", json=other)
            assert r_other.status_code == 201
            assert r_other.json()["phase"] == "COMPLETED"
            mid = {v["valve_id"]: (v["opening"], v["revision"])
                   for v in api.get("/api/devices/valves").json()}
            assert mid["V02"] == (70, 2)

            # Re-submitting op-x resumes it: V02's committed change is
            # recognized (not re-driven), V03 rejects (failure persisted in
            # the device db), and the compensation fences on V02.
            r = api.post("/api/switches", json=body)
            assert r.status_code == 200
            s = r.json()
            assert s["phase"] == "REVISION_CONFLICT"
            conflict = s["conflict"]
            assert conflict["valve_id"] == "V02"
            assert conflict["phase"] == "COMPENSATE"
            assert conflict["expected_revision"] == 1
            assert conflict["actual_revision"] == 2
            assert [(u["valve_id"], u["phase"])
                    for u in conflict["unexecuted"]] == [
                ("V02", "COMPENSATE"), ("V01", "COMPENSATE"),
            ]
            by_id = {v["valve_id"]: v for v in s["valves"]}
            assert by_id["V02"]["forward"] == "SUCCESS"   # recognized receipt
            assert by_id["V02"]["compensate"] == "FENCED"
            assert by_id["V03"]["forward"] == "FAILED"
            assert by_id["V04"]["forward"] == "SKIPPED"

            # The other console's confirmed opening was NOT reverted, and
            # op-x's resume never advanced V02's revision again.
            final = {v["valve_id"]: (v["opening"], v["revision"])
                     for v in api.get("/api/devices/valves").json()}
            assert final["V02"] == (70, 2)
            assert final["V01"] == (51, 1)  # restoration never executed

            acts = api.get(
                "/api/devices/executed-actions",
                params={"operation_id": "op-crash-fence"},
            ).json()
            forward = [a for a in acts if a["phase"] == "FORWARD"]
            # V02 committed exactly once, as the conditional change 0 -> 1.
            assert [(a["valve_id"], a["expected_revision"],
                     a["actual_revision"]) for a in forward] == [
                ("V01", 0, 1), ("V02", 0, 1),
            ]
            assert [a for a in acts if a["phase"] == "COMPENSATE"] == []

            # Same-id retransmission only returns the stored conclusion.
            again = api.post("/api/switches", json=body)
            assert again.status_code == 200
            assert again.json()["phase"] == "REVISION_CONFLICT"
            assert again.json()["conflict"] == conflict
            still = {v["valve_id"]: (v["opening"], v["revision"])
                     for v in api.get("/api/devices/valves").json()}
            assert still["V02"] == (70, 2)
