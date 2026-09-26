"""Saga engine unit tests: ordering, compensation, idempotency, recovery."""
from __future__ import annotations

import pytest

from app.models import SwitchRequest, ValveSpec
from app.saga import PayloadConflict

from conftest import make_request


def test_happy_path_completes(engine):
    eng, store, devices, _ = engine
    status = eng.submit(make_request("op-ok", n=4))[0]

    assert status.phase == "COMPLETED"
    assert status.success and status.terminal
    assert {v.valve_id: v.current_opening for v in status.valves} == {
        f"V{i:02d}": 60 + i for i in range(1, 5)
    }
    # Intent + one durable action row per valve persisted.
    rec = store.get("op-ok")
    assert rec is not None and len(rec.actions) == 4
    assert all(a.forward_status == "SUCCESS" for a in rec.actions)
    # Every conditional change advanced the revision exactly 0 -> 1.
    assert {v.valve_id: v.revision for v in devices.list_valves()} == {
        f"V{i:02d}": 1 for i in range(1, 5)
    }
    for a in devices.executed_actions("op-ok"):
        assert (a.expected_revision, a.actual_revision) == (0, 1)


def test_forward_failure_triggers_reverse_compensation(engine):
    eng, store, devices, _ = engine
    devices.set_failures(forward=["V03"], compensate=[])

    status = eng.submit(make_request("op-fail", n=4))[0]

    assert status.phase == "COMPENSATED"
    assert not status.success
    by_id = {v.valve_id: v for v in status.valves}
    assert by_id["V01"].forward == "SUCCESS"
    assert by_id["V02"].forward == "SUCCESS"
    assert by_id["V03"].forward == "FAILED"
    assert by_id["V04"].forward == "SKIPPED"
    # The two changed valves restored to their original openings.
    assert by_id["V01"].current_opening == 1   # initial was 0+1
    assert by_id["V02"].current_opening == 2
    # V03/V04 never moved.
    assert by_id["V03"].current_opening == 3
    assert by_id["V04"].current_opening == 4

    # Compensation happened in REVERSE success order: V02 then V01.
    comp = [a for a in devices.executed_actions("op-fail")
            if a.phase == "COMPENSATE"]
    assert [a.valve_id for a in comp] == ["V02", "V01"]
    assert all(a.opening == {"V01": 1, "V02": 2}[a.valve_id] for a in comp)
    # Forward bumped each valve 0 -> 1; compensation bumped 1 -> 2.
    # (V04 was never attempted, so the device never registered it.)
    assert {v.valve_id: v.revision for v in devices.list_valves()} == {
        "V01": 2, "V02": 2, "V03": 0
    }


def test_idempotent_replay_returns_same_result_and_does_not_re_act(engine):
    eng, store, devices, _ = engine
    req = make_request("op-replay", n=3)
    first = eng.submit(req)[0]
    actions_before = devices.executed_actions("op-replay")
    openings_before = {v.valve_id: v.opening for v in devices.list_valves()}

    second, created = eng.submit(req)

    assert created is False
    assert second.phase == first.phase == "COMPLETED"
    assert devices.executed_actions("op-replay") == actions_before
    assert {v.valve_id: v.opening for v in devices.list_valves()} == openings_before


def test_same_id_different_payload_conflicts_and_touches_nothing(engine):
    eng, store, devices, _ = engine
    eng.submit(make_request("op-x", n=3, target=60))
    actions_before = devices.executed_actions()

    changed = SwitchRequest(
        operation_id="op-x",
        valves=[
            ValveSpec(valve_id=f"V{i:02d}", initial_opening=i, target_opening=90)
            for i in range(1, 4)
        ],
    )
    with pytest.raises(PayloadConflict):
        eng.submit(changed)

    # No new device activity whatsoever.
    assert devices.executed_actions() == actions_before
    # Original result still intact.
    assert eng.status("op-x").phase == "COMPLETED"


def test_same_id_different_snapshot_conflicts_and_touches_nothing(engine):
    eng, store, devices, _ = engine
    req = make_request("op-snap", n=3, revisions={"V01": 0, "V02": 0, "V03": 0})
    assert eng.submit(req)[0].phase == "COMPLETED"
    actions_before = devices.executed_actions()

    stale = make_request("op-snap", n=3, revisions={"V01": 0, "V02": 0, "V03": 0})
    # Same id, same valves, but a *different* snapshot is a different intent.
    stale.revisions = {"V01": 1, "V02": 1, "V03": 1}
    with pytest.raises(PayloadConflict):
        eng.submit(stale)
    assert devices.executed_actions() == actions_before


