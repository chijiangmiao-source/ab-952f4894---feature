"""Pydantic schemas for the vacuum valve bank switch API."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# Switch lifecycle phases, persisted per switch request.
SwitchPhase = Literal[
    "PENDING",        # intent registered, no device work yet
    "EXECUTING",      # at least one forward action attempted
    "COMPLETED",      # all valves reached target opening
    "COMPENSATING",   # a failure occurred; rolling back in reverse order
    "COMPENSATED",    # every successfully changed valve restored
    "COMPENSATION_FAILED",  # rollback incomplete; resumable
    "REVISION_CONFLICT",    # revision fence hit; stable, queryable conclusion
]

# Per-action device-side phase, used as the dedupe key component together
# with the operation id (device dedupes on "op id + phase").
ActionPhase = Literal["FORWARD", "COMPENSATE"]

ActionResult = Literal["PENDING", "SUCCESS", "FAILED", "SKIPPED", "FENCED"]


class ValveSpec(BaseModel):
    """One valve in a switch request."""

    valve_id: str = Field(..., min_length=1, max_length=64)
    initial_opening: int = Field(..., ge=0, le=100)
    target_opening: int = Field(..., ge=0, le=100)
    # Revision snapshot shown by the console when the switch was prepared.
    # Optional: legacy pages omit it and the server snapshots the revisions
    # at acceptance time instead.
    expected_revision: Optional[int] = Field(default=None, ge=0)

    @field_validator("valve_id")
    @classmethod
    def _nonblank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("valve_id must not be blank")
        return v


class SwitchRequest(BaseModel):
    operation_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Stable client-chosen idempotency key for this switch.",
    )
    valves: List[ValveSpec] = Field(..., min_length=2, max_length=8)

    @field_validator("operation_id")
    @classmethod
    def _op_nonblank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("operation_id must not be blank")
        return v

    @field_validator("valves")
    @classmethod
    def _unique_valves(cls, v: List[ValveSpec]) -> List[ValveSpec]:
        ids = [spec.valve_id for spec in v]
        if len(set(ids)) != len(ids):
            raise ValueError("valve_id entries must be unique within a request")
        return v


class UnexecutedAction(BaseModel):
    """An action the switch never executed because of the revision fence."""

    valve_id: str
    phase: ActionPhase
    # Opening the action would have driven to (target for FORWARD, original
    # opening for COMPENSATE).
    intended_opening: int


class RevisionConflictInfo(BaseModel):
    """Stable, queryable conclusion of a switch that hit a revision fence."""

    valve_id: str                 # valve whose revision no longer matched
    phase: ActionPhase            # action kind that detected the fence
    expected_revision: int        # revision the action was conditioned on
    actual_revision: int          # revision the device actually holds
    unexecuted: List[UnexecutedAction]
    detail: str


class ValveResult(BaseModel):
    valve_id: str
    initial_opening: int
    target_opening: int
    forward: ActionResult
    compensate: ActionResult
    current_opening: Optional[int] = None
    error: Optional[str] = None
    # Revision bookkeeping: the snapshot the forward action was conditioned
    # on, the revisions the device confirmed in its receipts, and the
    # revision the device currently holds.
    expected_revision: Optional[int] = None
    forward_actual_revision: Optional[int] = None
    compensate_expected_revision: Optional[int] = None
    compensate_actual_revision: Optional[int] = None
    current_revision: Optional[int] = None


class SwitchStatus(BaseModel):
    operation_id: str
    phase: SwitchPhase
    terminal: bool
    success: bool
    resumable: bool
    valves: List[ValveResult]
    failure: Optional[str] = None
    conflict: Optional[RevisionConflictInfo] = None
