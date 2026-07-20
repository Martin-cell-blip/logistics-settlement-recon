"""Transactional store for human decisions and product audit events."""
from __future__ import annotations

import json
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


class DecisionConflict(RuntimeError):
    def __init__(self, existing: dict):
        super().__init__("case already has a decision")
        self.existing = existing


class OperationalStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def _initialize(self) -> None:
        with self._lock, self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS human_decisions (
                    id INTEGER PRIMARY KEY,
                    case_id TEXT NOT NULL UNIQUE,
                    recorded_at TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    human_decision TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    recommended_action TEXT NOT NULL,
                    impact_amount REAL NOT NULL,
                    policy_version TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    previous_state TEXT NOT NULL,
                    new_state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS product_events (
                    id INTEGER PRIMARY KEY,
                    recorded_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    case_id TEXT,
                    session_id TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_case
                    ON product_events(case_id, recorded_at);
                """
            )

    @staticmethod
    def _record(row: sqlite3.Row | None) -> dict | None:
        return dict(row) if row is not None else None

    def latest_decision(self, case_id: str) -> dict | None:
        with self._lock, self._connect() as con:
            return self._record(
                con.execute(
                    "SELECT * FROM human_decisions WHERE case_id=?", (case_id,)
                ).fetchone()
            )

    def submit_decision(self, decision: dict) -> tuple[dict, bool]:
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT * FROM human_decisions WHERE case_id=?", (decision["case_id"],)
            ).fetchone()
            if existing is not None:
                existing_record = dict(existing)
                if existing_record["idempotency_key"] == decision["idempotency_key"]:
                    return existing_record, True
                raise DecisionConflict(existing_record)
            columns = list(decision)
            placeholders = ",".join("?" for _ in columns)
            con.execute(
                f"INSERT INTO human_decisions ({','.join(columns)}) VALUES ({placeholders})",
                [decision[column] for column in columns],
            )
            self._insert_event(
                con,
                {
                    "recorded_at": decision["recorded_at"],
                    "event_type": "DECISION_SUBMITTED",
                    "case_id": decision["case_id"],
                    "session_id": decision["idempotency_key"],
                    "metadata_json": json.dumps(
                        {
                            "decision": decision["human_decision"],
                            "reviewer": decision["reviewer"],
                            "policy_version": decision["policy_version"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            )
            saved = con.execute(
                "SELECT * FROM human_decisions WHERE case_id=?", (decision["case_id"],)
            ).fetchone()
            return dict(saved), False

    def append_event(self, event: dict) -> None:
        with self._lock, self._connect() as con:
            self._insert_event(con, event)

    @staticmethod
    def _insert_event(con: sqlite3.Connection, event: dict) -> None:
        con.execute(
            """INSERT INTO product_events
               (recorded_at,event_type,case_id,session_id,metadata_json)
               VALUES (?,?,?,?,?)""",
            (
                event["recorded_at"],
                event["event_type"],
                event.get("case_id"),
                event["session_id"],
                event["metadata_json"],
            ),
        )

    def metrics(self) -> dict:
        with self._lock, self._connect() as con:
            counts = Counter(
                row["human_decision"]
                for row in con.execute(
                    "SELECT human_decision FROM human_decisions"
                ).fetchall()
            )
            return {
                "decision_counts": dict(counts),
                "total_decisions": sum(counts.values()),
            }

    def case_events(self, case_id: str) -> list[dict]:
        with self._lock, self._connect() as con:
            return [
                dict(row)
                for row in con.execute(
                    "SELECT * FROM product_events WHERE case_id=? ORDER BY id",
                    (case_id,),
                ).fetchall()
            ]

    def close(self) -> None:
        """Compatibility hook for fixtures; connections are short-lived."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