def test_compensation_failure_leaves_resumable_state(engine):
    eng, store, devices, _ = engine
    # V02 must be restored (reverse order: V02 first) but its restore fails.
    devices.set_failures(forward=["V03"], compensate=["V02"])

    status = eng.submit(make_request("op-cf", n=4))[0]

    assert status.phase == "COMPENSATION_FAILED"
    assert status.terminal and status.resumable
    by_id = {v.valve_id: v for v in status.valves}
    assert by_id["V02"].compensate == "FAILED"
    assert by_id["V01"].compensate == "PENDING"  # not reached, still to restore
    assert by_id["V02"].current_opening == 62    # still sitting at target
    assert "network failure" in (by_id["V02"].error or "")

    # Operator clears the network fault; resume continues from V02, then V01.
    devices.set_failures(forward=["V03"], compensate=[])
    resumed = eng.resume("op-cf")
    assert resumed.phase == "COMPENSATED"
    by_id = {v.valve_id: v for v in resumed.valves}
    assert by_id["V02"].current_opening == 2
    assert by_id["V01"].current_opening == 1
    assert by_id["V02"].compensate == "SUCCESS"
    assert by_id["V01"].compensate == "SUCCESS"


def test_device_commit_before_receipt_is_recognized_on_resume(engine):
    """The exact restart gap: device already committed FORWARD for V02,
    but the application crashed before storing the receipt. Recovery must
    recognize it via (op, phase) dedupe and its revision interval, not move
    the valve again and not advance the revision a second time."""
    eng, store, devices, _ = engine
    req = make_request("op-gap", n=3)

    # Build durable intent; V01 done+receipted; then V02 commits on the
    # device and the console dies before the receipt lands.
    from app.store import ActionRecord
    rec = store.create_intent(
        "op-gap",
        {"payload_key": "[]"},
        [ActionRecord(valve_id=v.valve_id, idx=i,
                      initial_opening=v.initial_opening,
                      target_opening=v.target_opening,
                      forward_expected_revision=0)
         for i, v in enumerate(req.valves)],
    )
    devices.execute("op-gap", "V01", claimed_initial=1, new_opening=61,
                    phase="FORWARD", expected_revision=0)
    a01 = rec.actions[0]
    a01.forward_status = "SUCCESS"
    a01.forward_opening = 61
    a01.forward_expected_revision = 0
    a01.forward_actual_revision = 1
    store.mark_forward("op-gap", a01)
    # V02 commits on the device (revision 0 -> 1) ... and no receipt is written.
    devices.execute("op-gap", "V02", claimed_initial=2, new_opening=62,
                    phase="FORWARD", expected_revision=0)

    # Fresh engine instance, same durable stores == console restart.
    restarted = type(eng)(store, devices)
    status = restarted.resume("op-gap")

    assert status.phase == "COMPLETED"
    persisted = {a.valve_id: a for a in store.get("op-gap").actions}
    assert persisted["V02"].forward_status == "SUCCESS"
    assert persisted["V02"].forward_deduped is True  # recognized, not re-driven
    assert persisted["V02"].forward_expected_revision == 0
    assert persisted["V02"].forward_actual_revision == 1
    # Exactly one FORWARD action per valve on the device.
    forward = [a for a in devices.executed_actions("op-gap")
               if a.phase == "FORWARD"]
    assert [(a.valve_id, a.opening) for a in forward] == [
        ("V01", 61), ("V02", 62), ("V03", 63)
    ]
    # The recognized action did NOT advance the revision again.
    assert {v.valve_id: v.revision for v in devices.list_valves()} == {
        "V01": 1, "V02": 1, "V03": 1
    }


def test_device_rejects_are_persisted_and_safe_to_retry(engine):
    eng, store, devices, _ = engine
    devices.set_failures(forward=["V02"], compensate=[])
    status = eng.submit(make_request("op-rej", n=2))[0]
    assert status.phase == "COMPENSATED"
    by_id = {v.valve_id: v for v in status.valves}
    assert by_id["V02"].forward == "FAILED"
    assert "rejected" in by_id["V02"].error

    # While still broken, replay must not claim success.
    again, _ = eng.submit(make_request("op-rej", n=2))
    assert again.phase == "COMPENSATED"


