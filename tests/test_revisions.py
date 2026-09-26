"""Revision-fence tests: optimistic concurrency between two consoles.

Two engineers load the same valve bank on different consoles (same server
confirmed revisions) and submit switches.  The service must never let an
earlier compensation silently revert another confirmed switch's opening:
stale snapshots and stale compensations converge to the queryable
``REVISION_CONFLICT`` conclusion instead of overwriting field values.
"""
from __future__ import annotations

import pytest

from app.models import SwitchRequest, ValveSpec
from app.saga import PayloadConflict

from conftest import make_request


def _request(op: str, specs, with_revisions: dict | None = None) -> SwitchRequest:
    """specs: list of (valve_id, initial, target); with_revisions maps
    valve_id -> expected_revision (None everywhere = legacy request)."""
    with_revisions = with_revisions or {}
    return SwitchRequest(
        operation_id=op,
        valves=[
            ValveSpec(
                valve_id=vid,
                initial_opening=init,
                target_opening=tgt,
                expected_revision=with_revisions.get(vid),
            )
            for vid, init, tgt in specs
        ],
    )


def test_stale_snapshot_forward_fences_and_touches_nothing(engine):
    """Console A and B both loaded revision 0.  A's switch confirms; B's
    stale snapshot must fence instead of overwriting A's openings."""
    eng, store, devices, _ = engine
    specs = [("V01", 1, 51), ("V02", 2, 52)]
    first = eng.submit(_request("op-a", specs, {"V01": 0, "V02": 0}))[0]
    assert first.phase == "COMPLETED"
    assert {v.valve_id: v.revision for v in devices.list_valves()} == {
        "V01": 1, "V02": 1,
    }

    # B submits the same snapshot (revision 0) — now stale.
    stale = eng.submit(_request("op-b", [("V01", 1, 70), ("V02", 2, 70)],
                                {"V01": 0, "V02": 0}))[0]

    assert stale.phase == "REVISION_CONFLICT"
    assert stale.terminal and not stale.success and not stale.resumable
    # The fence is queryable: valve, expected, actual, unexecuted actions.
    conflict = stale.conflict
    assert conflict is not None
    assert conflict.valve_id == "V01"
    assert conflict.phase == "FORWARD"
    assert conflict.expected_revision == 0
    assert conflict.actual_revision == 1
    assert [(u.valve_id, u.phase, u.intended_opening)
            for u in conflict.unexecuted] == [
        ("V01", "FORWARD", 70), ("V02", "FORWARD", 70),
    ]
    by_id = {v.valve_id: v for v in stale.valves}
    assert by_id["V01"].forward == "FENCED"
    assert by_id["V02"].forward == "SKIPPED"
    # Field values untouched: A's confirmed openings and revisions stand.
    assert {v.valve_id: (v.opening, v.revision)
            for v in devices.list_valves()} == {
        "V01": (51, 1), "V02": (52, 1),
    }
    assert devices.executed_actions("op-b") == []

    # Same-id retransmission only returns the original conclusion.
    replay, created = eng.submit(
        _request("op-b", [("V01", 1, 70), ("V02", 2, 70)],
                 {"V01": 0, "V02": 0}))
    assert created is False
    assert replay.phase == "REVISION_CONFLICT"
    assert replay.conflict == conflict
    assert devices.executed_actions("op-b") == []

    # B refreshes the snapshot and retries with a new operation id.
    fresh = eng.submit(_request("op-b2", [("V01", 51, 70), ("V02", 52, 70)],
                                {"V01": 1, "V02": 1}))[0]
    assert fresh.phase == "COMPLETED"
    assert {v.valve_id: (v.opening, v.revision)
            for v in devices.list_valves()} == {
        "V01": (70, 2), "V02": (70, 2),
    }


