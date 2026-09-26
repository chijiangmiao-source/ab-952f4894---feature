#!/usr/bin/env python3
"""HTTP smoke test for the valve-bank switch service.

Verifies against a running server:
  1. health check
  2. forward failure -> reverse-order compensation -> COMPENSATED
  3. idempotent replay returns the same result
  4. same operation_id with a changed payload -> 409, devices untouched
  5. executed-action query endpoint (with revision ranges)
  6. revision fences: stale snapshot rejected, late compensation fenced
     (another console's confirmed opening is never silently reverted)
  7. the web console page exposes the revision snapshot UI

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


def snap_payload(op: str, specs: list[tuple]) -> dict:
    """specs: (valve_id, initial, target, expected_revision)."""
    return {
        "operation_id": op,
        "valves": [
            {"valve_id": vid, "initial_opening": init, "target_opening": tgt,
             "expected_revision": rev}
            for vid, init, tgt, rev in specs
        ],
    }


def valve_map(api: httpx.Client) -> dict:
    return {v["valve_id"]: v for v in api.get("/api/devices/valves").json()}


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
        check("executed actions carry revision ranges",
              all("expected_revision" in a and "actual_revision" in a
                  for a in acts), str(acts))

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
        cur = valve_map(api)
        check("409 did not touch any device",
              {k: v["opening"] for k, v in cur.items()} ==
              {"V01": 51, "V02": 52, "V03": 53, "V04": 54}, str(cur))
        check("valves endpoint exposes confirmed revisions",
              all(v["revision"] == 1 for v in cur.values()), str(cur))

        r4 = api.get("/api/devices/executed-actions",
                     params={"operation_id": "smoke-ok"})
        fw = [(a["valve_id"], a["phase"]) for a in r4.json()]
        check("executed-actions queryable",
              r4.status_code == 200 and len(fw) == 4, str(fw))

        # ---- revision fence 1: stale snapshot rejected, nothing overwritten
        api.post("/api/test/reset")
        r = api.post("/api/switches",
                     json=snap_payload("smoke-rev-a",
                                       [("V01", 1, 51, 0), ("V02", 2, 52, 0)]))
        check("console A switch -> COMPLETED",
              r.status_code == 201 and r.json()["phase"] == "COMPLETED",
              r.text)

        stale = snap_payload("smoke-rev-b",
                             [("V01", 1, 70, 0), ("V02", 2, 70, 0)])
        r = api.post("/api/switches", json=stale)
        s = r.json()
        check("stale snapshot -> 201 REVISION_CONFLICT",
              r.status_code == 201 and s["phase"] == "REVISION_CONFLICT",
              f"{r.status_code} {r.text}")
        cf = s["conflict"]
        check("fence lists valve/expected/actual",
              cf["valve_id"] == "V01" and cf["expected_revision"] == 0
              and cf["actual_revision"] == 1, str(cf))
        check("fence lists unexecuted actions",
              [(u["valve_id"], u["phase"]) for u in cf["unexecuted"]] ==
              [("V01", "FORWARD"), ("V02", "FORWARD")], str(cf["unexecuted"]))
        cur = valve_map(api)
        check("stale switch overwrote nothing",
              {k: (v["opening"], v["revision"]) for k, v in cur.items()} ==
              {"V01": (51, 1), "V02": (52, 1)}, str(cur))
        r = api.post("/api/switches", json=stale)
        check("same-id retransmission returns original conclusion",
              r.status_code == 200
              and r.json()["phase"] == "REVISION_CONFLICT"
              and r.json()["conflict"] == cf, r.text)
        r = api.post("/api/switches",
                     json=snap_payload("smoke-rev-b2",
                                       [("V01", 51, 70, 1), ("V02", 52, 70, 1)]))
        check("fresh snapshot with new id -> COMPLETED",
              r.status_code == 201 and r.json()["phase"] == "COMPLETED",
              r.text)

        # ---- revision fence 2: late compensation must not revert a
        #      confirmed opening of another console's switch
        api.post("/api/test/reset")
        api.post("/api/test/failures",
                 json={"forward": ["V03"], "compensate": ["V02"]})
        s1 = api.post("/api/switches", json=payload("smoke-fence-1", n=3)).json()
        check("switch 1 stuck in COMPENSATION_FAILED",
              s1["phase"] == "COMPENSATION_FAILED", str(s1))
        r = api.post("/api/switches",
                     json=snap_payload("smoke-fence-2",
                                       [("V01", 51, 80, 1), ("V09", 9, 19, 0)]))
        check("console 2 confirms V01 51 -> 80",
              r.status_code == 201 and r.json()["phase"] == "COMPLETED",
              r.text)
        api.post("/api/test/failures",
                 json={"forward": ["V03"], "compensate": []})
        s = api.post("/api/switches/smoke-fence-1/resume").json()
        check("resumed compensation hits revision fence",
              s["phase"] == "REVISION_CONFLICT", str(s))
        cf = s["conflict"]
        check("fence is the COMPENSATE of V01 (expected 1, actual 2)",
              cf["valve_id"] == "V01" and cf["phase"] == "COMPENSATE"
              and cf["expected_revision"] == 1 and cf["actual_revision"] == 2,
              str(cf))
        cur = valve_map(api)
        check("confirmed opening 80 NOT reverted; V02 restored",
              cur["V01"]["opening"] == 80 and cur["V02"]["opening"] == 2,
              str(cur))

        # ---- web console exposes the revision snapshot UI
        page = api.get("/")
        check("web console page served",
              page.status_code == 200 and "阀组切换" in page.text,
              str(page.status_code))
        check("web console shows server-confirmed revisions",
              "修订号" in page.text and "expected_revision" in page.text,
              "revision UI markers missing")
        check("web console renders revision-fence conclusions",
              "REVISION_CONFLICT" in page.text and "修订栅栏冲突" in page.text,
              "fence UI markers missing")

    print("HTTP smoke: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://web:8080"
    sys.exit(main(base.rstrip("/")))
