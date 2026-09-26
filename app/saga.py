"""Saga engine for a multi-valve switch.

Rules implemented here:

* Valves move in the request order; the FIRST failing forward action stops
  the forward pass.
* Every device action is a *conditional* change on the valve's revision:
  the request's snapshot (operator-supplied, or taken at acceptance time
  for legacy clients) decides the expected revision of each forward
  action; each compensation conditions on the revision this switch's own
  forward action produced.
* If a forward or reverse-compensation action finds the revision already
  advanced by another confirmed switch, the service never overwrites the
  field value: the action is recorded as ``FENCED`` and the switch
  converges to the stable, queryable ``REVISION_CONFLICT`` phase, listing
  the valve, the expected/actual revisions and the unexecuted actions.
* Already changed valves are compensated in the REVERSE of the order they
  succeeded in.
* ``COMPENSATED`` is reported only when every restoration succeeds.
* A failed compensation leaves the switch in the explicit, resumable phase
  ``COMPENSATION_FAILED`` with per-valve status showing exactly what remains.
* Every step is driven from durable state, so a process restart resumes an
  interrupted switch: a device action already committed (but whose receipt
  was never stored) is recognized through the device's
  ``(operation_id, phase)`` dedupe and its recorded revision interval, and
  is never applied twice nor allowed to advance the revision again.
* Re-submitting an existing operation_id returns the same stored result
  (including a revision-fence conclusion); a re-submission with a
  different payload is rejected (409) and never touches a device.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional, Tuple

from .device import DeviceAck, DeviceBank, DeviceError, RevisionFenceError
from .models import (
    RevisionConflict,
    SwitchRequest,
    SwitchStatus,
    UnexecutedAction,
    ValveResult,
)
from .store import ActionRecord, SwitchRecord, SwitchStore

TERMINAL_PHASES = {"COMPLETED", "COMPENSATED", "COMPENSATION_FAILED",
                   "REVISION_CONFLICT"}
# A COMPENSATION_FAILED switch is still resumable, hence not "closed".
# REVISION_CONFLICT is a stable conclusion: replays return it unchanged.
FINAL_PHASES = {"COMPLETED", "COMPENSATED", "REVISION_CONFLICT"}

# Forward-pass outcomes.
_OK = None


class PayloadConflict(Exception):
    """Same operation_id, different payload."""


class SagaEngine:
    def __init__(self, store: SwitchStore, devices: DeviceBank) -> None:
        self._store = store
        self._devices = devices

    # ------------------------------------------------------------ public API

    def submit(self, req: SwitchRequest) -> Tuple[SwitchStatus, bool]:
        """Returns (status, created). ``created`` is False for an idempotent
        replay of an already-known operation_id."""
        payload = self._canonical_payload(req)
        with self._store.lock:
            existing = self._store.get(req.operation_id)
            if existing is not None:
                if json.loads(existing.payload["payload_key"]) != payload:
                    raise PayloadConflict(req.operation_id)
                # Same intent: run recovery/settlement, then return same result.
                return self._resume_and_build(existing), False

            snapshot = self._revision_snapshot(req)
            actions = [
                ActionRecord(
                    valve_id=v.valve_id,
                    idx=i,
                    initial_opening=v.initial_opening,
                    target_opening=v.target_opening,
                    forward_expected_revision=snapshot[v.valve_id],
                )
                for i, v in enumerate(req.valves)
            ]
            rec = self._store.create_intent(
                req.operation_id,
                {"payload_key": json.dumps(payload, sort_keys=True)},
                actions,
            )
            return self._build_status(self._run(rec)), True

    def status(self, operation_id: str) -> Optional[SwitchStatus]:
        rec = self._store.get(operation_id)
        if rec is None:
            return None
        with self._store.lock:
            return self._build_status(self._store.get(operation_id))

    def resume(self, operation_id: str) -> Optional[SwitchStatus]:
        """Continue an interrupted/non-terminal or stuck compensation."""
        with self._store.lock:
            rec = self._store.get(operation_id)
            if rec is None:
                return None
            return self._resume_and_build(rec)

    # ------------------------------------------------------------- machinery

    @staticmethod
    def _canonical_payload(req: SwitchRequest):
        valves = [
            {
                "valve_id": v.valve_id,
                "initial_opening": v.initial_opening,
                "target_opening": v.target_opening,
            }
            for v in req.valves
        ]
        if req.revisions is None:
            # Legacy shape: identical to what pre-revision clients stored,
            # so their replay semantics stay exactly the same.
            return valves
        return {"valves": valves,
                "revisions": {k: req.revisions[k] for k in sorted(req.revisions)}}

    def _revision_snapshot(self, req: SwitchRequest) -> Dict[str, int]:
        """The expected revision per valve for this switch's forward pass.

        Operator-supplied snapshots are used verbatim; legacy requests get
        the snapshot of the moment the service accepts them.
        """
        if req.revisions is not None:
            return dict(req.revisions)
        current = {v.valve_id: v.revision for v in self._devices.list_valves()}
        return {v.valve_id: current.get(v.valve_id, 0) for v in req.valves}

    def _resume_and_build(self, rec: SwitchRecord) -> SwitchStatus:
        rec = self._store.get(rec.operation_id)
        if rec.phase in FINAL_PHASES:
            return self._build_status(rec)
        return self._build_status(self._run(rec))

    def _run(self, rec: SwitchRecord) -> SwitchRecord:
        """Drive the switch forward from whatever durable state exists."""
        op = rec.operation_id

        # ---- forward pass ------------------------------------------------
        if rec.phase in ("PENDING", "EXECUTING"):
            self._store.set_phase(op, "EXECUTING")
            failure = self._forward_pass(op)
            rec = self._store.get(op)
            if failure is None:
                self._store.set_phase(op, "COMPLETED")
                return self._store.get(op)
            # Something failed (a reject/network error, a revision fence, or
            # a pre-existing failure found on resume): enter compensation.
            self._store.set_phase(op, "COMPENSATING", failure)
            rec = self._store.get(op)

        # ---- compensation pass ------------------------------------------
        if rec.phase in ("COMPENSATING", "COMPENSATION_FAILED"):
            outcome = self._compensate_pass(op)
            rec = self._store.get(op)
            if outcome == "FENCED" or (outcome == "OK" and self._fenced(rec)):
                # A revision fence is a stable conclusion: the field values
                # of other confirmed switches were left untouched.
                self._store.set_phase(op, "REVISION_CONFLICT")
            elif outcome == "OK":
                self._store.set_phase(op, "COMPENSATED")
            else:
                self._store.set_phase(op, "COMPENSATION_FAILED")
            return self._store.get(op)

        return rec

    @staticmethod
    def _fenced(rec: SwitchRecord) -> bool:
        return any(
            a.forward_status == "FENCED" or a.compensate_status == "FENCED"
            for a in rec.actions
        )

    def _forward_pass(self, op: str) -> Optional[str]:
        """Execute pending forward actions in order. Returns failure text."""
        for a in self._store.get(op).actions:
            if a.forward_status == "SUCCESS":
                continue
            if a.forward_status == "SKIPPED":
                break
            if a.forward_status == "FAILED":
                # Resumed saga: the failed action is still failing; stop here.
                return a.forward_error or "pre-existing forward failure"
            if a.forward_status == "FENCED":
                # Resumed saga: the fence conclusion is stable.
                return a.forward_error or "pre-existing revision fence"

            try:
                ack = self._devices.execute(
                    operation_id=op,
                    valve_id=a.valve_id,
                    claimed_initial=a.initial_opening,
                    new_opening=a.target_opening,
                    phase="FORWARD",
                    expected_revision=a.forward_expected_revision,
                )
            except RevisionFenceError as fence:
                a.forward_status = "FENCED"
                a.forward_actual_revision = fence.actual
                a.forward_error = str(fence)
                self._store.mark_forward(op, a)
                self._mark_rest_skipped(
                    op, failed_idx=a.idx,
                    reason="not attempted: earlier valve hit a revision fence",
                )
                return str(fence)
            except DeviceError as exc:
                a.forward_status = "FAILED"
                a.forward_error = str(exc)
                # Every valve after the failure point is marked SKIPPED so the
                # state table explains the half-switch unambiguously.
                self._store.mark_forward(op, a)
                self._mark_rest_skipped(
                    op, failed_idx=a.idx,
                    reason="not attempted: earlier valve failed",
                )
                return str(exc)
            a.forward_status = "SUCCESS"
            a.forward_opening = ack.opening
            a.forward_deduped = ack.deduped
            # Reconcile with the device receipt: for a deduped action this is
            # the revision interval recorded when the change actually
            # happened, so a restarted process never advances it again.
            a.forward_expected_revision = ack.expected_revision
            a.forward_actual_revision = ack.actual_revision
            self._store.mark_forward(op, a)
        return None

    def _mark_rest_skipped(self, op: str, failed_idx: int, reason: str) -> None:
        for later in self._store.get(op).actions:
            if later.idx > failed_idx and later.forward_status == "PENDING":
                later.forward_status = "SKIPPED"
                later.forward_error = reason
                self._store.mark_forward(op, later)

    def _compensate_pass(self, op: str) -> str:
        """Restore successfully changed valves in REVERSE order.

        Returns "OK" only if every restoration succeeded, "FAILED" on a
        transient device/transport failure (resumable) and "FENCED" when a
        restoration found the valve's revision advanced by another
        confirmed switch (the field value is then left untouched).
        """
        rec = self._store.get(op)
        to_restore = [a for a in rec.actions if a.forward_status == "SUCCESS"]
        outcome = "OK"

        # Reverse of the success/request order.
        for a in sorted(to_restore, key=lambda x: x.idx, reverse=True):
            if a.compensate_status == "SUCCESS":
                continue
            if a.compensate_status == "FENCED":
                outcome = "FENCED"
                break
            # The restoration conditions on the revision this switch's own
            # forward action produced; if another confirmed switch moved the
            # valve since, the fence refuses the change instead of silently
            # reverting that other opening.
            expected = a.forward_actual_revision
            try:
                ack = self._devices.execute(
                    operation_id=op,
                    valve_id=a.valve_id,
                    claimed_initial=a.initial_opening,
                    new_opening=a.initial_opening,
                    phase="COMPENSATE",
                    expected_revision=expected,
                )
            except RevisionFenceError as fence:
                a.compensate_status = "FENCED"
                a.compensate_expected_revision = expected
                a.compensate_actual_revision = fence.actual
                a.compensate_error = str(fence)
                self._store.mark_compensate(op, a)
                outcome = "FENCED"
                # Stop at the fence: the remaining restorations are listed
                # as unexecuted actions of the revision conflict.
                break
            except DeviceError as exc:
                a.compensate_status = "FAILED"
                a.compensate_error = str(exc)
                self._store.mark_compensate(op, a)
                outcome = "FAILED"
                # Keep trying later valves in reverse order? No: the
                # requirement is an explicit resumable state; we stop at the
                # first restoration failure so the operator sees the blocker,
                # and resume() retries this exact valve then continues.
                break
            a.compensate_status = "SUCCESS"
            a.compensate_opening = ack.opening
            a.compensate_deduped = ack.deduped
            a.compensate_expected_revision = ack.expected_revision
            a.compensate_actual_revision = ack.actual_revision
            self._store.mark_compensate(op, a)

        # Any earlier-fenced/failed restoration (from a prior attempt) also
        # decides the outcome.
        rec = self._store.get(op)
        for a in rec.actions:
            if a.forward_status != "SUCCESS":
                continue
            if a.compensate_status == "FENCED":
                outcome = "FENCED"
            elif a.compensate_status != "SUCCESS" and outcome == "OK":
                outcome = "FAILED"
        return outcome

    # -------------------------------------------------------------- statuses

    def _build_status(self, rec: SwitchRecord) -> SwitchStatus:
        current = {v.valve_id: v for v in self._devices.list_valves()}
        valve_results: List[ValveResult] = []
        conflicts: List[RevisionConflict] = []
        unexecuted: List[UnexecutedAction] = []
        compensation_started = rec.phase in (
            "COMPENSATING", "COMPENSATION_FAILED", "COMPENSATED",
            "REVISION_CONFLICT",
        )
        for a in rec.actions:
            state = current.get(a.valve_id)
            valve_results.append(
                ValveResult(
                    valve_id=a.valve_id,
                    initial_opening=a.initial_opening,
                    target_opening=a.target_opening,
                    forward=a.forward_status,
                    compensate=a.compensate_status,
                    current_opening=(
                        state.opening if state is not None else a.initial_opening
                    ),
                    current_revision=(
                        state.revision if state is not None else None
                    ),
                    forward_expected_revision=a.forward_expected_revision,
                    forward_actual_revision=a.forward_actual_revision,
                    compensate_expected_revision=a.compensate_expected_revision,
                    compensate_actual_revision=a.compensate_actual_revision,
                    forward_deduped=a.forward_deduped,
                    compensate_deduped=a.compensate_deduped,
                    error=a.forward_error or a.compensate_error,
                )
            )
            if a.forward_status == "FENCED":
                conflicts.append(RevisionConflict(
                    valve_id=a.valve_id, phase="FORWARD",
                    expected_revision=a.forward_expected_revision,
                    actual_revision=a.forward_actual_revision,
                ))
            if a.compensate_status == "FENCED":
                conflicts.append(RevisionConflict(
                    valve_id=a.valve_id, phase="COMPENSATE",
                    expected_revision=a.compensate_expected_revision,
                    actual_revision=a.compensate_actual_revision,
                ))
            if a.forward_status in ("PENDING", "SKIPPED"):
                unexecuted.append(UnexecutedAction(
                    valve_id=a.valve_id, phase="FORWARD",
                    reason=a.forward_error or "not attempted",
                ))
            if (compensation_started and a.forward_status == "SUCCESS"
                    and a.compensate_status == "PENDING"):
                unexecuted.append(UnexecutedAction(
                    valve_id=a.valve_id, phase="COMPENSATE",
                    reason="restoration not executed",
                ))
        terminal = rec.phase in TERMINAL_PHASES
        resumable = rec.phase in ("PENDING", "EXECUTING",
                                  "COMPENSATING", "COMPENSATION_FAILED")
        return SwitchStatus(
            operation_id=rec.operation_id,
            phase=rec.phase,
            terminal=terminal,
            success=rec.phase == "COMPLETED",
            resumable=resumable,
            valves=valve_results,
            failure=rec.failure,
            conflicts=conflicts,
            unexecuted=unexecuted,
        )
