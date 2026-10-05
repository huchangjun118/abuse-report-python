"""SQLite 持久化层。

设计要点：
- 所有写路径都在 ``transact()`` 上下文中执行，单事务提交，
  进程中断时要么整体落盘要么整体回滚，不会产生字段级混合。
- 一把进程内 RLock 串行化写事务，配合乐观版本号避免并发回退。
- audit_log 与业务写入同事务，保证"决定必有痕、痕与状态一致"。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

from .errors import Conflict
from .models import (
    Appeal,
    AppealStatus,
    AuditEvent,
    Case,
    CaseStatus,
    Report,
    ReportStatus,
    Sanction,
    ActionType,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    reporter_id TEXT NOT NULL,
    target_account TEXT NOT NULL,
    event_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence TEXT NOT NULL,
    status TEXT NOT NULL,
    case_id TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    target_account TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    report_count INTEGER NOT NULL,
    unique_reporters INTEGER NOT NULL,
    evidence TEXT NOT NULL,
    reporter_counts TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sanctions (
    sanction_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    target_account TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_refs TEXT NOT NULL,
    operator TEXT NOT NULL,
    effective_at REAL NOT NULL,
    expires_at REAL,
    active INTEGER NOT NULL,
    lifted_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_sanctions_target ON sanctions(target_account, active);
CREATE TABLE IF NOT EXISTS appeals (
    appeal_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    appellant_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    decided_by TEXT,
    decision_reason TEXT,
    created_at REAL NOT NULL,
    decided_at REAL
);
CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_buckets (
    reporter_id TEXT NOT NULL,
    bucket_start REAL NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (reporter_id, bucket_start)
);
"""


def _loads(raw: str) -> list:
    return json.loads(raw) if raw else []