@pytest.mark.parametrize("n", [1, 9])
def test_valve_count_bounds(engine, n):
    eng, _, _, _ = engine
    with pytest.raises(ValueError):
        eng.submit(make_request("op-bounds", n=n))


# ---------------------------------------------------------------------
# Revision fence: two consoles, one valve bank
# ---------------------------------------------------------------------

def test_stale_snapshot_forward_fence_never_overwrites(engine):
    """Two engineers load the same bank (all revisions 0). E1's switch
    completes; E2 then submits with the now-stale snapshot. The service
    must refuse to overwrite and converge to REVISION_CONFLICT."""
    eng, store, devices, _ = engine
    snap0 = {f"V{i:02d}": 0 for i in range(1, 5)}

    first = eng.submit(make_request("op-e1", n=4, target=50, revisions=snap0))[0]
    assert first.phase == "COMPLETED"
    assert all(v.revision == 1 for v in devices.list_valves())

    # E2's console still holds the r0 snapshot of the same valves.
    stale = eng.submit(make_request("op-e2", n=4, target=70, revisions=snap0))[0]

    assert stale.phase == "REVISION_CONFLICT"
    assert stale.terminal and not stale.success and not stale.resumable
    by_id = {v.valve_id: v for v in stale.valves}
    assert by_id["V01"].forward == "FENCED"
    assert by_id["V01"].forward_expected_revision == 0
    assert by_id["V01"].forward_actual_revision == 1
    assert [v.forward for v in stale.valves[1:]] == ["SKIPPED"] * 3
    # The conflict report names valve, expected, actual and unexecuted work.
    assert [(c.valve_id, c.phase, c.expected_revision, c.actual_revision)
            for c in stale.conflicts] == [("V01", "FORWARD", 0, 1)]
    assert [(u.valve_id, u.phase) for u in stale.unexecuted] == [
        ("V02", "FORWARD"), ("V03", "FORWARD"), ("V04", "FORWARD")
    ]
    # E1's confirmed openings and revisions are untouched.
    assert {v.valve_id: (v.opening, v.revision)
            for v in devices.list_valves()} == {
        "V01": (51, 1), "V02": (52, 1), "V03": (53, 1), "V04": (54, 1),
    }
    # Same-id retransmission returns the original conclusion only.
    replay, created = eng.submit(make_request("op-e2", n=4, target=70,
                                              revisions=snap0))
    assert created is False
    assert replay.phase == "REVISION_CONFLICT"
    assert replay.conflicts == stale.conflicts


def test_forward_fence_rolls_back_own_changes_before_concluding(engine):
    """If the fence is only hit mid-way, valves this switch already moved
    are conditionally restored before the conflict is concluded."""
    eng, store, devices, _ = engine
    # E1 confirms a switch on V03/V04 only.
    eng.submit(SwitchRequest(
        operation_id="op-other",
        valves=[ValveSpec(valve_id="V03", initial_opening=3, target_opening=53),
                ValveSpec(valve_id="V04", initial_opening=4, target_opening=54)],
        revisions={"V03": 0, "V04": 0},
    ))
    # E2's snapshot predates E1 on V03/V04 but is fresh for V01/V02.
    snap = {"V01": 0, "V02": 0, "V03": 0, "V04": 0}
    status = eng.submit(make_request("op-e2b", n=4, target=70, revisions=snap))[0]

    assert status.phase == "REVISION_CONFLICT"
    by_id = {v.valve_id: v for v in status.valves}
    assert by_id["V01"].forward == "SUCCESS"
    assert by_id["V02"].forward == "SUCCESS"
    assert by_id["V03"].forward == "FENCED"
    # Own changes were restored (compensated), E1's valves untouched.
    assert by_id["V01"].compensate == "SUCCESS"
    assert by_id["V02"].compensate == "SUCCESS"
    assert by_id["V01"].current_opening == 1
    assert by_id["V02"].current_opening == 2
    assert by_id["V03"].current_opening == 53
    assert by_id["V04"].current_opening == 54
    assert [(c.valve_id, c.expected_revision, c.actual_revision)
            for c in status.conflicts] == [("V03", 0, 1)]


