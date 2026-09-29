"""SQLite persistence: the case queue, approvals, alerts, watchlist, cross-case sightings, callbacks.

SQLite in WAL mode lets the API process and any number of `anveshak worker` processes on
the same host share one queue. Claiming a job is a single atomic UPDATE ... RETURNING, so
two workers can never run the same case. For multi-host deployments the same schema maps
directly onto PostgreSQL (see docs/adr/0015-scaling.md).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id          TEXT PRIMARY KEY,
    case_reference   TEXT NOT NULL,
    data_mode        TEXT NOT NULL,
    status           TEXT NOT NULL,          -- queued | running | done | failed
    request_json     TEXT NOT NULL,
    result_json      TEXT,
    findings_hash    TEXT,
    error            TEXT,
    sahyog_reference TEXT,
    callback_url     TEXT,
    claimed_by       TEXT,
    claimed_at       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cases_status ON cases(status, created_at);
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
CREATE TABLE IF NOT EXISTS alerts (
    alert_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    severity     TEXT NOT NULL,
    rule         TEXT NOT NULL,
    chain        TEXT,
    address      TEXT,
    case_id      TEXT,
    message      TEXT NOT NULL,
    data_json    TEXT,
    acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS alerts_created ON alerts(created_at);
CREATE TABLE IF NOT EXISTS watchlist (
    watch_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chain           TEXT NOT NULL,
    address         TEXT NOT NULL,
    case_id         TEXT,
    reason          TEXT NOT NULL,
    added_at        TEXT NOT NULL,
    last_checked_at TEXT,
    baseline_json   TEXT,                    -- transfer ids already known at the last check
    active          INTEGER NOT NULL DEFAULT 1,
    UNIQUE (chain, address, case_id)
);
CREATE TABLE IF NOT EXISTS sightings (
    chain    TEXT NOT NULL,
    address  TEXT NOT NULL,
    case_id  TEXT NOT NULL,
    role     TEXT NOT NULL,
    PRIMARY KEY (chain, address, case_id)
);
CREATE INDEX IF NOT EXISTS sightings_address ON sightings(chain, address);
CREATE TABLE IF NOT EXISTS callbacks (
    case_id     TEXT NOT NULL,
    url         TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    status      TEXT NOT NULL,
    detail      TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CaseStore:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=30000")
            self._db.executescript(SCHEMA)

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock, self._db:
            return self._db.execute(sql, params)

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    # ------------------------------------------------------------------ cases / queue

    def create(self, case_id: str, case_reference: str, data_mode: str, request: dict, sahyog_reference: str | None = None, callback_url: str | None = None) -> None:
        t = now()
        self._exec(
            "INSERT INTO cases (case_id, case_reference, data_mode, status, request_json, sahyog_reference, callback_url, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (case_id, case_reference, data_mode, "queued", json.dumps(request), sahyog_reference, callback_url, t, t),
        )

    def claim_next(self, worker_id: str) -> dict | None:
        """Atomically move the oldest queued case to running and return it."""
        with self._lock, self._db:
            row = self._db.execute(
                """UPDATE cases SET status='running', claimed_by=?, claimed_at=?, updated_at=?
                   WHERE case_id = (SELECT case_id FROM cases WHERE status='queued' ORDER BY created_at LIMIT 1) AND status='queued'
                   RETURNING *""",
                (worker_id, now(), now()),
            ).fetchone()
        return dict(row) if row else None

    def requeue_stale(self, older_than: timedelta = timedelta(hours=1)) -> int:
        cutoff = (datetime.now(timezone.utc) - older_than).isoformat()
        cur = self._exec("UPDATE cases SET status='queued', claimed_by=NULL, updated_at=? WHERE status='running' AND claimed_at < ?", (now(), cutoff))
        return cur.rowcount

    def set_done(self, case_id: str, result_json: str, findings_hash: str) -> None:
        self._exec("UPDATE cases SET status='done', result_json=?, findings_hash=?, updated_at=? WHERE case_id=?", (result_json, findings_hash, now(), case_id))

    def set_failed(self, case_id: str, error: str) -> None:
        self._exec("UPDATE cases SET status='failed', error=?, updated_at=? WHERE case_id=?", (error, now(), case_id))

    def get(self, case_id: str) -> dict | None:
        rows = self._all("SELECT * FROM cases WHERE case_id = ?", (case_id,))
        return rows[0] if rows else None

    def list(self, limit: int = 50) -> list[dict]:
        return self._all(
            "SELECT case_id, case_reference, data_mode, status, findings_hash, error, sahyog_reference, created_at, updated_at FROM cases ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )

    def done_results(self, limit: int = 1000) -> list[dict]:
        return self._all("SELECT case_id, case_reference, data_mode, result_json FROM cases WHERE status='done' ORDER BY created_at DESC LIMIT ?", (limit,))

    def status_counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self._all("SELECT status, COUNT(*) AS n FROM cases GROUP BY status")}

    # ------------------------------------------------------------------ approvals

    def record_approval(self, case_id: str, decision_id: str, approved_by: str, officer_id: str, note: str | None, receipt: dict) -> None:
        self._exec(
            "INSERT INTO approvals (case_id, decision_id, approved_by, officer_id, note, approved_at, receipt_json) VALUES (?,?,?,?,?,?,?)",
            (case_id, decision_id, approved_by, officer_id, note, now(), json.dumps(receipt)),
        )

    def approvals(self, case_id: str) -> list[dict]:
        return self._all("SELECT * FROM approvals WHERE case_id = ? ORDER BY approved_at", (case_id,))

    # ------------------------------------------------------------------ alerts

    def add_alert(self, severity: str, rule: str, message: str, chain: str | None = None, address: str | None = None, case_id: str | None = None, data: dict | None = None) -> int:
        cur = self._exec(
            "INSERT INTO alerts (created_at, severity, rule, chain, address, case_id, message, data_json) VALUES (?,?,?,?,?,?,?,?)",
            (now(), severity, rule, chain, address, case_id, message, json.dumps(data) if data else None),
        )
        return int(cur.lastrowid)

    def alerts(self, limit: int = 100, case_id: str | None = None, unacknowledged: bool = False) -> list[dict]:
        clauses, params = [], []
        if case_id:
            clauses.append("case_id = ?")
            params.append(case_id)
        if unacknowledged:
            clauses.append("acknowledged = 0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._all(f"SELECT * FROM alerts {where} ORDER BY alert_id DESC LIMIT ?", (*params, limit))

    def acknowledge(self, alert_id: int) -> bool:
        return self._exec("UPDATE alerts SET acknowledged = 1 WHERE alert_id = ?", (alert_id,)).rowcount == 1

    # ------------------------------------------------------------------ watchlist

    def watch(self, chain: str, address: str, case_id: str | None, reason: str) -> int | None:
        cur = self._exec("INSERT OR IGNORE INTO watchlist (chain, address, case_id, reason, added_at) VALUES (?,?,?,?,?)", (chain, address, case_id, reason, now()))
        return int(cur.lastrowid) if cur.rowcount else None

    def watches(self, active_only: bool = True) -> list[dict]:
        where = "WHERE active = 1" if active_only else ""
        return self._all(f"SELECT * FROM watchlist {where} ORDER BY watch_id")

    def update_watch(self, watch_id: int, baseline: list[str]) -> None:
        self._exec("UPDATE watchlist SET baseline_json = ?, last_checked_at = ? WHERE watch_id = ?", (json.dumps(baseline), now(), watch_id))

    def deactivate_watch(self, watch_id: int) -> bool:
        return self._exec("UPDATE watchlist SET active = 0 WHERE watch_id = ?", (watch_id,)).rowcount == 1

    # ------------------------------------------------------------------ sightings (cross-case links)

    def record_sightings(self, case_id: str, rows: list[tuple[str, str, str]]) -> None:
        with self._lock, self._db:
            self._db.executemany("INSERT OR IGNORE INTO sightings (chain, address, case_id, role) VALUES (?,?,?,?)", [(c, a, case_id, r) for c, a, r in rows])

    def other_cases_for(self, case_id: str) -> list[dict]:
        return self._all(
            """SELECT s2.chain, s2.address, s2.case_id AS other_case_id, s2.role AS other_role, s1.role AS role, c.case_reference AS other_reference
               FROM sightings s1 JOIN sightings s2 ON s1.chain = s2.chain AND s1.address = s2.address AND s2.case_id != s1.case_id
               JOIN cases c ON c.case_id = s2.case_id
               WHERE s1.case_id = ? ORDER BY s2.chain, s2.address""",
            (case_id,),
        )

    # ------------------------------------------------------------------ callbacks

    def record_callback(self, case_id: str, url: str, status: str, detail: str | None) -> None:
        self._exec("INSERT INTO callbacks (case_id, url, attempted_at, status, detail) VALUES (?,?,?,?,?)", (case_id, url, now(), status, detail))

    def callbacks(self, case_id: str) -> list[dict]:
        return self._all("SELECT * FROM callbacks WHERE case_id = ? ORDER BY attempted_at", (case_id,))

    def raw(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return self._all(sql, params)
