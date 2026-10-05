"""SQLite-backed persistence.

Concurrency model:
* a process-wide reentrant lock serialises access within the process;
* every write runs in an ``IMMEDIATE`` transaction so cross-process writers
  are serialised by SQLite itself;
* compare-and-set updates carry the expected row version / status so a lost
  update surfaces as ``False`` instead of silently overwriting data;
* partial unique indexes enforce "one open case per target" and
  "one active action per account" at the storage layer.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from .enums import (
    ActionStatus,
    ActionType,
    AppealStatus,
    CaseStatus,
    Priority,
)
from .models import Action, Appeal, AuditEntry, Case, Report

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id                TEXT PRIMARY KEY,
    dedup_key         TEXT NOT NULL UNIQUE,
    source            TEXT NOT NULL,
    reporter_id       TEXT NOT NULL,
    target_account_id TEXT NOT NULL,
    event_key         TEXT NOT NULL,
    case_id           TEXT NOT NULL,
    evidence          TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_target ON reports(target_account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_reports_reporter ON reports(reporter_id, created_at);

CREATE TABLE IF NOT EXISTS dedup_counters (
    key        TEXT PRIMARY KEY,
    count      INTEGER NOT NULL,
    first_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    id                TEXT PRIMARY KEY,
    target_account_id TEXT NOT NULL UNIQUE,
    status            TEXT NOT NULL,
    priority          TEXT NOT NULL,
    evidence_count    INTEGER NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    version           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id           TEXT PRIMARY KEY,
    case_id      TEXT NOT NULL,
    account_id   TEXT NOT NULL,
    action_type  TEXT NOT NULL,
    status       TEXT NOT NULL,
    reason       TEXT NOT NULL,
    basis        TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    expires_at   TEXT,
    decided_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    auto         INTEGER NOT NULL DEFAULT 0,
    applied_at   TEXT,
    released_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_account ON actions(account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_actions_case ON actions(case_id);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status, expires_at);
-- storage-level guarantee: at most one ACTIVE action per account
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_action
    ON actions(account_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS appeals (
    id              TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL,
    account_id      TEXT NOT NULL,
    status          TEXT NOT NULL,
    reason          TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    decided_by      TEXT,
    decided_at      TEXT,
    decision_reason TEXT,
    UNIQUE(case_id, account_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor_id    TEXT NOT NULL,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    allowed     INTEGER NOT NULL,
    detail      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id, id);

CREATE TABLE IF NOT EXISTS reporter_hits (
    reporter_id TEXT NOT NULL,
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hits_reporter ON reporter_hits(reporter_id, ts);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone().astimezone().isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Repository:
    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # -- connection / schema -------------------------------------------
    def connect(self) -> "Repository":
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        return self

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serialised immediate-write transaction."""
        assert self._conn is not None, "repository not connected"
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # -- reports -------------------------------------------------------
    def upsert_report(self, report: Report) -> tuple[Report, bool, int]:
        """Insert a report unless its dedup key already exists.

        Returns ``(stored_report, is_new, duplicate_count)`` where the counter
        records how many times the same submission was seen (1 = first time).
        """
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO reports VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    report.id,
                    report.dedup_key,
                    report.source,
                    report.reporter_id,
                    report.target_account_id,
                    report.event_key,
                    report.case_id,
                    report.evidence,
                    _iso(report.created_at),
                ),
            )
            is_new = cur.rowcount > 0
            if is_new:
                c.execute(
                    "INSERT INTO dedup_counters(key, count, first_seen) VALUES (?,?,?)",
                    (report.dedup_key, 1, _iso(report.created_at)),
                )
                count = 1
            else:
                c.execute(
                    "UPDATE dedup_counters SET count = count + 1 WHERE key = ?",
                    (report.dedup_key,),
                )
                row = c.execute(
                    "SELECT count FROM dedup_counters WHERE key = ?",
                    (report.dedup_key,),
                ).fetchone()
                count = row["count"]
            stored = self.get_report(report.dedup_key)
        assert stored is not None
        return stored, is_new, count

    def get_report(self, dedup_key: str) -> Report | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reports WHERE dedup_key = ?", (dedup_key,)
            ).fetchone()
        return _report_row(row) if row else None

    # -- cases ---------------------------------------------------------
    def insert_case(self, case: Case) -> Case:
        with self.tx() as c:
            c.execute(
                "INSERT INTO cases VALUES (?,?,?,?,?,?,?,?)",
                (
                    case.id,
                    case.target_account_id,
                    case.status.value,
                    case.priority.value,
                    case.evidence_count,
                    _iso(case.created_at),
                    _iso(case.updated_at),
                    case.version,
                ),
            )
        return case

    def get_case(self, case_id: str) -> Case | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cases WHERE id = ?", (case_id,)
            ).fetchone()
        return _case_row(row) if row else None

    def get_case_for_target(self, target_account_id: str) -> Case | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cases WHERE target_account_id = ? ORDER BY created_at DESC LIMIT 1",
                (target_account_id,),
            ).fetchone()
        return _case_row(row) if row else None

    def cas_case(
        self,
        case_id: str,
        expected_version: int,
        *,
        status: CaseStatus | None = None,
        priority: Priority | None = None,
        evidence_delta: int = 0,
        now: datetime,
    ) -> Case | None:
        """Compare-and-set a case row.

        Fails (returns None) if the version moved concurrently, preventing
        status rollback and mixed field writes.
        """
        sets = ["version = version + 1", "updated_at = ?"]
        params: list[object] = [_iso(now)]
        if status is not None:
            sets.append("status = ?")
            params.append(status.value)
        if priority is not None:
            sets.append("priority = ?")
            params.append(priority.value)
        if evidence_delta:
            sets.append("evidence_count = evidence_count + ?")
            params.append(evidence_delta)
        params.extend([case_id, expected_version])
        with self.tx() as c:
            cur = c.execute(
                f"UPDATE cases SET {', '.join(sets)} WHERE id = ? AND version = ?",
                params,
            )
            if cur.rowcount == 0:
                return None
            row = c.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        return _case_row(row)

    def list_cases(self) -> list[Case]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cases ORDER BY created_at"
            ).fetchall()
        return [_case_row(r) for r in rows]

    def reassign_report_case(self, report_id: str, case_id: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE reports SET case_id = ? WHERE id = ?", (case_id, report_id)
            )

    # -- actions -------------------------------------------------------
    def insert_action(self, action: Action) -> Action:
        with self.tx() as c:
            c.execute(
                "INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    action.id,
                    action.case_id,
                    action.account_id,
                    action.action_type.value,
                    action.status.value,
                    action.reason,
                    action.basis,
                    _iso(action.effective_at),
                    _iso(action.expires_at) if action.expires_at else None,
                    action.decided_by,
                    _iso(action.created_at),
                    1 if action.auto else 0,
                    _iso(action.applied_at) if action.applied_at else None,
                    _iso(action.released_at) if action.released_at else None,
                ),
            )
        return action

    def get_action(self, action_id: str) -> Action | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM actions WHERE id = ?", (action_id,)
            ).fetchone()
        return _action_row(row) if row else None

    def get_active_action(self, account_id: str) -> Action | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM actions WHERE account_id = ? AND status = 'active'",
                (account_id,),
            ).fetchone()
        return _action_row(row) if row else None

    def list_actions_for_account(self, account_id: str) -> list[Action]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM actions WHERE account_id = ? ORDER BY created_at, id",
                (account_id,),
            ).fetchall()
        return [_action_row(r) for r in rows]

    def list_actions_by_case(self, case_id: str) -> list[Action]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM actions WHERE case_id = ? ORDER BY created_at, id",
                (case_id,),
            ).fetchall()
        return [_action_row(r) for r in rows]

    def list_actions_by_status(self, status: ActionStatus) -> list[Action]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM actions WHERE status = ? ORDER BY created_at, id",
                (status.value,),
            ).fetchall()
        return [_action_row(r) for r in rows]

    def cas_action_status(
        self,
        action_id: str,
        expected: ActionStatus,
        new: ActionStatus,
        now: datetime,
    ) -> bool:
        with self.tx() as c:
            if new == ActionStatus.ACTIVE:
                cur = c.execute(
                    "UPDATE actions SET status = ?, applied_at = COALESCE(applied_at, ?) "
                    "WHERE id = ? AND status = ?",
                    (new.value, _iso(now), action_id, expected.value),
                )
            elif new in (ActionStatus.EXPIRED, ActionStatus.REVOKED):
                cur = c.execute(
                    "UPDATE actions SET status = ?, released_at = ? "
                    "WHERE id = ? AND status = ?",
                    (new.value, _iso(now), action_id, expected.value),
                )
            else:
                cur = c.execute(
                    "UPDATE actions SET status = ? WHERE id = ? AND status = ?",
                    (new.value, action_id, expected.value),
                )
            return cur.rowcount > 0

    def list_due_actions(self, now: datetime) -> list[Action]:
        """Active, expiry-bearing actions whose expiry has passed."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM actions WHERE status = 'active' "
                "AND expires_at IS NOT NULL AND expires_at <= ?",
                (_iso(now),),
            ).fetchall()
        return [_action_row(r) for r in rows]

    # -- appeals -------------------------------------------------------
    def insert_appeal(self, appeal: Appeal) -> Appeal:
        with self.tx() as c:
            c.execute(
                "INSERT INTO appeals VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    appeal.id,
                    appeal.case_id,
                    appeal.account_id,
                    appeal.status.value,
                    appeal.reason,
                    appeal.created_by,
                    _iso(appeal.created_at),
                    appeal.decided_by,
                    _iso(appeal.decided_at) if appeal.decided_at else None,
                    appeal.decision_reason,
                ),
            )
        return appeal

    def get_appeal(self, case_id: str, account_id: str) -> Appeal | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM appeals WHERE case_id = ? AND account_id = ?",
                (case_id, account_id),
            ).fetchone()
        return _appeal_row(row) if row else None

    def get_appeal_by_id(self, appeal_id: str) -> Appeal | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM appeals WHERE id = ?", (appeal_id,)
            ).fetchone()
        return _appeal_row(row) if row else None

    def cas_appeal_decision(
        self,
        appeal_id: str,
        new_status: AppealStatus,
        decided_by: str,
        reason: str,
        now: datetime,
    ) -> bool:
        """Only a still-pending appeal can be decided; concurrent deciders
        cannot both succeed."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE appeals SET status = ?, decided_by = ?, decided_at = ?, "
                "decision_reason = ? WHERE id = ? AND status = 'pending'",
                (
                    new_status.value,
                    decided_by,
                    _iso(now),
                    reason,
                    appeal_id,
                ),
            )
            return cur.rowcount > 0

    # -- audit ---------------------------------------------------------
    def insert_audit(self, entry: AuditEntry) -> AuditEntry:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO audit_log(ts, actor_id, action, entity_type, entity_id, "
                "allowed, detail) VALUES (?,?,?,?,?,?,?)",
                (
                    _iso(entry.ts),
                    entry.actor_id,
                    entry.action,
                    entry.entity_type,
                    entry.entity_id,
                    1 if entry.allowed else 0,
                    entry.detail,
                ),
            )
            entry.id = cur.lastrowid
        return entry

    def list_audit(
        self, entity_type: str | None = None, entity_id: str | None = None
    ) -> list[AuditEntry]:
        sql = "SELECT * FROM audit_log"
        clauses: list[str] = []
        params: list[object] = []
        if entity_type:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        if entity_id:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            AuditEntry(
                id=r["id"],
                ts=_dt(r["ts"]),
                actor_id=r["actor_id"],
                action=r["action"],
                entity_type=r["entity_type"],
                entity_id=r["entity_id"],
                allowed=bool(r["allowed"]),
                detail=r["detail"],
            )
            for r in rows
        ]

    # -- rate limiting / thresholds ------------------------------------
    def add_reporter_hit(self, reporter_id: str, now: datetime) -> int:
        with self.tx() as c:
            c.execute(
                "INSERT INTO reporter_hits(reporter_id, ts) VALUES (?,?)",
                (reporter_id, _iso(now)),
            )
        return 0

    def prune_and_count_hits(self, reporter_id: str, window_start: datetime) -> int:
        with self.tx() as c:
            c.execute(
                "DELETE FROM reporter_hits WHERE reporter_id = ? AND ts < ?",
                (reporter_id, _iso(window_start)),
            )
            row = c.execute(
                "SELECT COUNT(*) AS n FROM reporter_hits WHERE reporter_id = ?",
                (reporter_id,),
            ).fetchone()
        return row["n"]

    def count_distinct_reporters(
        self, target_account_id: str, since: datetime
    ) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(DISTINCT reporter_id) AS n FROM reports "
                "WHERE target_account_id = ? AND created_at >= ?",
                (target_account_id, _iso(since)),
            ).fetchone()
        return row["n"]

    def count_reports_for_target(self, target_account_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM reports WHERE target_account_id = ?",
                (target_account_id,),
            ).fetchone()
        return row["n"]


