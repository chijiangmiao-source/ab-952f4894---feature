"""Saga engine for a multi-valve switch.

Rules implemented here:

* Valves move in the request order; the FIRST failing forward action stops
  the forward pass.
* Already changed valves are compensated in the REVERSE of the order they
  succeeded in.
* ``COMPENSATED`` is reported only when every restoration succeeds.
* A failed compensation leaves the switch in the explicit, resumable phase
  ``COMPENSATION_FAILED`` with per-valve status showing exactly what remains.
* Every step is driven from durable state, so a process restart resumes an
  interrupted switch: a device action already committed (but whose receipt
  was never stored) is recognized through the device's
  ``(operation_id, phase)`` dedupe and never applied twice.
* Re-submitting an existing operation_id returns the same stored result; a
  re-submission with a different payload is rejected (409) and never touches
  a device.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional, Tuple

from .device import DeviceAck, DeviceBank, DeviceError
from .models import SwitchRequest, SwitchStatus, ValveResult
from .store import ActionRecord, SwitchRecord, SwitchStore

TERMINAL_PHASES = {"COMPLETED", "COMPENSATED", "COMPENSATION_FAILED"}
# A COMPENSATION_FAILED switch is still resumable, hence not "closed".
FINAL_PHASES = {"COMPLETED", "COMPENSATED"}


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

            actions = [
                ActionRecord(
                    valve_id=v.valve_id,
                    idx=i,
                    initial_opening=v.initial_opening,
                    target_opening=v.target_opening,
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
            failure = self._forward_pass(op)
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
            fully_restored = self._compensate_pass(op)
            rec = self._store.get(op)
            if fully_restored:
                self._store.set_phase(op, "COMPENSATED")
            else:
                self._store.set_phase(op, "COMPENSATION_FAILED")
            return self._store.get(op)

        return rec

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

            ack, err = self._call_device(
                op, a, phase="FORWARD", desired=a.target_opening
            )
            if err is not None:
                a.forward_status = "FAILED"
                a.forward_error = err
                # Every valve after the failure point is marked SKIPPED so the
                # state table explains the half-switch unambiguously.
                self._store.mark_forward(op, a)
                self._mark_rest_skipped(op, failed_idx=a.idx)
                return err
            a.forward_status = "SUCCESS"
            a.forward_opening = ack.opening
            a.forward_deduped = ack.deduped
            self._store.mark_forward(op, a)
        return None

    def _mark_rest_skipped(self, op: str, failed_idx: int) -> None:
        for later in self._store.get(op).actions:
            if later.idx > failed_idx and later.forward_status == "PENDING":
                later.forward_status = "SKIPPED"
                later.forward_error = "not attempted: earlier valve failed"
                self._store.mark_forward(op, later)

    def _compensate_pass(self, op: str) -> bool:
        """Restore successfully changed valves in REVERSE order.

        Returns True only if every restoration succeeded.
        """
        rec = self._store.get(op)
        to_restore = [a for a in rec.actions if a.forward_status == "SUCCESS"]
        all_ok = True

        # Reverse of the success/request order.
        for a in sorted(to_restore, key=lambda x: x.idx, reverse=True):
            if a.compensate_status == "SUCCESS":
                continue
            ack, err = self._call_device(
                op, a, phase="COMPENSATE", desired=a.initial_opening
            )
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
            self._store.mark_compensate(op, a)

        # Any earlier-failed restoration (from a prior attempt) also blocks.
        rec = self._store.get(op)
        for a in rec.actions:
            if a.forward_status == "SUCCESS" and a.compensate_status != "SUCCESS":
                all_ok = False
        return all_ok

    def _call_device(
        self, op_id: str, a: ActionRecord, phase: str, desired: int
    ) -> Tuple[Optional[DeviceAck], Optional[str]]:
        try:
            ack = self._devices.execute(
                operation_id=op_id,
                valve_id=a.valve_id,
                claimed_initial=a.initial_opening,
                new_opening=desired,
                phase=phase,
            )
            return ack, None
        except DeviceError as exc:
            return None, str(exc)

    # -------------------------------------------------------------- statuses

    def _build_status(self, rec: SwitchRecord) -> SwitchStatus:
        current = {v.valve_id: v.opening for v in self._devices.list_valves()}
        valve_results: List[ValveResult] = []
        for a in rec.actions:
            valve_results.append(
                ValveResult(
                    valve_id=a.valve_id,
                    initial_opening=a.initial_opening,
                    target_opening=a.target_opening,
                    forward=a.forward_status,
                    compensate=a.compensate_status,
                    current_opening=current.get(a.valve_id, a.initial_opening),
                    error=a.forward_error or a.compensate_error,
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
        )