def test_fence_mid_switch_keeps_confirmed_actions_explicit(engine):
    """The fence stops the switch where it stands: actions already confirmed
    by the device remain (with receipts), the rest are listed unexecuted."""
    eng, store, devices, _ = engine
    eng.submit(_request("op-a", [("V01", 1, 51), ("V02", 2, 52)],
                        {"V01": 0, "V02": 0}))

    # V01's snapshot is fresh (rev 1), V02's is stale (rev 0).
    status = eng.submit(_request("op-b", [("V01", 51, 80), ("V02", 2, 90)],
                                 {"V01": 1, "V02": 0}))[0]

    assert status.phase == "REVISION_CONFLICT"
    assert status.conflict.valve_id == "V02"
    assert (status.conflict.expected_revision,
            status.conflict.actual_revision) == (0, 1)
    assert [(u.valve_id, u.phase) for u in status.conflict.unexecuted] == [
        ("V02", "FORWARD"),
    ]
    by_id = {v.valve_id: v for v in status.valves}
    # V01's own conditional change was confirmed (rev 1 -> 2) and stays.
    assert by_id["V01"].forward == "SUCCESS"
    assert by_id["V01"].forward_actual_revision == 2
    assert by_id["V01"].current_opening == 80
    assert by_id["V02"].forward == "FENCED"
    assert by_id["V02"].current_opening == 52  # op-a's value, not overwritten
    assert {v.valve_id: (v.opening, v.revision)
            for v in devices.list_valves()} == {
        "V01": (80, 2), "V02": (52, 1),
    }


def test_compensation_fence_never_reverts_confirmed_opening(engine):
    """The core guarantee: an earlier switch's compensation must not silently
    revert the opening another console's switch already confirmed."""
    eng, store, devices, _ = engine
    # S1: V03 rejected; V02's restoration hits an injected network failure.
    devices.set_failures(forward=["V03"], compensate=["V02"])
    s1 = eng.submit(make_request("op-s1", n=3))[0]
    assert s1.phase == "COMPENSATION_FAILED"
    # V01 and V02 moved (rev 0 -> 1) and are still at their targets.
    assert {v.valve_id: (v.opening, v.revision)
            for v in devices.list_valves()} == {
        "V01": (61, 1), "V02": (62, 1), "V03": (3, 0),
    }

    # S2 (other console) confirms V01 61 -> 80 on top of revision 1.
    s2 = eng.submit(_request("op-s2", [("V01", 61, 80), ("V09", 9, 19)],
                             {"V01": 1, "V09": 0}))[0]
    assert s2.phase == "COMPLETED"
    assert {v.valve_id: v.revision for v in devices.list_valves()}["V01"] == 2

    # Fault clears; S1 resumes.  V02 restores fine (its revision still
    # matches), then V01's compensation fences: revision 2, not 1.
    devices.set_failures(forward=["V03"], compensate=[])
    resumed = eng.resume("op-s1")

    assert resumed.phase == "REVISION_CONFLICT"
    conflict = resumed.conflict
    assert conflict.valve_id == "V01" and conflict.phase == "COMPENSATE"
    assert (conflict.expected_revision, conflict.actual_revision) == (1, 2)
    assert [(u.valve_id, u.phase, u.intended_opening)
            for u in conflict.unexecuted] == [("V01", "COMPENSATE", 1)]
    by_id = {v.valve_id: v for v in resumed.valves}
    assert by_id["V02"].compensate == "SUCCESS"      # restored 62 -> 2
    assert by_id["V02"].compensate_expected_revision == 1
    assert by_id["V02"].compensate_actual_revision == 2
    assert by_id["V01"].compensate == "FENCED"
    # The confirmed opening of the other switch was NOT reverted.
    assert by_id["V01"].current_opening == 80
    assert {v.valve_id: (v.opening, v.revision)
            for v in devices.list_valves()} == {
        "V01": (80, 2), "V02": (2, 2), "V03": (3, 0), "V09": (19, 1),
    }

    # Same-id retransmission returns the stored conclusion; nothing moves.
    replay, created = eng.submit(make_request("op-s1", n=3))
    assert created is False
    assert replay.phase == "REVISION_CONFLICT"
    assert replay.conflict == conflict
    assert {v.valve_id: v.opening for v in devices.list_valves()}["V01"] == 80


