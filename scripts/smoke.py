#!/usr/bin/env python3
"""HTTP smoke test for the valve-bank switch service.

Verifies against a running server:
  1. health check
  2. forward failure -> reverse-order compensation -> COMPENSATED
  3. idempotent replay returns the same result
  4. same operation_id with a changed payload -> 409, devices untouched
  5. executed-action query endpoint

Exits 0 on success, 1 on any failure.
"""
from __future__ import annotations

import sys

import httpx


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        raise SystemExit(1)


def payload(op: str, n: int = 4) -> dict:
    return {
        "operation_id": op,
        "valves": [
            {"valve_id": f"V{i:02d}", "initial_opening": i,
             "target_opening": 50 + i}
            for i in range(1, n + 1)
        ],
    }


def main(base_url: str) -> int:
    print(f"HTTP smoke against {base_url}")
    with httpx.Client(base_url=base_url, timeout=10) as api:
        r = api.get("/health")
        check("GET /health -> 200 healthy",
              r.status_code == 200 and r.json().get("status") == "healthy",
              r.text)

        check("test reset available",
              api.post("/api/test/reset").status_code == 200)

        # ---- reverse compensation on a device rejection
        r = api.post("/api/test/failures",
                     json={"forward": ["V03"], "compensate": []})
        check("inject forward failure on V03", r.status_code == 200, r.text)

        r = api.post("/api/switches", json=payload("smoke-comp"))
        check("rejected switch -> 201", r.status_code == 201, r.text)
        s = r.json()
        check("phase COMPENSATED", s["phase"] == "COMPENSATED", s["phase"])
        by_id = {v["valve_id"]: v for v in s["valves"]}
        check("V03 FAILED / V04 SKIPPED",
              by_id["V03"]["forward"] == "FAILED"
              and by_id["V04"]["forward"] == "SKIPPED")
        check("all valves back at initial openings",
              [v["current_opening"] for v in s["valves"]] == [1, 2, 3, 4],
              str([v["current_opening"] for v in s["valves"]]))

        acts = api.get("/api/devices/executed-actions",
                       params={"operation_id": "smoke-comp"}).json()
        comp = [a["valve_id"] for a in acts if a["phase"] == "COMPENSATE"]
        check("compensation order is reverse (V02 then V01)",
              comp == ["V02", "V01"], str(comp))

        # ---- happy path, idempotency, conflict
        api.post("/api/test/reset")
        body = payload("smoke-ok")
        r1 = api.post("/api/switches", json=body)
        check("happy switch -> 201 COMPLETED",
              r1.status_code == 201 and r1.json()["phase"] == "COMPLETED",
              r1.text)

        r2 = api.post("/api/switches", json=body)
        check("same operation_id replay -> 200 same result",
              r2.status_code == 200 and r2.json()["phase"] == "COMPLETED",
              f"{r2.status_code} {r2.text}")

        conflicting = payload("smoke-ok")
        conflicting["valves"][0]["target_opening"] = 99
        r3 = api.post("/api/switches", json=conflicting)
        check("changed payload -> 409", r3.status_code == 409, r3.text)
        cur = {v["valve_id"]: v["opening"]
               for v in api.get("/api/devices/valves").json()}
        check("409 did not touch any device",
              cur == {"V01": 51, "V02": 52, "V03": 53, "V04": 54}, str(cur))

        r4 = api.get("/api/devices/executed-actions",
                     params={"operation_id": "smoke-ok"})
        fw = [(a["valve_id"], a["phase"]) for a in r4.json()]
        check("executed-actions queryable",
              r4.status_code == 200 and len(fw) == 4, str(fw))

    print("HTTP smoke: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://web:8080"
    sys.exit(main(base.rstrip("/")))
