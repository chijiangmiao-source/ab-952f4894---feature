"""Durable storage for switch intents and per-valve action records.

Two tables capture the full saga state:

* ``switches``  - the intent (full request payload) plus current phase.
* ``actions``   - one row per valve with forward/compensation outcomes,
  including the expected/actual revision of every conditional device
  change (the receipt the saga reconciles against after a restart).

A process-wide lock plus SQLite transactions make each state transition
atomic; WAL mode lets a verifier read while the service is up.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

INITIAL_PHASE = "PENDING"


@dataclass
class ActionRecord:
    valve_id: str
    idx: int
    initial_opening: int
    target_opening: int
    forward_status: str = "PENDING"        # PENDING/SUCCESS/FAILED/SKIPPED/FENCED
    forward_opening: Optional[int] = None
    forward_deduped: bool = False
    forward_error: Optional[str] = None
    forward_expected_revision: Optional[int] = None
    forward_actual_revision: Optional[int] = None
    compensate_status: str = "PENDING"
    compensate_opening: Optional[int] = None
    compensate_deduped: bool = False
    compensate_error: Optional[str] = None
    compensate_expected_revision: Optional[int] = None
    compensate_actual_revision: Optional[int] = None


@dataclass
class SwitchRecord:
    operation_id: str
    phase: str
    failure: Optional[str]
    payload: Dict
    actions: List[ActionRecord] = field(default_factory=list)


class SwitchStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS switches (
                    operation_id TEXT PRIMARY KEY,
                    phase        TEXT NOT NULL,
                    failure      TEXT,
                    payload      TEXT NOT NULL,
                    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS actions (
                    operation_id       TEXT NOT NULL REFERENCES switches(operation_id),
                    idx                INTEGER NOT NULL,
                    valve_id           TEXT NOT NULL,
                    initial_opening    INTEGER NOT NULL,
                    target_opening     INTEGER NOT NULL,
                    forward_status     TEXT NOT NULL DEFAULT 'PENDING',
                    forward_opening    INTEGER,
                    forward_deduped    INTEGER NOT NULL DEFAULT 0,
                    forward_error      TEXT,
                    forward_expected_revision INTEGER,
                    forward_actual_revision   INTEGER,
                    compensate_status  TEXT NOT NULL DEFAULT 'PENDING',
                    compensate_opening INTEGER,
                    compensate_deduped INTEGER NOT NULL DEFAULT 0,
                    compensate_error   TEXT,
                    compensate_expected_revision INTEGER,
                    compensate_actual_revision   INTEGER,
                    PRIMARY KEY (operation_id, valve_id)
                );
                """
            )
            self._migrate()

    def _migrate(self) -> None:
        """Add revision columns to databases created by older versions."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(actions)")}
        for col in (
            "forward_expected_revision", "forward_actual_revision",
            "compensate_expected_revision", "compensate_actual_revision",
        ):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE actions ADD COLUMN {col} INTEGER")

    # ----------------------------------------------------------------- helpers

    def _hydrate(self, row: sqlite3.Row) -> SwitchRecord:
        actions = []
        for a in self._conn.execute(
            "SELECT * FROM actions WHERE operation_id = ? ORDER BY idx",
            (row["operation_id"],),
        ).fetchall():
            actions.append(
                ActionRecord(
                    valve_id=a["valve_id"],
                    idx=a["idx"],
                    initial_opening=a["initial_opening"],
                    target_opening=a["target_opening"],
                    forward_status=a["forward_status"],
                    forward_opening=a["forward_opening"],
                    forward_deduped=bool(a["forward_deduped"]),
                    forward_error=a["forward_error"],
                    forward_expected_revision=a["forward_expected_revision"],
                    forward_actual_revision=a["forward_actual_revision"],
                    compensate_status=a["compensate_status"],
                    compensate_opening=a["compensate_opening"],
                    compensate_deduped=bool(a["compensate_deduped"]),
                    compensate_error=a["compensate_error"],
                    compensate_expected_revision=a["compensate_expected_revision"],
                    compensate_actual_revision=a["compensate_actual_revision"],
                )
            )
        return SwitchRecord(
            operation_id=row["operation_id"],
            phase=row["phase"],
            failure=row["failure"],
            payload=json.loads(row["payload"]),
            actions=actions,
        )

    def get(self, operation_id: str) -> Optional[SwitchRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM switches WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            return self._hydrate(row) if row else None

    def all(self) -> List[SwitchRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM switches ORDER BY created_at"
            ).fetchall()
            return [self._hydrate(r) for r in rows]

    @property
    def lock(self) -> threading.RLock:
        """Saga engine takes this for whole-run atomicity per process."""
        return self._lock

    # ----------------------------------------------------------------- writers

    def create_intent(self, operation_id: str, payload: Dict,
                      actions: List[ActionRecord]) -> SwitchRecord:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO switches(operation_id, phase, failure, payload) "
                "VALUES(?, ?, NULL, ?)",
                (operation_id, INITIAL_PHASE, json.dumps(payload, sort_keys=True)),
            )
            self._conn.executemany(
                "INSERT INTO actions(operation_id, idx, valve_id, "
                "initial_opening, target_opening, forward_expected_revision) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                [
                    (operation_id, a.idx, a.valve_id,
                     a.initial_opening, a.target_opening,
                     a.forward_expected_revision)
                    for a in actions
                ],
            )
        rec = self.get(operation_id)
        assert rec is not None
        return rec

    def set_phase(self, operation_id: str, phase: str,
                  failure: Optional[str] = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE switches SET phase = ?, failure = COALESCE(?, failure), "
                "updated_at = datetime('now') WHERE operation_id = ?",
                (phase, failure, operation_id),
            )

    def mark_forward(self, operation_id: str, a: ActionRecord) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE actions SET forward_status = ?, forward_opening = ?, "
                "forward_deduped = ?, forward_error = ?, "
                "forward_expected_revision = ?, forward_actual_revision = ? "
                "WHERE operation_id = ? AND valve_id = ?",
                (a.forward_status, a.forward_opening,
                 int(a.forward_deduped), a.forward_error,
                 a.forward_expected_revision, a.forward_actual_revision,
                 operation_id, a.valve_id),
            )
            self._conn.execute(
                "UPDATE switches SET updated_at = datetime('now') "
                "WHERE operation_id = ?",
                (operation_id,),
            )

    def mark_compensate(self, operation_id: str, a: ActionRecord) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE actions SET compensate_status = ?, compensate_opening = ?, "
                "compensate_deduped = ?, compensate_error = ?, "
                "compensate_expected_revision = ?, compensate_actual_revision = ? "
                "WHERE operation_id = ? AND valve_id = ?",
                (a.compensate_status, a.compensate_opening,
                 int(a.compensate_deduped), a.compensate_error,
                 a.compensate_expected_revision, a.compensate_actual_revision,
                 operation_id, a.valve_id),
            )
            self._conn.execute(
                "UPDATE switches SET updated_at = datetime('now') "
                "WHERE operation_id = ?",
                (operation_id,),
            )

    def reset(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                "DELETE FROM actions; DELETE FROM switches;"
            )


def record_to_dict(rec: SwitchRecord) -> Dict:
    return asdict(rec)