def test_legacy_request_without_revisions_uses_acceptance_snapshot(engine):
    """Old pages omit the snapshot: the server snapshots revisions at
    acceptance time, so legacy requests keep working and remain replayable."""
    eng, store, devices, _ = engine
    req = make_request("op-legacy", n=2)  # no expected_revision fields
    assert all(v.expected_revision is None for v in req.valves)

    first = eng.submit(req)[0]
    assert first.phase == "COMPLETED"
    rec = store.get("op-legacy")
    # Snapshot taken at acceptance: both valves were at revision 0.
    assert [a.expected_revision for a in rec.actions] == [0, 0]
    assert [a.forward_actual_revision for a in rec.actions] == [1, 1]

    # A second legacy request on the same valves snapshots the CURRENT
    # revisions (1) at acceptance and therefore also completes.
    second = eng.submit(make_request("op-legacy2", n=2, target=70))[0]
    assert second.phase == "COMPLETED"
    rec2 = store.get("op-legacy2")
    assert [a.expected_revision for a in rec2.actions] == [1, 1]
    assert {v.valve_id: v.revision for v in devices.list_valves()} == {
        "V01": 2, "V02": 2,
    }


def test_same_id_with_different_snapshot_conflicts_409(engine):
    """Same operation id, same valves but a different revision snapshot is a
    different payload: rejected without touching any device."""
    eng, store, devices, _ = engine
    eng.submit(_request("op-x", [("V01", 1, 51), ("V02", 2, 52)],
                        {"V01": 0, "V02": 0}))
    before = devices.executed_actions()

    with pytest.raises(PayloadConflict):
        eng.submit(_request("op-x", [("V01", 1, 51), ("V02", 2, 52)],
                            {"V01": 1, "V02": 1}))
    assert devices.executed_actions() == before


def test_restart_reconciles_device_log_by_op_and_revision_range(engine):
    """Crash gap with a twist: while the console was down, another confirmed
    switch advanced V02.  Recovery must recognize V02's conditional change
    from the device log (op id + revision range) without re-advancing it,
    and must not disturb the other switch's confirmed opening."""
    eng, store, devices, _ = engine
    req = make_request("op-gap", n=3)

    from app.store import ActionRecord
    rec = store.create_intent(
        "op-gap",
        {"payload_key": "[]"},
        [ActionRecord(valve_id=v.valve_id, idx=i,
                      initial_opening=v.initial_opening,
                      target_opening=v.target_opening)
         for i, v in enumerate(req.valves)],
    )
    # V01 done + receipted; V02 committed on the device, receipt lost.
    devices.execute("op-gap", "V01", claimed_initial=1, new_opening=61,
                    phase="FORWARD", expected_revision=0)
    a01 = rec.actions[0]
    a01.forward_status = "SUCCESS"
    a01.forward_opening = 61
    a01.forward_actual_revision = 1
    store.mark_forward("op-gap", a01)
    devices.execute("op-gap", "V02", claimed_initial=2, new_opening=62,
                    phase="FORWARD", expected_revision=0)

    # Another console confirms V02 62 -> 90 while op-gap is down.
    other = eng.submit(_request("op-other", [("V02", 62, 90), ("V07", 7, 17)],
                                {"V02": 1, "V07": 0}))[0]
    assert other.phase == "COMPLETED"
    assert {v.valve_id: v.revision
            for v in devices.list_valves()}["V02"] == 2

    # Fresh engine instance, same durable stores == console restart.
    restarted = type(eng)(store, devices)
    status = restarted.resume("op-gap")

    assert status.phase == "COMPLETED"
    persisted = {a.valve_id: a for a in store.get("op-gap").actions}
    # V02 recognized from the device log: deduped, revision range 0 -> 1
    # kept, NOT advanced again (V02 is at revision 2 thanks to op-other).
    assert persisted["V02"].forward_deduped is True
    assert persisted["V02"].forward_actual_revision == 1
    assert {v.valve_id: v.revision for v in devices.list_valves()}["V02"] == 2
    # The other switch's confirmed opening stands.
    assert {v.valve_id: v.opening for v in devices.list_valves()}["V02"] == 90
    # Device log: exactly one conditional change per (op, valve, phase).
    log = [(a.valve_id, a.expected_revision, a.actual_revision)
           for a in devices.executed_actions("op-gap")
           if a.phase == "FORWARD"]
    assert log == [("V01", 0, 1), ("V02", 0, 1), ("V03", 0, 1)]
