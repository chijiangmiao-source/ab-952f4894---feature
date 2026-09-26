"""Simulated vacuum-valve device bank.

The device layer is deliberately separated from the application database:
it represents the physical valves and survives application/console restarts.

Guarantees:

* Every valve carries a monotonically increasing ``revision`` alongside its
  opening.  Every state-changing call is a *conditional change*: the caller
  asserts an ``expected_revision`` and the device only moves the valve when
  the stored revision still matches, persisting the pair
  ``expected_revision -> actual_revision`` (actual = expected + 1) together
  with the new opening.  A mismatch raises :class:`RevisionConflictError`
  and never touches the field value, so a stale forward or compensation
  action can never silently revert another confirmed switch.
* Every call is deduplicated on the pair ``(operation_id, phase)`` per
  valve.  The opening change, the revision bump and the dedupe record
  commit in one local transaction, so a crash that happens *after* the
  device changed but *before* the application stored its receipt still
  leaves a recognizable conditional change: the retried call returns the
  recorded revision range without moving the valve or advancing the
  revision a second time.
* Executed actions (forward, compensation and restart re-recorded ones)
  are queryable with their revision ranges (``executed_actions``).
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


class RevisionConflictError(DeviceError):
    """The valve's stored revision no longer matches the caller's expected
    revision: another confirmed switch has advanced it.  The device refused
    the conditional change and left the field value untouched."""

    def __init__(self, valve_id: str, expected_revision: int,
                 actual_revision: int) -> None:
        self.valve_id = valve_id
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            f"revision fence on valve {valve_id}: expected revision "
            f"{expected_revision}, device holds {actual_revision} "
            "(advanced by another confirmed switch); field value untouched"
        )


@dataclass(frozen=True)
class DeviceAck:
    operation_id: str
    valve_id: str
    phase: str
    opening: int          # opening recorded by the device for this action
    deduped: bool         # True if the action had already been executed
    expected_revision: int  # revision the conditional change was based on
    actual_revision: int    # revision the device recorded after the change


@dataclass(frozen=True)
class ExecutedAction:
    operation_id: str
    valve_id: str
    phase: str
    opening: int
    expected_revision: int
    actual_revision: int
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
                    operation_id     TEXT NOT NULL,
                    valve_id         TEXT NOT NULL,
                    phase            TEXT NOT NULL,
                    opening          INTEGER NOT NULL,
                    expected_revision INTEGER NOT NULL DEFAULT 0,
                    actual_revision  INTEGER NOT NULL DEFAULT 0,
                    executed_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    seq              INTEGER NOT NULL,
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
        def columns(table: str) -> set[str]:
            return {
                r["name"]
                for r in self._conn.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }

        if "revision" not in columns("valves"):
            self._conn.execute(
                "ALTER TABLE valves ADD COLUMN revision "
                "INTEGER NOT NULL DEFAULT 0"
            )
        ea = columns("executed_actions")
        if "expected_revision" not in ea:
            self._conn.execute(
                "ALTER TABLE executed_actions ADD COLUMN expected_revision "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "actual_revision" not in ea:
            self._conn.execute(
                "ALTER TABLE executed_actions ADD COLUMN actual_revision "
                "INTEGER NOT NULL DEFAULT 0"
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
                "SELECT valve_id, opening, revision FROM valves "
                "ORDER BY valve_id"
            ).fetchall()
        return [
            ValveState(r["valve_id"], r["opening"], r["revision"])
            for r in rows
        ]

    def executed_actions(self, operation_id: Optional[str] = None) -> List[ExecutedAction]:
        with self._lock:
            base = (
                "SELECT operation_id, valve_id, phase, opening, "
                "expected_revision, actual_revision, executed_at "
                "FROM executed_actions"
            )
            if operation_id is None:
                rows = self._conn.execute(base + " ORDER BY seq").fetchall()
            else:
                rows = self._conn.execute(
                    base + " WHERE operation_id = ? ORDER BY seq",
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

        The action is persisted as a conditional change
        ``expected_revision -> expected_revision + 1``: the opening only
        changes when the stored revision still equals ``expected_revision``.

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
                    # Idempotent replay / restart reconciliation: return the
                    # recorded conditional change (same revision range), and
                    # do NOT advance the revision a second time.
                    self._conn.commit()
                    return DeviceAck(
                        operation_id, valve_id, phase,
                        existing["opening"], deduped=True,
                        expected_revision=existing["expected_revision"],
                        actual_revision=existing["actual_revision"],
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
                    current_opening, current_revision = claimed_initial, 0
                else:
                    current_opening = valve["opening"]
                    current_revision = valve["revision"]

                # Revision fence: another confirmed switch has advanced the
                # revision since the caller's snapshot.  Refuse the change;
                # the field value is left exactly as it is.
                if current_revision != expected_revision:
                    self._conn.commit()
                    raise RevisionConflictError(
                        valve_id, expected_revision, current_revision
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

                next_revision = expected_revision + 1
                seq_row = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS s FROM executed_actions"
                ).fetchone()
                self._conn.execute(
                    "UPDATE valves SET opening = ?, revision = ? "
                    "WHERE valve_id = ?",
                    (new_opening, next_revision, valve_id),
                )
                self._conn.execute(
                    "INSERT INTO executed_actions"
                    "(operation_id, valve_id, phase, opening, "
                    " expected_revision, actual_revision, seq) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (operation_id, valve_id, phase, new_opening,
                     expected_revision, next_revision, seq_row["s"] + 1),
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
            operation_id, valve_id, phase, new_opening, deduped=False,
            expected_revision=expected_revision,
            actual_revision=expected_revision + 1,
        )