# -- row mappers ----------------------------------------------------------
def _report_row(row: sqlite3.Row) -> Report:
    return Report(
        id=row["id"],
        dedup_key=row["dedup_key"],
        source=row["source"],
        reporter_id=row["reporter_id"],
        target_account_id=row["target_account_id"],
        event_key=row["event_key"],
        case_id=row["case_id"],
        evidence=row["evidence"],
        created_at=_dt(row["created_at"]),
    )


def _case_row(row: sqlite3.Row) -> Case:
    return Case(
        id=row["id"],
        target_account_id=row["target_account_id"],
        status=CaseStatus(row["status"]),
        priority=Priority(row["priority"]),
        evidence_count=row["evidence_count"],
        created_at=_dt(row["created_at"]),
        updated_at=_dt(row["updated_at"]),
        version=row["version"],
    )


def _action_row(row: sqlite3.Row) -> Action:
    return Action(
        id=row["id"],
        case_id=row["case_id"],
        account_id=row["account_id"],
        action_type=ActionType(row["action_type"]),
        status=ActionStatus(row["status"]),
        reason=row["reason"],
        basis=row["basis"],
        effective_at=_dt(row["effective_at"]),
        expires_at=_dt(row["expires_at"]) if row["expires_at"] else None,
        decided_by=row["decided_by"],
        created_at=_dt(row["created_at"]),
        auto=bool(row["auto"]),
        applied_at=_dt(row["applied_at"]) if row["applied_at"] else None,
        released_at=_dt(row["released_at"]) if row["released_at"] else None,
    )


def _appeal_row(row: sqlite3.Row) -> Appeal:
    return Appeal(
        id=row["id"],
        case_id=row["case_id"],
        account_id=row["account_id"],
        status=AppealStatus(row["status"]),
        reason=row["reason"],
        created_by=row["created_by"],
        created_at=_dt(row["created_at"]),
        decided_by=row["decided_by"],
        decided_at=_dt(row["decided_at"]) if row["decided_at"] else None,
        decision_reason=row["decision_reason"],
    )
