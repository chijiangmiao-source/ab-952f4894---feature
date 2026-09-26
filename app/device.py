"""Simulated vacuum-valve device bank.

The device layer is deliberately separated from the application database:
it represents the physical valves and survives application/console restarts.

Guarantees:

* Every state-changing call is deduplicated on the pair
  ``(operation_id, phase)`` per valve.  The opening change and the dedupe
  record commit in one local transaction, so a crash that happens *after*
  the device changed but *before* the application stored its receipt still
  leaves a recognizable executed action: the retried call returns the same
  result without moving the valve a second time.
* Executed actions are queryable (``executed_actions``).
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


@dataclass(frozen=True)
class DeviceAck:
    operation_id: str
    valve_id: str
    phase: str
    opening: int          # opening recorded by the device for this action
    deduped: bool         # True if the action had already been executed


@dataclass(frozen=True)
class ExecutedAction:
    operation_id: str
    valve_id: str
    phase: str
    opening: int
    executed_at: str


@dataclass(frozen=True)
class ValveState:
    valve_id: str
    opening: int


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
                    opening  INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS executed_actions (
                    operation_id TEXT NOT NULL,
                    valve_id     TEXT NOT NULL,
                    phase        TEXT NOT NULL,
                    opening      INTEGER NOT NULL,
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
                "SELECT valve_id, opening FROM valves ORDER BY valve_id"
            ).fetchall()
        return [ValveState(r["valve_id"], r["opening"]) for r in rows]

    def executed_actions(self, operation_id: Optional[str] = None) -> List[ExecutedAction]:
        with self._lock:
            if operation_id is None:
                rows = self._conn.execute(
                    "SELECT operation_id, valve_id, phase, opening, executed_at "
                    "FROM executed_actions ORDER BY seq"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT operation_id, valve_id, phase, opening, executed_at "
                    "FROM executed_actions WHERE operation_id = ? ORDER BY seq",
                    (operation_id,),
                ).fetchall()
        return [
            ExecutedAction(
                r["operation_id"], r["valve_id"], r["phase"],
                r["opening"], r["executed_at"],
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
    ) -> DeviceAck:
        """Apply (or de-duplicate) one action on a valve.

        ``claimed_initial`` is only used when the device has never seen the
        valve; the engineer-declared initial opening is what the simulated
        physical valve starts at.
        """
        crash_spec = os.environ.get("CRASH_AFTER", "").strip()
        with self._lock:
            # BEGIN IMMEDIATE-style serialization for the read/modify/write.
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    "SELECT opening FROM executed_actions "
                    "WHERE operation_id = ? AND valve_id = ? AND phase = ?",
                    (operation_id, valve_id, phase),
                ).fetchone()
                if existing is not None:
                    # Idempotent replay: same result, NO second state change.
                    self._conn.commit()
                    return DeviceAck(
                        operation_id, valve_id, phase,
                        existing["opening"], deduped=True,
                    )

                valve = self._conn.execute(
                    "SELECT opening FROM valves WHERE valve_id = ?", (valve_id,)
                ).fetchone()
                if valve is None:
                    self._conn.execute(
                        "INSERT INTO valves(valve_id, opening) VALUES(?, ?)",
                        (valve_id, claimed_initial),
                    )
                    current = claimed_initial
                else:
                    current = valve["opening"]

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

                seq_row = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS s FROM executed_actions"
                ).fetchone()
                self._conn.execute(
                    "UPDATE valves SET opening = ? WHERE valve_id = ?",
                    (new_opening, valve_id),
                )
                self._conn.execute(
                    "INSERT INTO executed_actions"
                    "(operation_id, valve_id, phase, opening, seq) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (operation_id, valve_id, phase, new_opening, seq_row["s"] + 1),
                )
                # Device-side change and dedupe record commit atomically.
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        # ---- crash point: device committed, application receipt not yet stored
        if crash_spec and crash_spec == f"{operation_id}:{valve_id}:{phase}":
            # Hard kill: no finally blocks, no receipt write.
            os._exit(CRASH_EXIT_CODE)

        return DeviceAck(operation_id, valve_id, phase, new_opening, deduped=False)
