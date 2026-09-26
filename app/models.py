"""Pydantic schemas for the vacuum valve bank switch API."""
from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# Switch lifecycle phases, persisted per switch request.
SwitchPhase = Literal[
    "PENDING",        # intent registered, no device work yet
    "EXECUTING",      # at least one forward action attempted
    "COMPLETED",      # all valves reached target opening
    "COMPENSATING",   # a failure occurred; rolling back in reverse order
    "COMPENSATED",    # every successfully changed valve restored
    "COMPENSATION_FAILED",  # rollback incomplete; resumable
    "REVISION_CONFLICT",    # revision fence hit; field values left untouched
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
    revisions: Optional[Dict[str, int]] = Field(
        default=None,
        description=(
            "Optional snapshot of the server-confirmed opening revision per "
            "valve, as shown to the operator before submitting. When "
            "omitted (legacy clients), the service takes the snapshot at "
            "acceptance time."
        ),
    )

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

    @model_validator(mode="after")
    def _revisions_match_valves(self) -> "SwitchRequest":
        if self.revisions is not None:
            ids = {v.valve_id for v in self.valves}
            keys = set(self.revisions)
            if keys != ids:
                raise ValueError(
                    "revisions must cover exactly the requested valve_ids "
                    f"(missing: {sorted(ids - keys)}, extra: {sorted(keys - ids)})"
                )
            for valve_id, rev in self.revisions.items():
                if not isinstance(rev, int) or rev < 0:
                    raise ValueError(
                        f"revision for {valve_id!r} must be a non-negative integer"
                    )
        return self


class ValveResult(BaseModel):
    valve_id: str
    initial_opening: int
    target_opening: int
    forward: ActionResult
    compensate: ActionResult
    current_opening: Optional[int] = None
    current_revision: Optional[int] = None
    forward_expected_revision: Optional[int] = None
    forward_actual_revision: Optional[int] = None
    compensate_expected_revision: Optional[int] = None
    compensate_actual_revision: Optional[int] = None
    forward_deduped: bool = False
    compensate_deduped: bool = False
    error: Optional[str] = None


class RevisionConflict(BaseModel):
    """One conditional change that hit the revision fence: the device
    revision had already been advanced by another confirmed switch, so the
    action was NOT applied and the field value was left untouched."""

    valve_id: str
    phase: ActionPhase
    expected_revision: int
    actual_revision: int


class UnexecutedAction(BaseModel):
    """An action the switch never (or no longer) executed, listed so the
    operator sees exactly what remains untouched."""

    valve_id: str
    phase: ActionPhase
    reason: str


class SwitchStatus(BaseModel):
    operation_id: str
    phase: SwitchPhase
    terminal: bool
    success: bool
    resumable: bool
    valves: List[ValveResult]
    failure: Optional[str] = None
    conflicts: List[RevisionConflict] = []
    unexecuted: List[UnexecutedAction] = []
