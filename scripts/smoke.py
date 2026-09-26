#!/usr/bin/env python3
"""HTTP smoke test for the valve-bank switch service.

Verifies against a running server:
  1. health check and the console page (revision snapshot UI)
  2. forward failure -> reverse-order compensation -> COMPENSATED
  3. idempotent replay returns the same result
  4. same operation_id with a changed payload -> 409, devices untouched
  5. executed-action query endpoint (with revision intervals)
  6. stale snapshot -> REVISION_CONFLICT, field values untouched
  7. resumed compensation hits the revision fence and never reverts
     another switch's confirmed opening

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


def payload(op: str, n: int = 4, revisions: dict | None = None) -> dict:
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


def main(base_url: str) -> int:
    print(f"HTTP smoke against {base_url}")
    with httpx.Client(base_url=base_url, timeout=10) as api:
        r = api.get("/health")
        check("GET /health -> 200 healthy",
              r.status_code == 200 and r.json().get("status") == "healthy",
              r.text)

        page = api.get("/")
        check("console page served with revision snapshot UI",
              page.status_code == 200 and "修订号" in page.text
              and "REVISION_CONFLICT" in page.text)

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
        comp_rev = [(a["expected_revision"], a["actual_revision"])
                    for a in acts if a["phase"] == "COMPENSATE"]
        check("compensation receipts record revision interval 1 -> 2",
              comp_rev == [(1, 2), (1, 2)], str(comp_rev))

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

        valves = api.get("/api/devices/valves").json()
        check("device valves publish confirmed revisions",
              all(v["revision"] == 1 for v in valves), str(valves))

        # ---- stale snapshot: the second console is fenced off
        api.post("/api/test/reset")
        stale = {f"V{i:02d}": 0 for i in range(1, 5)}  # snapshot both consoles hold
        r = api.post("/api/switches", json=payload("smoke-e1", revisions=stale))
        check("first console switch -> COMPLETED",
              r.status_code == 201 and r.json()["phase"] == "COMPLETED", r.text)

        r = api.post("/api/switches", json=payload("smoke-e2", revisions=stale))
        check("stale snapshot -> 201 REVISION_CONFLICT",
              r.status_code == 201 and r.json()["phase"] == "REVISION_CONFLICT",
              f"{r.status_code} {r.text}")
        s = r.json()
        check("conflict lists valve/expected/actual",
              [(c["valve_id"], c["phase"], c["expected_revision"],
                c["actual_revision"]) for c in s["conflicts"]]
              == [("V01", "FORWARD", 0, 1)], str(s["conflicts"]))
        check("unexecuted actions listed",
              [(u["valve_id"], u["phase"]) for u in s["unexecuted"]]
              == [("V02", "FORWARD"), ("V03", "FORWARD"), ("V04", "FORWARD")],
              str(s["unexecuted"]))
        cur = {v["valve_id"]: (v["opening"], v["revision"])
               for v in api.get("/api/devices/valves").json()}
        check("fenced switch did not overwrite confirmed openings",
              cur == {"V01": (51, 1), "V02": (52, 1),
                      "V03": (53, 1), "V04": (54, 1)}, str(cur))
        r = api.post("/api/switches", json=payload("smoke-e2", revisions=stale))
        check("same-id retransmission returns the original conclusion",
              r.status_code == 200
              and r.json()["phase"] == "REVISION_CONFLICT"
              and r.json()["conflicts"] == s["conflicts"],
              f"{r.status_code} {r.text}")

        # ---- compensation hits the fence: earlier compensation must not
        #      revert another switch's confirmed opening
        api.post("/api/test/reset")
        api.post("/api/test/failures",
                 json={"forward": ["V03"], "compensate": ["V02"]})
        r = api.post("/api/switches", json=payload("smoke-s2"))
        check("stuck switch -> COMPENSATION_FAILED",
              r.status_code == 201 and r.json()["phase"] == "COMPENSATION_FAILED",
              r.text)

        other = {
            "operation_id": "smoke-s1",
            "valves": [
                {"valve_id": "V01", "initial_opening": 51, "target_opening": 71},
                {"valve_id": "V02", "initial_opening": 52, "target_opening": 72},
            ],
            "revisions": {"V01": 1, "V02": 1},  # fresh snapshot of the bank
        }
        r = api.post("/api/switches", json=other)
        check("second console confirms on same valves -> COMPLETED",
              r.status_code == 201 and r.json()["phase"] == "COMPLETED", r.text)

        api.post("/api/test/failures",
                 json={"forward": ["V03"], "compensate": []})
        r = api.post("/api/switches/smoke-s2/resume")
        check("resumed compensation -> REVISION_CONFLICT",
              r.status_code == 200 and r.json()["phase"] == "REVISION_CONFLICT",
              f"{r.status_code} {r.text}")
        s = r.json()
        check("fence names the compensated valve and revisions",
              [(c["valve_id"], c["phase"], c["expected_revision"],
                c["actual_revision"]) for c in s["conflicts"]]
              == [("V02", "COMPENSATE", 1, 2)], str(s["conflicts"]))
        check("remaining restoration listed as unexecuted",
              ("V01", "COMPENSATE") in [(u["valve_id"], u["phase"])
                                        for u in s["unexecuted"]],
              str(s["unexecuted"]))
        cur = {v["valve_id"]: (v["opening"], v["revision"])
               for v in api.get("/api/devices/valves").json()}
        check("other switch's confirmed openings NOT reverted",
              cur.get("V01") == (71, 2) and cur.get("V02") == (72, 2), str(cur))
        r = api.post("/api/switches", json=payload("smoke-s2"))
        check("replay of fenced switch returns stored conclusion",
              r.status_code == 200
              and r.json()["phase"] == "REVISION_CONFLICT",
              f"{r.status_code} {r.text}")

        # leave the service clean for the next run
        api.post("/api/test/reset")

    print("HTTP smoke: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://web:8080"
    sys.exit(main(base.rstrip("/")))