def test_compensation_fence_never_reverts_other_confirmed_opening(engine):
    """The headline rule: an earlier compensation must not silently revert
    another switch's confirmed opening. S2 changes V01/V02 then gets stuck
    in COMPENSATION_FAILED; S1 confirms new openings on the same valves;
    S2's resumed compensation hits the revision fence and leaves S1's
    values in place."""
    eng, store, devices, _ = engine
    devices.set_failures(forward=["V03"], compensate=["V02"])

    stuck = eng.submit(make_request("op-s2", n=4, target=60))[0]
    assert stuck.phase == "COMPENSATION_FAILED"
    assert {v.valve_id: (v.opening, v.revision)
            for v in devices.list_valves()} == {
        "V01": (61, 1), "V02": (62, 1), "V03": (3, 0),
    }

    # Another console confirms a switch on V01/V02 with a FRESH snapshot.
    other = eng.submit(SwitchRequest(
        operation_id="op-s1",
        valves=[ValveSpec(valve_id="V01", initial_opening=61, target_opening=71),
                ValveSpec(valve_id="V02", initial_opening=62, target_opening=72)],
        revisions={"V01": 1, "V02": 1},
    ))[0]
    assert other.phase == "COMPLETED"
    assert {v.valve_id: (v.opening, v.revision)
            for v in devices.list_valves()}["V01"] == (71, 2)

    # Fault clears; S2 resumes. Its compensation conditions on revision 1,
    # but S1 advanced the valves to revision 2 -> fence, no overwrite.
    devices.set_failures(forward=["V03"], compensate=[])
    final = eng.resume("op-s2")

    assert final.phase == "REVISION_CONFLICT"
    assert final.terminal and not final.resumable
    by_id = {v.valve_id: v for v in final.valves}
    assert by_id["V02"].compensate == "FENCED"
    assert by_id["V02"].compensate_expected_revision == 1
    assert by_id["V02"].compensate_actual_revision == 2
    assert by_id["V01"].compensate == "PENDING"  # unexecuted, listed
    assert [(c.valve_id, c.phase, c.expected_revision, c.actual_revision)
            for c in final.conflicts] == [("V02", "COMPENSATE", 1, 2)]
    assert ("V01", "COMPENSATE") in [(u.valve_id, u.phase)
                                     for u in final.unexecuted]
    # S1's confirmed openings were NOT reverted to S2's recorded initials.
    assert {v.valve_id: (v.opening, v.revision)
            for v in devices.list_valves()} == {
        "V01": (71, 2), "V02": (72, 2), "V03": (3, 0),
    }
    # Same-id replay returns the stored conclusion without device activity.
    before = devices.executed_actions()
    replay, created = eng.submit(make_request("op-s2", n=4, target=60))
    assert created is False and replay.phase == "REVISION_CONFLICT"
    assert devices.executed_actions() == before


def test_legacy_request_without_snapshot_uses_acceptance_time_revisions(engine):
    """Old pages carry no revisions: the service snapshots at acceptance,
    so legacy semantics (and replays) keep working."""
    eng, store, devices, _ = engine
    eng.submit(make_request("op-first", n=2, target=50))  # revisions -> 1

    # A legacy request for the same valves is accepted with a fresh
    # acceptance-time snapshot and completes normally.
    legacy = eng.submit(make_request("op-legacy", n=2, target=80))[0]
    assert legacy.phase == "COMPLETED"
    by_id = {v.valve_id: v for v in legacy.valves}
    assert by_id["V01"].forward_expected_revision == 1
    assert by_id["V01"].forward_actual_revision == 2
    assert by_id["V01"].current_opening == 81

    # Legacy replay of the same payload returns the same result.
    replay, created = eng.submit(make_request("op-legacy", n=2, target=80))
    assert created is False and replay.phase == "COMPLETED"


def test_revisions_must_cover_exactly_the_requested_valves(engine):
    eng, _, devices, _ = engine
    with pytest.raises(ValueError, match="revisions"):
        make_request("op-badrev", n=3, revisions={"V01": 0, "V02": 0})
    with pytest.raises(ValueError, match="revisions"):
        make_request("op-badrev2", n=2,
                     revisions={"V01": 0, "V02": 0, "V09": 0})
    with pytest.raises(ValueError, match="non-negative"):
        make_request("op-badrev3", n=2, revisions={"V01": 0, "V02": -1})
    assert devices.executed_actions() == []