class Store:
    """线程安全的 SQLite 存储。"""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transact(self) -> Iterator[sqlite3.Connection]:
        """串行化的写事务：进入时加锁，退出时提交或回滚。"""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ---------------- 行映射 ----------------

    @staticmethod
    def _row_to_report(row: sqlite3.Row) -> Report:
        return Report(
            report_id=row["report_id"],
            idempotency_key=row["idempotency_key"],
            reporter_id=row["reporter_id"],
            target_account=row["target_account"],
            event_id=row["event_id"],
            reason=row["reason"],
            evidence=_loads(row["evidence"]),
            status=ReportStatus(row["status"]),
            case_id=row["case_id"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_case(row: sqlite3.Row) -> Case:
        case = Case(
            case_id=row["case_id"],
            target_account=row["target_account"],
            status=CaseStatus(row["status"]),
            report_count=row["report_count"],
            unique_reporters=row["unique_reporters"],
            evidence=_loads(row["evidence"]),
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        case.reporter_counts = {str(k): int(v) for k, v in json.loads(row["reporter_counts"] or "{}").items()}
        return case

    @staticmethod
    def _row_to_sanction(row: sqlite3.Row) -> Sanction:
        return Sanction(
            sanction_id=row["sanction_id"],
            case_id=row["case_id"],
            target_account=row["target_account"],
            action=ActionType(row["action"]),
            reason=row["reason"],
            evidence_refs=_loads(row["evidence_refs"]),
            operator=row["operator"],
            effective_at=row["effective_at"],
            expires_at=row["expires_at"],
            active=bool(row["active"]),
            lifted_reason=row["lifted_reason"],
        )

    @staticmethod
    def _row_to_appeal(row: sqlite3.Row) -> Appeal:
        return Appeal(
            appeal_id=row["appeal_id"],
            case_id=row["case_id"],
            appellant_id=row["appellant_id"],
            reason=row["reason"],
            status=AppealStatus(row["status"]),
            decided_by=row["decided_by"],
            decision_reason=row["decision_reason"],
            created_at=row["created_at"],
            decided_at=row["decided_at"],
        )

    @staticmethod
    def _row_to_audit(row: sqlite3.Row) -> AuditEvent:
        return AuditEvent(
            event_id=row["event_id"],
            ts=row["ts"],
            actor=row["actor"],
            role=row["role"],
            action=row["action"],
            target=row["target"],
            reason=row["reason"],
            detail=row["detail"],
        )

    # ---------------- 举报 ----------------

    def insert_report(self, conn: sqlite3.Connection, report: Report) -> bool:
        """按幂等键插入；已存在则返回 False（不重复计数）。"""
        cur = conn.execute(
            """INSERT OR IGNORE INTO reports
               (report_id, idempotency_key, reporter_id, target_account,
                event_id, reason, evidence, status, case_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                report.report_id,
                report.idempotency_key,
                report.reporter_id,
                report.target_account,
                report.event_id,
                report.reason,
                json.dumps(report.evidence),
                report.status.value,
                report.case_id,
                report.created_at,
            ),
        )
        return cur.rowcount == 1

    def get_report_by_key(self, idempotency_key: str) -> Optional[Report]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reports WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        return self._row_to_report(row) if row else None

    def update_report_status(
        self,
        conn: sqlite3.Connection,
        report_id: str,
        status: ReportStatus,
        case_id: Optional[str],
    ) -> None:
        conn.execute(
            "UPDATE reports SET status=?, case_id=? WHERE report_id=?",
            (status.value, case_id, report_id),
        )

    # ---------------- 案件 ----------------

    def get_case_by_target(
        self, conn: sqlite3.Connection, target: str
    ) -> Optional[Case]:
        row = conn.execute(
            "SELECT * FROM cases WHERE target_account=?", (target,)
        ).fetchone()
        return self._row_to_case(row) if row else None

    def get_case(self, case_id: str) -> Optional[Case]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cases WHERE case_id=?", (case_id,)
            ).fetchone()
        return self._row_to_case(row) if row else None

    def insert_case(self, conn: sqlite3.Connection, case: Case) -> bool:
        cur = conn.execute(
            """INSERT OR IGNORE INTO cases
               (case_id, target_account, status, report_count, unique_reporters,
                evidence, reporter_counts, version, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                case.case_id,
                case.target_account,
                case.status.value,
                case.report_count,
                case.unique_reporters,
                json.dumps(case.evidence),
                json.dumps(case.reporter_counts),
                case.version,
                case.created_at,
                case.updated_at,
            ),
        )
        return cur.rowcount == 1

    def update_case(self, conn: sqlite3.Connection, case: Case) -> None:
        """乐观锁整体更新：版本不匹配则抛 Conflict，杜绝字段级混合。"""
        cur = conn.execute(
            """UPDATE cases SET status=?, report_count=?, unique_reporters=?,
                   evidence=?, reporter_counts=?, version=?, updated_at=?
               WHERE case_id=? AND version=?""",
            (
                case.status.value,
                case.report_count,
                case.unique_reporters,
                json.dumps(case.evidence),
                json.dumps(case.reporter_counts),
                case.version + 1,
                case.updated_at,
                case.case_id,
                case.version,
            ),
        )
        if cur.rowcount != 1:
            raise Conflict(f"case {case.case_id} version conflict")
        case.version += 1

    # ---------------- 处置 ----------------

    def insert_sanction(self, conn: sqlite3.Connection, s: Sanction) -> None:
        conn.execute(
            """INSERT INTO sanctions
               (sanction_id, case_id, target_account, action, reason,
                evidence_refs, operator, effective_at, expires_at, active,
                lifted_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                s.sanction_id,
                s.case_id,
                s.target_account,
                s.action.value,
                s.reason,
                json.dumps(s.evidence_refs),
                s.operator,
                s.effective_at,
                s.expires_at,
                1 if s.active else 0,
                s.lifted_reason,
            ),
        )

    def list_sanctions(
        self, conn: sqlite3.Connection, target: str, active_only: bool = True
    ) -> list[Sanction]:
        sql = "SELECT * FROM sanctions WHERE target_account=?"
        if active_only:
            sql += " AND active=1"
        rows = conn.execute(sql, (target,)).fetchall()
        return [self._row_to_sanction(r) for r in rows]

    def lift_sanction(
        self, conn: sqlite3.Connection, sanction_id: str, reason: str
    ) -> None:
        conn.execute(
            "UPDATE sanctions SET active=0, lifted_reason=? WHERE sanction_id=?",
            (reason, sanction_id),
        )

    def list_expired_active_sanctions(self, now: float) -> list[Sanction]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sanctions WHERE active=1 AND expires_at IS NOT NULL AND expires_at<=?",
                (now,),
            ).fetchall()
        return [self._row_to_sanction(r) for r in rows]

    # ---------------- 申诉 ----------------

    def insert_appeal(self, conn: sqlite3.Connection, appeal: Appeal) -> None:
        conn.execute(
            """INSERT INTO appeals
               (appeal_id, case_id, appellant_id, reason, status,
                decided_by, decision_reason, created_at, decided_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                appeal.appeal_id,
                appeal.case_id,
                appeal.appellant_id,
                appeal.reason,
                appeal.status.value,
                appeal.decided_by,
                appeal.decision_reason,
                appeal.created_at,
                appeal.decided_at,
            ),
        )

    def get_appeal(self, appeal_id: str) -> Optional[Appeal]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM appeals WHERE appeal_id=?", (appeal_id,)
            ).fetchone()
        return self._row_to_appeal(row) if row else None

    def get_pending_appeal(
        self, conn: sqlite3.Connection, case_id: str
    ) -> Optional[Appeal]:
        row = conn.execute(
            "SELECT * FROM appeals WHERE case_id=? AND status=?",
            (case_id, AppealStatus.PENDING.value),
        ).fetchone()
        return self._row_to_appeal(row) if row else None

    def update_appeal(self, conn: sqlite3.Connection, appeal: Appeal) -> None:
        conn.execute(
            """UPDATE appeals SET status=?, decided_by=?, decision_reason=?,
               decided_at=? WHERE appeal_id=?""",
            (
                appeal.status.value,
                appeal.decided_by,
                appeal.decision_reason,
                appeal.decided_at,
                appeal.appeal_id,
            ),
        )

    # ---------------- 审计 ----------------

    def append_audit(self, conn: sqlite3.Connection, event: AuditEvent) -> None:
        conn.execute(
            """INSERT INTO audit_log
               (event_id, ts, actor, role, action, target, reason, detail)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                event.event_id,
                event.ts,
                event.actor,
                event.role,
                event.action,
                event.target,
                event.reason,
                event.detail,
            ),
        )

    def list_audit(self, target: Optional[str] = None) -> list[AuditEvent]:
        with self._lock:
            if target is None:
                rows = self._conn.execute(
                    "SELECT * FROM audit_log ORDER BY seq"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM audit_log WHERE target=? ORDER BY seq",
                    (target,),
                ).fetchall()
        return [self._row_to_audit(r) for r in rows]

    # ---------------- 限速 ----------------

    def hit_rate_bucket(
        self, conn: sqlite3.Connection, reporter_id: str, bucket_start: float
    ) -> int:
        """窗口计数 +1 并返回最新计数（同事务，天然防并发超发）。"""
        conn.execute(
            """INSERT INTO rate_buckets (reporter_id, bucket_start, count)
               VALUES (?,?,1)
               ON CONFLICT(reporter_id, bucket_start)
               DO UPDATE SET count=count+1""",
            (reporter_id, bucket_start),
        )
        row = conn.execute(
            "SELECT count FROM rate_buckets WHERE reporter_id=? AND bucket_start=?",
            (reporter_id, bucket_start),
        ).fetchone()
        return int(row["count"])
