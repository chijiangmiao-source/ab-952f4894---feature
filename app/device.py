"""Simulated vacuum-valve device bank.

The device layer is deliberately separated from the application database:
it represents the physical valves and survives application/console restarts.

Guarantees:

* **Conditional opening changes (revision fence).** Every valve carries a
  monotonically increasing ``revision``. Each state-changing call must name
  the ``expected_revision`` it based its decision on; the opening only
  changes when the valve is still at that revision, and the change advances
  the revision by exactly one (``expected -> expected + 1``). If another
  confirmed switch has already advanced the revision, the call is fenced
  off with :class:`RevisionFenceError` and the field value is left
  untouched.
* Every state-changing call is deduplicated on the pair
  ``(operation_id, phase)`` per valve.  The opening change, the revision
  bump and the dedupe record (which stores the expected/actual revision
  interval) commit in one local transaction, so a crash that happens
  *after* the device changed but *before* the application stored its
  receipt still leaves a recognizable executed action: the retried call
  returns the same result without moving the valve or advancing the
  revision a second time.
* Executed actions — forward, compensation and restart-reconciliation
  alike — are queryable (``executed_actions``) with their revision
  intervals.
* Failure injection (reject / network) never mutates state.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from typing import List, Optional

# Exit code used when the crash injection point is hit. Kept distinctive so
# tests can assert the process was killed mid-switch.
CRASH_EXIT_CODE = 77


class DeviceError(Exception):
    """Base class for simulated device/transport failures."""


class DeviceRejectedError(DeviceError):
    """The device actively rejected the command."""


class DeviceNetworkError(DeviceError):
    """The network/transport returned failure; device state is unknown/unchanged."""


class RevisionFenceError(DeviceError):
    """The conditional change was refused: the valve's revision has already
    been advanced (by another confirmed switch) past the caller's expected
    revision. The device did NOT change the opening."""

    def __init__(self, valve_id: str, phase: str, expected: int, actual: int) -> None:
        self.valve_id = valve_id
        self.phase = phase
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"revision fence on valve {valve_id} ({phase}): expected revision "
            f"{expected}, device is at revision {actual} — opening left untouched"
        )


@dataclass(frozen=True)
class DeviceAck:
    operation_id: str
    valve_id: str
    phase: str
    opening: int          # opening recorded by the device for this action
    expected_revision: int  # revision the caller conditioned on
    actual_revision: int    # revision after this action (expected + 1)
    deduped: bool         # True if the action had already been executed


@dataclass(frozen=True)
class ExecutedAction:
    operation_id: str
    valve_id: str
    phase: str
    opening: int
    expected_revision: Optional[int]
    actual_revision: Optional[int]
    executed_at: str


@dataclass(frozen=True)
class ValveState:
    valve_id: str
    opening: int
    revision: int


class DeviceBank:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.RLock()
        # Autocommit mode: every read/modify/write is wrapped in an explicit
        # BEGIN IMMEDIATE ... COMMIT below.
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS valves (
                    valve_id TEXT PRIMARY KEY,
                    opening  INTEGER NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS executed_actions (
                    operation_id TEXT NOT NULL,
                    valve_id     TEXT NOT NULL,
                    phase        TEXT NOT NULL,
                    opening      INTEGER NOT NULL,
                    expected_revision INTEGER,
                    actual_revision   INTEGER,
                    executed_at  TEXT NOT NULL DEFAULT (datetime('now')),
                    seq          INTEGER NOT NULL,
                    PRIMARY KEY (operation_id, valve_id, phase)
                );
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self._migrate()

    def _migrate(self) -> None:
        """Add revision columns to databases created by older versions."""
        valve_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(valves)")}
        if "revision" not in valve_cols:
            self._conn.execute(
                "ALTER TABLE valves ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
            )
        action_cols = {
            r["name"] for r in self._conn.execute("PRAGMA table_info(executed_actions)")
        }
        for col in ("expected_revision", "actual_revision"):
            if col not in action_cols:
                self._conn.execute(
                    f"ALTER TABLE executed_actions ADD COLUMN {col} INTEGER"
                )

    # ------------------------------------------------------------------ admin

    def reset(self) -> None:
        """Wipe the simulated device bank (test support)."""
        with self._lock, self._conn:
            self._conn.executescript(
                "DELETE FROM executed_actions; DELETE FROM valves; "
                "DELETE FROM config;"
            )

    def set_failures(self, forward: List[str], compensate: List[str]) -> None:
        """Configure valves that reject calls for a given phase."""
        import json

        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO config(key, value) VALUES('fail_forward', ?)",
                (json.dumps(sorted(forward)),),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO config(key, value) VALUES('fail_compensate', ?)",
                (json.dumps(sorted(compensate)),),
            )

    def _failure_set(self, key: str) -> set[str]:
        import json

        row = self._conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        ).fetchone()
        return set(json.loads(row["value"])) if row else set()

    def list_valves(self) -> List[ValveState]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT valve_id, opening, revision FROM valves ORDER BY valve_id"
            ).fetchall()
        return [ValveState(r["valve_id"], r["opening"], r["revision"]) for r in rows]

    def executed_actions(self, operation_id: Optional[str] = None) -> List[ExecutedAction]:
        with self._lock:
            if operation_id is None:
                rows = self._conn.execute(
                    "SELECT operation_id, valve_id, phase, opening, "
                    "expected_revision, actual_revision, executed_at "
                    "FROM executed_actions ORDER BY seq"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT operation_id, valve_id, phase, opening, "
                    "expected_revision, actual_revision, executed_at "
                    "FROM executed_actions WHERE operation_id = ? ORDER BY seq",
                    (operation_id,),
                ).fetchall()
        return [
            ExecutedAction(
                r["operation_id"], r["valve_id"], r["phase"],
                r["opening"], r["expected_revision"], r["actual_revision"],
                r["executed_at"],
            )
            for r in rows
        ]

    # ----------------------------------------------------------------- runtime

    def execute(
        self,
        operation_id: str,
        valve_id: str,
        claimed_initial: int,
        new_opening: int,
        phase: str,
        expected_revision: int,
    ) -> DeviceAck:
        """Apply (or de-duplicate) one conditional action on a valve.

        The opening only changes when the valve is still at
        ``expected_revision``; the change is persisted together with the
        revision interval ``expected_revision -> expected_revision + 1``.
        A replay of an already-executed ``(operation_id, valve_id, phase)``
        returns the recorded receipt without touching the valve again.

        ``claimed_initial`` is only used when the device has never seen the
        valve; the engineer-declared initial opening is what the simulated
        physical valve starts at (revision 0).
        """
        crash_spec = os.environ.get("CRASH_AFTER", "").strip()
        with self._lock:
            # BEGIN IMMEDIATE-style serialization for the read/modify/write.
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    "SELECT opening, expected_revision, actual_revision "
                    "FROM executed_actions "
                    "WHERE operation_id = ? AND valve_id = ? AND phase = ?",
                    (operation_id, valve_id, phase),
                ).fetchone()
                if existing is not None:
                    # Idempotent replay / restart reconciliation: the
                    # conditional change already happened with this revision
                    # interval — return the recorded receipt, do NOT advance
                    # the revision a second time.
                    self._conn.commit()
                    return DeviceAck(
                        operation_id, valve_id, phase,
                        existing["opening"],
                        existing["expected_revision"],
                        existing["actual_revision"],
                        deduped=True,
                    )

                valve = self._conn.execute(
                    "SELECT opening, revision FROM valves WHERE valve_id = ?",
                    (valve_id,),
                ).fetchone()
                if valve is None:
                    self._conn.execute(
                        "INSERT INTO valves(valve_id, opening, revision) "
                        "VALUES(?, ?, 0)",
                        (valve_id, claimed_initial),
                    )
                    current_revision = 0
                else:
                    current_revision = valve["revision"]

                # Revision fence: another confirmed switch has advanced the
                # valve past the caller's snapshot. Never overwrite the
                # field value.
                if current_revision != expected_revision:
                    self._conn.commit()
                    raise RevisionFenceError(
                        valve_id, phase, expected_revision, current_revision
                    )

                # Failure injection happens before any mutation, so retries
                # after a transient reject/network failure remain safe.
                fail_key = "fail_forward" if phase == "FORWARD" else "fail_compensate"
                if valve_id in self._failure_set(fail_key):
                    self._conn.commit()
                    if phase == "FORWARD":
                        raise DeviceRejectedError(
                            f"valve {valve_id} rejected FORWARD to {new_opening}"
                        )
                    raise DeviceNetworkError(
                        f"network failure while restoring valve {valve_id}"
                    )

                new_revision = current_revision + 1
                seq_row = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS s FROM executed_actions"
                ).fetchone()
                self._conn.execute(
                    "UPDATE valves SET opening = ?, revision = ? WHERE valve_id = ?",
                    (new_opening, new_revision, valve_id),
                )
                self._conn.execute(
                    "INSERT INTO executed_actions"
                    "(operation_id, valve_id, phase, opening, "
                    " expected_revision, actual_revision, seq) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (operation_id, valve_id, phase, new_opening,
                     current_revision, new_revision, seq_row["s"] + 1),
                )
                # Device-side change, revision bump and dedupe record commit
                # atomically.
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        # ---- crash point: device committed, application receipt not yet stored
        if crash_spec and crash_spec == f"{operation_id}:{valve_id}:{phase}":
            # Hard kill: no finally blocks, no receipt write.
            os._exit(CRASH_EXIT_CODE)

        return DeviceAck(
            operation_id, valve_id, phase, new_opening,
            current_revision, new_revision, deduped=False,
        )
