"""SQLite persistence: the case queue, approvals, alerts, watchlist, cross-case sightings, callbacks,
recommendation outcomes (append-only) and the audit log (append-only, hash-chained).

SQLite in WAL mode lets the API process and any number of `anveshak worker` processes on
the same host share one queue. Claiming a job is a single atomic UPDATE ... RETURNING, so
two workers can never run the same case. For multi-host deployments the same schema maps
directly onto PostgreSQL (see docs/adr/0015-scaling.md).
"""

from __future__ import annotations

import hashlib
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
    agency_id        TEXT,                    -- the agency whose case this is (scopes visibility)
    parent_case_id   TEXT,                    -- set when the case is a re-run of another
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
    agency_id       TEXT,
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
CREATE TABLE IF NOT EXISTS recommendation_status (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id           TEXT NOT NULL,
    recommendation_id TEXT NOT NULL,
    status            TEXT NOT NULL,
    sahyog_request_id TEXT,
    note              TEXT,
    reported_by       TEXT,
    client_id         TEXT NOT NULL,
    recorded_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS recommendation_status_case ON recommendation_status(case_id, seq);
CREATE TRIGGER IF NOT EXISTS recommendation_status_no_update BEFORE UPDATE ON recommendation_status
BEGIN SELECT RAISE(ABORT, 'recommendation_status is append-only'); END;
CREATE TRIGGER IF NOT EXISTS recommendation_status_no_delete BEFORE DELETE ON recommendation_status
BEGIN SELECT RAISE(ABORT, 'recommendation_status is append-only'); END;
CREATE TABLE IF NOT EXISTS audit_log (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    client_id   TEXT NOT NULL,
    agency_id   TEXT,
    ip          TEXT,
    method      TEXT NOT NULL,
    path        TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    case_id     TEXT,
    prev_hash   TEXT NOT NULL,
    entry_hash  TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""

AUDIT_FIELDS = ("seq", "at", "client_id", "agency_id", "ip", "method", "path", "status_code", "case_id")


def audit_hash(prev_hash: str, entry: dict) -> str:
    body = json.dumps({k: entry[k] for k in AUDIT_FIELDS}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev_hash + body).encode()).hexdigest()


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
            self._migrate()
            self._db.executescript(SCHEMA)

    # Columns added after the first release; older databases get them via ALTER TABLE.
    _ADDED_COLUMNS = {
        "cases": [("sahyog_reference", "TEXT"), ("callback_url", "TEXT"), ("claimed_by", "TEXT"), ("claimed_at", "TEXT"), ("agency_id", "TEXT"), ("parent_case_id", "TEXT")],
        "watchlist": [("agency_id", "TEXT")],
    }

    def _migrate(self) -> None:
        """Additive, idempotent schema migration for databases created by earlier versions."""
        for table, columns in self._ADDED_COLUMNS.items():
            existing = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})").fetchall()}
            if not existing:
                continue  # table not created yet — SCHEMA creates it complete
            for name, sql_type in columns:
                if name not in existing:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
        self._db.commit()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock, self._db:
            return self._db.execute(sql, params)

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    # ------------------------------------------------------------------ cases / queue

    def create(
        self, case_id: str, case_reference: str, data_mode: str, request: dict, sahyog_reference: str | None = None, callback_url: str | None = None,
        agency_id: str | None = None, parent_case_id: str | None = None,
    ) -> None:
        t = now()
        self._exec(
            "INSERT INTO cases (case_id, case_reference, data_mode, status, request_json, sahyog_reference, callback_url, agency_id, parent_case_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (case_id, case_reference, data_mode, "queued", json.dumps(request), sahyog_reference, callback_url, agency_id, parent_case_id, t, t),
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

    @staticmethod
    def _scope(agency: str | None | object, column: str = "agency_id") -> tuple[str, tuple]:
        """`ALL` = no restriction; otherwise only rows of that agency (None = rows without agency)."""
        if agency is ALL:
            return "1=1", ()
        if agency is None:
            return f"{column} IS NULL", ()
        return f"{column} = ?", (agency,)

    def list(self, limit: int = 50, agency: str | None | object = None) -> list[dict]:
        where, params = self._scope(agency if agency is not None else ALL)
        return self._all(
            f"SELECT case_id, case_reference, data_mode, status, findings_hash, error, sahyog_reference, agency_id, parent_case_id, created_at, updated_at FROM cases WHERE {where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        )

    def done_results(self, limit: int = 1000, agency: str | None | object = None) -> list[dict]:
        where, params = self._scope(agency if agency is not None else ALL)
        return self._all(f"SELECT case_id, case_reference, data_mode, result_json FROM cases WHERE status='done' AND {where} ORDER BY created_at DESC LIMIT ?", (*params, limit))

    def status_counts(self, agency: str | None | object = None) -> dict[str, int]:
        where, params = self._scope(agency if agency is not None else ALL)
        return {r["status"]: r["n"] for r in self._all(f"SELECT status, COUNT(*) AS n FROM cases WHERE {where} GROUP BY status", params)}

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

    def alerts(self, limit: int = 100, case_id: str | None = None, unacknowledged: bool = False, agency: str | None | object = None) -> list[dict]:
        clauses, params = [], []
        if case_id:
            clauses.append("a.case_id = ?")
            params.append(case_id)
        if unacknowledged:
            clauses.append("a.acknowledged = 0")
        if agency is not None and agency is not ALL:
            clauses.append("c.agency_id = ?")
            params.append(agency)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._all(f"SELECT a.* FROM alerts a LEFT JOIN cases c ON c.case_id = a.case_id {where} ORDER BY a.alert_id DESC LIMIT ?", (*params, limit))

    def alert(self, alert_id: int) -> dict | None:
        rows = self._all("SELECT a.*, c.agency_id AS case_agency_id FROM alerts a LEFT JOIN cases c ON c.case_id = a.case_id WHERE a.alert_id = ?", (alert_id,))
        return rows[0] if rows else None

    def acknowledge(self, alert_id: int) -> bool:
        return self._exec("UPDATE alerts SET acknowledged = 1 WHERE alert_id = ?", (alert_id,)).rowcount == 1

    # ------------------------------------------------------------------ watchlist

    def watch(self, chain: str, address: str, case_id: str | None, reason: str, agency_id: str | None = None) -> int | None:
        cur = self._exec(
            "INSERT OR IGNORE INTO watchlist (chain, address, case_id, reason, added_at, agency_id) VALUES (?,?,?,?,?,?)",
            (chain, address, case_id, reason, now(), agency_id),
        )
        return int(cur.lastrowid) if cur.rowcount else None

    def watches(self, active_only: bool = True, agency: str | None | object = None) -> list[dict]:
        clauses = ["active = 1"] if active_only else []
        params: tuple = ()
        if agency is not None and agency is not ALL:
            clauses.append("agency_id = ?")
            params = (agency,)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._all(f"SELECT * FROM watchlist {where} ORDER BY watch_id", params)

    def get_watch(self, watch_id: int) -> dict | None:
        rows = self._all("SELECT * FROM watchlist WHERE watch_id = ?", (watch_id,))
        return rows[0] if rows else None

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
            """SELECT s2.chain, s2.address, s2.case_id AS other_case_id, s2.role AS other_role, s1.role AS role, c.case_reference AS other_reference,
                      c.agency_id AS other_agency_id
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

    # ------------------------------------------------------------------ recommendation outcomes (append-only)

    def add_status(self, case_id: str, recommendation_id: str, status: str, sahyog_request_id: str | None, note: str | None, reported_by: str | None, client_id: str) -> int:
        cur = self._exec(
            "INSERT INTO recommendation_status (case_id, recommendation_id, status, sahyog_request_id, note, reported_by, client_id, recorded_at) VALUES (?,?,?,?,?,?,?,?)",
            (case_id, recommendation_id, status, sahyog_request_id, note, reported_by, client_id, now()),
        )
        return int(cur.lastrowid)

    def statuses(self, case_id: str | None = None) -> list[dict]:
        if case_id is None:
            return self._all("SELECT * FROM recommendation_status ORDER BY seq")
        return self._all("SELECT * FROM recommendation_status WHERE case_id = ? ORDER BY seq", (case_id,))

    # ------------------------------------------------------------------ audit log (append-only, hash-chained)

    def audit(self, client_id: str, agency_id: str | None, ip: str | None, method: str, path: str, status_code: int, case_id: str | None) -> None:
        with self._lock, self._db:
            last = self._db.execute("SELECT entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
            prev = last[0] if last else "0" * 64
            seq = (self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM audit_log").fetchone()[0] or 0) + 1
            entry = {"seq": seq, "at": now(), "client_id": client_id, "agency_id": agency_id, "ip": ip, "method": method, "path": path, "status_code": status_code, "case_id": case_id}
            self._db.execute(
                "INSERT INTO audit_log (seq, at, client_id, agency_id, ip, method, path, status_code, case_id, prev_hash, entry_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (*(entry[k] for k in AUDIT_FIELDS), prev, audit_hash(prev, entry)),
            )

    def audit_entries(self, limit: int = 200, client_id: str | None = None, case_id: str | None = None) -> list[dict]:
        clauses, params = [], []
        if client_id:
            clauses.append("client_id = ?")
            params.append(client_id)
        if case_id:
            clauses.append("case_id = ?")
            params.append(case_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._all(f"SELECT * FROM audit_log {where} ORDER BY seq DESC LIMIT ?", (*params, limit))

    def verify_audit(self) -> dict:
        prev = "0" * 64
        count = 0
        for row in self._all("SELECT * FROM audit_log ORDER BY seq"):
            if row["prev_hash"] != prev or audit_hash(prev, row) != row["entry_hash"]:
                return {"intact": False, "entries": count, "first_bad_seq": row["seq"]}
            prev = row["entry_hash"]
            count += 1
        return {"intact": True, "entries": count, "head": prev}

    def raw(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return self._all(sql, params)


ALL = object()  # scope marker: every agency
