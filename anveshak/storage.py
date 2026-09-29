"""SQLite persistence for cases and officer approvals."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id        TEXT PRIMARY KEY,
    case_reference TEXT NOT NULL,
    data_mode      TEXT NOT NULL,
    status         TEXT NOT NULL,          -- queued | running | done | failed
    request_json   TEXT NOT NULL,
    result_json    TEXT,
    findings_hash  TEXT,
    error          TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    case_id      TEXT NOT NULL,
    decision_id  TEXT NOT NULL,
    approved_by  TEXT NOT NULL,
    officer_id   TEXT NOT NULL,
    note         TEXT,
    approved_at  TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    PRIMARY KEY (case_id, decision_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CaseStore:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def create(self, case_id: str, case_reference: str, data_mode: str, request: dict) -> None:
        now = _now()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO cases (case_id, case_reference, data_mode, status, request_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (case_id, case_reference, data_mode, "queued", json.dumps(request), now, now),
            )

    def _update(self, case_id: str, **fields: Any) -> None:
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._db:
            self._db.execute(f"UPDATE cases SET {cols} WHERE case_id = ?", (*fields.values(), case_id))

    def set_running(self, case_id: str) -> None:
        self._update(case_id, status="running")

    def set_done(self, case_id: str, result_json: str, findings_hash: str) -> None:
        self._update(case_id, status="done", result_json=result_json, findings_hash=findings_hash)

    def set_failed(self, case_id: str, error: str) -> None:
        self._update(case_id, status="failed", error=error)

    def get(self, case_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        return dict(row) if row else None

    def list(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT case_id, case_reference, data_mode, status, findings_hash, error, created_at, updated_at FROM cases ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_approval(self, case_id: str, decision_id: str, approved_by: str, officer_id: str, note: str | None, receipt: dict) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO approvals (case_id, decision_id, approved_by, officer_id, note, approved_at, receipt_json) VALUES (?,?,?,?,?,?,?)",
                (case_id, decision_id, approved_by, officer_id, note, _now(), json.dumps(receipt)),
            )

    def approvals(self, case_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM approvals WHERE case_id = ? ORDER BY approved_at", (case_id,)).fetchall()
        return [dict(r) for r in rows]
