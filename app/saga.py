"""Saga engine for a multi-valve switch.

Rules implemented here:

* Valves move in the request order; the FIRST failing forward action stops
  the forward pass.
* Every device action is a conditional change on the valve's revision:
  forward actions are conditioned on the snapshot carried by the submitting
  page (or snapshotted at acceptance time for legacy requests), and each
  compensation is conditioned on the revision that switch's own forward
  receipt recorded.  A compensation can therefore never silently revert
  another confirmed switch's opening.
* If a forward or compensation action finds the revision already advanced
  by another confirmed switch, the service does NOT overwrite the field
  value: the switch stably converges to the queryable ``REVISION_CONFLICT``
  conclusion listing the fenced valve, expected/actual revisions and the
  actions that were never executed.  Re-submitting the same operation_id
  only returns that stored conclusion.
* Already changed valves are compensated in the REVERSE of the order they
  succeeded in.
* ``COMPENSATED`` is reported only when every restoration succeeds.
* A failed compensation leaves the switch in the explicit, resumable phase
  ``COMPENSATION_FAILED`` with per-valve status showing exactly what remains.
* Every step is driven from durable state, so a process restart resumes an
  interrupted switch: a device action already committed (but whose receipt
  was never stored) is recognized through the device's
  ``(operation_id, phase)`` dedupe log and its recorded revision range, and
  is never applied twice nor advanced again.
* Re-submitting an existing operation_id returns the same stored result; a
  re-submission with a different payload is rejected (409) and never touches
  a device.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .device import DeviceAck, DeviceBank, DeviceError, RevisionConflictError
from .models import SwitchRequest, SwitchStatus, ValveResult
from .store import ActionRecord, SwitchRecord, SwitchStore

TERMINAL_PHASES = {"COMPLETED", "COMPENSATED", "COMPENSATION_FAILED",
                   "REVISION_CONFLICT"}
# COMPENSATION_FAILED is still resumable, hence not "closed"; a converged
# REVISION_CONFLICT is final: same-id retransmission only returns it.
FINAL_PHASES = {"COMPLETED", "COMPENSATED", "REVISION_CONFLICT"}


class PayloadConflict(Exception):
    """Same operation_id, different payload."""


@dataclass
class _Fence:
    """A revision fence detected by one device action."""
    action: ActionRecord
    phase: str                     # FORWARD / COMPENSATE
    error: RevisionConflictError


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

            # Revision snapshot: carried by the submitting page, or taken at
            # acceptance time for legacy requests that omit it.
            current_revs = {
                v.valve_id: v.revision for v in self._devices.list_valves()
            }
            actions = [
                ActionRecord(
                    valve_id=v.valve_id,
                    idx=i,
                    initial_opening=v.initial_opening,
                    target_opening=v.target_opening,
                    expected_revision=(
                        v.expected_revision
                        if v.expected_revision is not None
                        else current_revs.get(v.valve_id, 0)
                    ),
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
    def _canonical_payload(req: SwitchRequest) -> List[Dict]:
        return [
            {
                "valve_id": v.valve_id,
                "initial_opening": v.initial_opening,
                "target_opening": v.target_opening,
                "expected_revision": v.expected_revision,
            }
            for v in req.valves
        ]

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
            failure, fence = self._forward_pass(op)
            if fence is not None:
                self._converge_fence(op, fence)
                return self._store.get(op)
            rec = self._store.get(op)
            if failure is None:
                self._store.set_phase(op, "COMPLETED")
                return self._store.get(op)
            # Something failed (or a pre-existing failure was found on resume):
            # enter compensation.
            self._store.set_phase(op, "COMPENSATING", failure)
            rec = self._store.get(op)

        # ---- compensation pass ------------------------------------------
        if rec.phase in ("COMPENSATING", "COMPENSATION_FAILED"):
            fully_restored, fence = self._compensate_pass(op)
            if fence is not None:
                self._converge_fence(op, fence)
                return self._store.get(op)
            rec = self._store.get(op)
            if fully_restored:
                self._store.set_phase(op, "COMPENSATED")
            else:
                self._store.set_phase(op, "COMPENSATION_FAILED")
            return self._store.get(op)

        return rec

    def _forward_pass(
        self, op: str
    ) -> Tuple[Optional[str], Optional[_Fence]]:
        """Execute pending forward actions in order.

        Returns ``(failure_text, fence)``; at most one of them is set.
        """
        for a in self._store.get(op).actions:
            if a.forward_status == "SUCCESS":
                continue
            if a.forward_status == "SKIPPED":
                break
            if a.forward_status == "FAILED":
                # Resumed saga: the failed action is still failing; stop here.
                return a.forward_error or "pre-existing forward failure", None

            ack, err, fence = self._call_device(
                op, a, phase="FORWARD", desired=a.target_opening,
                expected_revision=a.expected_revision,
            )
            if fence is not None:
                a.forward_status = "FENCED"
                a.forward_error = str(fence.error)
                self._store.mark_forward(op, a)
                self._mark_rest_skipped(op, failed_idx=a.idx,
                                        reason="not attempted: revision fence")
                return None, fence
            if err is not None:
                a.forward_status = "FAILED"
                a.forward_error = err
                # Every valve after the failure point is marked SKIPPED so the
                # state table explains the half-switch unambiguously.
                self._store.mark_forward(op, a)
                self._mark_rest_skipped(op, failed_idx=a.idx,
                                        reason="not attempted: earlier valve failed")
                return err, None
            a.forward_status = "SUCCESS"
            a.forward_opening = ack.opening
            a.forward_deduped = ack.deduped
            a.forward_actual_revision = ack.actual_revision
            self._store.mark_forward(op, a)
        return None, None

    def _mark_rest_skipped(self, op: str, failed_idx: int,
                           reason: str) -> None:
        for later in self._store.get(op).actions:
            if later.idx > failed_idx and later.forward_status == "PENDING":
                later.forward_status = "SKIPPED"
                later.forward_error = reason
                self._store.mark_forward(op, later)

    def _compensate_pass(
        self, op: str
    ) -> Tuple[bool, Optional[_Fence]]:
        """Restore successfully changed valves in REVERSE order.

        Returns ``(all_ok, fence)``: ``all_ok`` is True only if every
        restoration succeeded; ``fence`` is set when a restoration found the
        valve's revision advanced by another confirmed switch.
        """
        rec = self._store.get(op)
        to_restore = [a for a in rec.actions if a.forward_status == "SUCCESS"]
        all_ok = True

        # Reverse of the success/request order.
        for a in sorted(to_restore, key=lambda x: x.idx, reverse=True):
            if a.compensate_status == "SUCCESS":
                continue
            # Condition the restoration on the revision this switch's own
            # forward receipt recorded: if another confirmed switch advanced
            # the valve since, the device refuses and we must not overwrite
            # its field value.
            expected = a.forward_actual_revision
            if expected is None:
                # Legacy rows (pre-revision receipts): the forward action
                # produced exactly one revision bump from the snapshot.
                expected = a.expected_revision + 1
            a.compensate_expected_revision = expected
            ack, err, fence = self._call_device(
                op, a, phase="COMPENSATE", desired=a.initial_opening,
                expected_revision=expected,
            )
            if fence is not None:
                a.compensate_status = "FENCED"
                a.compensate_error = str(fence.error)
                self._store.mark_compensate(op, a)
                return False, fence
            if err is not None:
                a.compensate_status = "FAILED"
                a.compensate_error = err
                self._store.mark_compensate(op, a)
                all_ok = False
                # Keep trying later valves in reverse order? No: the
                # requirement is an explicit resumable state; we stop at the
                # first restoration failure so the operator sees the blocker,
                # and resume() retries this exact valve then continues.
                break
            a.compensate_status = "SUCCESS"
            a.compensate_opening = ack.opening
            a.compensate_deduped = ack.deduped
            a.compensate_actual_revision = ack.actual_revision
            self._store.mark_compensate(op, a)

        # Any earlier-failed restoration (from a prior attempt) also blocks.
        rec = self._store.get(op)
        for a in rec.actions:
            if a.forward_status == "SUCCESS" and a.compensate_status != "SUCCESS":
                all_ok = False
        return all_ok, None

    def _converge_fence(self, op: str, fence: _Fence) -> None:
        """Stably converge the switch to the queryable REVISION_CONFLICT
        conclusion: fenced valve, expected/actual revisions and every action
        that was never executed.  No further device change is attempted."""
        rec = self._store.get(op)
        unexecuted: List[Dict] = []
        if fence.phase == "FORWARD":
            for a in rec.actions:
                if a.forward_status in ("FENCED", "SKIPPED", "PENDING"):
                    unexecuted.append({
                        "valve_id": a.valve_id,
                        "phase": "FORWARD",
                        "intended_opening": a.target_opening,
                    })
        else:  # COMPENSATE fence
            pending = [
                a for a in rec.actions
                if a.forward_status == "SUCCESS"
                and a.compensate_status in ("FENCED", "PENDING", "FAILED")
            ]
            # Listed in the order the restorations would have run (reverse).
            for a in sorted(pending, key=lambda x: x.idx, reverse=True):
                unexecuted.append({
                    "valve_id": a.valve_id,
                    "phase": "COMPENSATE",
                    "intended_opening": a.initial_opening,
                })
        conflict = {
            "valve_id": fence.action.valve_id,
            "phase": fence.phase,
            "expected_revision": fence.error.expected_revision,
            "actual_revision": fence.error.actual_revision,
            "unexecuted": unexecuted,
            "detail": (
                f"revision fence: valve {fence.action.valve_id} "
                f"{fence.phase} expected revision "
                f"{fence.error.expected_revision} but the device holds "
                f"{fence.error.actual_revision}; another confirmed switch "
                "advanced it, field values left untouched"
            ),
        }
        self._store.set_conflict(op, conflict)

    def _call_device(
        self, op_id: str, a: ActionRecord, phase: str, desired: int,
        expected_revision: int,
    ) -> Tuple[Optional[DeviceAck], Optional[str], Optional[_Fence]]:
        try:
            ack = self._devices.execute(
                operation_id=op_id,
                valve_id=a.valve_id,
                claimed_initial=a.initial_opening,
                new_opening=desired,
                phase=phase,
                expected_revision=expected_revision,
            )
            return ack, None, None
        except RevisionConflictError as exc:
            return None, None, _Fence(a, phase, exc)
        except DeviceError as exc:
            return None, str(exc), None

    # -------------------------------------------------------------- statuses

    def _build_status(self, rec: SwitchRecord) -> SwitchStatus:
        current = {v.valve_id: v for v in self._devices.list_valves()}
        valve_results: List[ValveResult] = []
        for a in rec.actions:
            cur = current.get(a.valve_id)
            valve_results.append(
                ValveResult(
                    valve_id=a.valve_id,
                    initial_opening=a.initial_opening,
                    target_opening=a.target_opening,
                    forward=a.forward_status,
                    compensate=a.compensate_status,
                    current_opening=(
                        cur.opening if cur is not None else a.initial_opening
                    ),
                    error=a.forward_error or a.compensate_error,
                    expected_revision=a.expected_revision,
                    forward_actual_revision=a.forward_actual_revision,
                    compensate_expected_revision=a.compensate_expected_revision,
                    compensate_actual_revision=a.compensate_actual_revision,
                    current_revision=cur.revision if cur is not None else None,
                )
            )
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
            conflict=rec.conflict,
        )
