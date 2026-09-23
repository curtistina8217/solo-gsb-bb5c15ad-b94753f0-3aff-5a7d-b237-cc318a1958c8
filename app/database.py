"""SQLite 持久化层：审计记录与包注册表。"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

DEFAULT_DB_PATH = os.environ.get("AUDIT_DB_PATH", os.path.join(os.getcwd(), "data", "audits.db"))

# 作业项状态：queued -> running -> succeeded/failed；queued 可经 DELETE 取消为 cancelled
ITEM_QUEUED = "queued"
ITEM_RUNNING = "running"
ITEM_SUCCEEDED = "succeeded"
ITEM_FAILED = "failed"
ITEM_CANCELLED = "cancelled"

# 作业状态：queued（尚未开始）/ running（处理中）/ succeeded / failed
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_SUCCEEDED = "succeeded"
JOB_FAILED = "failed"

_TERMINAL_ITEM_STATES = (ITEM_SUCCEEDED, ITEM_FAILED, ITEM_CANCELLED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audits (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    root_plugin TEXT,
    root_version TEXT,
    dependency_count INTEGER NOT NULL,
    manifest_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    node_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS packages (
    name TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS package_versions (
    package TEXT NOT NULL,
    version TEXT NOT NULL,
    dependencies_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (package, version),
    FOREIGN KEY (package) REFERENCES packages(name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_jobs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    total INTEGER NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    succeeded INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    cancelled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_job_items (
    job_id TEXT NOT NULL,
    item_index INTEGER NOT NULL,
    status TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    audit_id TEXT,
    result_json TEXT,
    error_code TEXT,
    error_message TEXT,
    error_details_json TEXT,
    started_at TEXT,
    finished_at TEXT,
    PRIMARY KEY (job_id, item_index),
    FOREIGN KEY (job_id) REFERENCES audit_jobs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_audits_created_at ON audits(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_jobs_status ON audit_jobs(status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_audit_job_items_status
    ON audit_job_items(job_id, status, item_index);
"""


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------- 注册表 ----------

def upsert_package(
    conn: sqlite3.Connection,
    name: str,
    versions: List[Dict[str, Any]],
) -> None:
    """整包写入（版本按主键合并，重复版本覆盖依赖）。"""
    with transaction(conn):
        conn.execute("INSERT OR IGNORE INTO packages(name) VALUES (?)", (name,))
        for item in versions:
            conn.execute(
                """
                INSERT INTO package_versions(package, version, dependencies_json)
                VALUES (?, ?, ?)
                ON CONFLICT(package, version) DO UPDATE SET
                    dependencies_json = excluded.dependencies_json
                """,
                (name, item["version"], json.dumps(item.get("dependencies", {}), ensure_ascii=False)),
            )


def load_registry(conn: sqlite3.Connection) -> Dict[str, Dict[str, Dict[str, str]]]:
    """返回 {包名: {版本: {依赖名: 范围}}}。"""
    registry: Dict[str, Dict[str, Dict[str, str]]] = {}
    rows = conn.execute(
        "SELECT package, version, dependencies_json FROM package_versions ORDER BY package, version"
    ).fetchall()
    for row in rows:
        registry.setdefault(row["package"], {})[row["version"]] = json.loads(row["dependencies_json"])
    return registry


def package_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM packages").fetchone()[0])


# ---------- 审计记录 ----------

def insert_audit(
    conn: sqlite3.Connection,
    audit_id: str,
    created_at: str,
    status: str,
    root_plugin: Optional[str],
    root_version: Optional[str],
    dependency_count: int,
    manifest: Dict[str, Any],
    result: Dict[str, Any],
    node_count: int,
) -> None:
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO audits(
                id, created_at, status, root_plugin, root_version,
                dependency_count, manifest_json, result_json, node_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                created_at,
                status,
                root_plugin,
                root_version,
                dependency_count,
                json.dumps(manifest, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                node_count,
            ),
        )


def get_audit(conn: sqlite3.Connection, audit_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM audits WHERE id = ?", (audit_id,)).fetchone()


def list_audits(
    conn: sqlite3.Connection,
    status: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> List[sqlite3.Row]:
    if status:
        return conn.execute(
            """
            SELECT id, created_at, status, root_plugin, dependency_count, node_count,
                   json_array_length(json_extract(result_json, '$.conflicts')) AS conflict_count
            FROM audits WHERE status = ?
            ORDER BY created_at DESC, id
            LIMIT ? OFFSET ?
            """,
            (status, limit, offset),
        ).fetchall()
    return conn.execute(
        """
        SELECT id, created_at, status, root_plugin, dependency_count, node_count,
               json_array_length(json_extract(result_json, '$.conflicts')) AS conflict_count
        FROM audits
        ORDER BY created_at DESC, id
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()


def count_audits(conn: sqlite3.Connection, status: Optional[str] = None) -> int:
    if status:
        return int(
            conn.execute("SELECT COUNT(*) FROM audits WHERE status = ?", (status,)).fetchone()[0]
        )
    return int(conn.execute("SELECT COUNT(*) FROM audits").fetchone()[0])


def iter_audits(conn: sqlite3.Connection, status: Optional[str] = None) -> Iterator[sqlite3.Row]:
    """遍历全部审计记录（按创建时间倒序），供全量扫描类查询使用。"""
    if status:
        yield from conn.execute(
            "SELECT * FROM audits WHERE status = ? ORDER BY created_at DESC, id",
            (status,),
        )
    else:
        yield from conn.execute("SELECT * FROM audits ORDER BY created_at DESC, id")


# ---------- 异步审计作业 ----------


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def create_job(
    conn: sqlite3.Connection, job_id: str, manifests: List[Dict[str, Any]]
) -> None:
    """在单个事务内创建作业及其全部 queued 项。"""
    now = _now()
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO audit_jobs(id, created_at, updated_at, status, total)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, now, now, JOB_QUEUED, len(manifests)),
        )
        conn.executemany(
            """
            INSERT INTO audit_job_items(job_id, item_index, status, manifest_json)
            VALUES (?, ?, ?, ?)
            """,
            [
                (job_id, index, ITEM_QUEUED, json.dumps(raw, ensure_ascii=False))
                for index, raw in enumerate(manifests)
            ],
        )


def get_job(conn: sqlite3.Connection, job_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM audit_jobs WHERE id = ?", (job_id,)).fetchone()


def list_job_items(
    conn: sqlite3.Connection, job_id: str
) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM audit_job_items WHERE job_id = ? ORDER BY item_index",
        (job_id,),
    ).fetchall()


def list_queued_jobs(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """按提交顺序返回仍未终结（queued/running）的作业，供工作线程领取。"""
    return conn.execute(
        """
        SELECT * FROM audit_jobs
        WHERE status IN (?, ?)
        ORDER BY created_at, id
        """,
        (JOB_QUEUED, JOB_RUNNING),
    ).fetchall()


def claim_next_item(
    conn: sqlite3.Connection, job_id: str
) -> Optional[sqlite3.Row]:
    """原子领取作业中下一个 queued 项并置为 running；没有则返回 None。"""
    with transaction(conn):
        # IMMEDIATE 事务串行化领取动作，避免重启并发/多线程下重复领取
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT item_index FROM audit_job_items
            WHERE job_id = ? AND status = ?
            ORDER BY item_index
            LIMIT 1
            """,
            (job_id, ITEM_QUEUED),
        ).fetchone()
        if row is None:
            return None
        item_index = row["item_index"]
        conn.execute(
            "UPDATE audit_job_items SET status = ?, started_at = ? WHERE job_id = ? AND item_index = ?",
            (ITEM_RUNNING, _now(), job_id, item_index),
        )
        conn.execute(
            "UPDATE audit_jobs SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            (JOB_RUNNING, _now(), job_id, JOB_QUEUED),
        )
        return conn.execute(
            "SELECT * FROM audit_job_items WHERE job_id = ? AND item_index = ?",
            (job_id, item_index),
        ).fetchone()


def _finish_job_status(remaining_queued: int, running: int, failed: int) -> str:
    """根据各项计数推导作业状态：仍有未完成项 -> running；有失败 -> failed；否则 succeeded。"""
    if remaining_queued > 0 or running > 0:
        return JOB_RUNNING
    if failed > 0:
        return JOB_FAILED
    return JOB_SUCCEEDED


def complete_item_success(
    conn: sqlite3.Connection,
    job_id: str,
    item_index: int,
    audit_id: str,
    created_at: str,
    audit_status: str,
    root_plugin: Optional[str],
    root_version: Optional[str],
    dependency_count: int,
    manifest: Dict[str, Any],
    result: Dict[str, Any],
    node_count: int,
) -> None:
    """单项成功：审计记录与作业项结果在同一事务内落库，保证原子性。"""
    finished_at = _now()
    with transaction(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO audits(
                id, created_at, status, root_plugin, root_version,
                dependency_count, manifest_json, result_json, node_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                created_at,
                audit_status,
                root_plugin,
                root_version,
                dependency_count,
                json.dumps(manifest, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                node_count,
            ),
        )
        conn.execute(
            """
            UPDATE audit_job_items
            SET status = ?, audit_id = ?, result_json = ?,
                error_code = NULL, error_message = NULL, error_details_json = NULL,
                finished_at = ?
            WHERE job_id = ? AND item_index = ?
            """,
            (ITEM_SUCCEEDED, audit_id, json.dumps(result, ensure_ascii=False),
             finished_at, job_id, item_index),
        )
        _bump_job_counts(conn, job_id, succeeded_delta=1)
    # 上下文管理器在 BEGIN IMMEDIATE 后会再次 COMMIT，空提交无副作用


def complete_item_failure(
    conn: sqlite3.Connection,
    job_id: str,
    item_index: int,
    code: str,
    message: str,
    details: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """单项失败：仅记录结构化错误，不产生 audits 记录；不阻断其他项。"""
    finished_at = _now()
    with transaction(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE audit_job_items
            SET status = ?, audit_id = NULL, result_json = NULL,
                error_code = ?, error_message = ?, error_details_json = ?,
                finished_at = ?
            WHERE job_id = ? AND item_index = ?
            """,
            (
                ITEM_FAILED,
                code,
                message,
                json.dumps(details, ensure_ascii=False) if details is not None else None,
                finished_at,
                job_id,
                item_index,
            ),
        )
        _bump_job_counts(conn, job_id, failed_delta=1)


def _bump_job_counts(
    conn: sqlite3.Connection,
    job_id: str,
    succeeded_delta: int = 0,
    failed_delta: int = 0,
    cancelled_delta: int = 0,
) -> None:
    """更新作业聚合计数；若全部项已终结则同步落定作业状态。须在调用方事务内执行。"""
    row = conn.execute(
        "SELECT total, completed, succeeded, failed, cancelled FROM audit_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    completed = row["completed"] + succeeded_delta + failed_delta + cancelled_delta
    succeeded = row["succeeded"] + succeeded_delta
    failed = row["failed"] + failed_delta
    cancelled = row["cancelled"] + cancelled_delta
    remaining = row["total"] - completed
    job_status = _finish_job_status(
        remaining_queued=max(remaining, 0),
        running=0 if completed >= row["total"] else 1,
        failed=failed,
    )
    conn.execute(
        """
        UPDATE audit_jobs
        SET updated_at = ?, completed = ?, succeeded = ?, failed = ?,
            cancelled = ?, status = ?
        WHERE id = ?
        """,
        (_now(), completed, succeeded, failed, cancelled, job_status, job_id),
    )


def cancel_item(conn: sqlite3.Connection, job_id: str, item_index: int) -> str:
    """取消单个尚未开始的项。

    返回值：``ok`` 成功；``not_found`` 作业不存在；``item_not_found`` 项不存在；
    ``not_cancellable`` 该项已离开 queued 状态。
    """
    with transaction(conn):
        job = conn.execute("SELECT id FROM audit_jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            return "not_found"
        conn.execute("BEGIN IMMEDIATE")
        item = conn.execute(
            "SELECT status FROM audit_job_items WHERE job_id = ? AND item_index = ?",
            (job_id, item_index),
        ).fetchone()
        if item is None:
            return "item_not_found"
        if item["status"] != ITEM_QUEUED:
            return "not_cancellable"
        conn.execute(
            "UPDATE audit_job_items SET status = ?, finished_at = ? WHERE job_id = ? AND item_index = ?",
            (ITEM_CANCELLED, _now(), job_id, item_index),
        )
        _bump_job_counts(conn, job_id, cancelled_delta=1)
        return "ok"


def cancel_queued_items(conn: sqlite3.Connection, job_id: str) -> Optional[int]:
    """取消作业内全部尚未开始的项，返回取消数量；作业不存在返回 None。"""
    with transaction(conn):
        job = conn.execute("SELECT id FROM audit_jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            return None
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE audit_job_items SET status = ?, finished_at = ? WHERE job_id = ? AND status = ?",
            (ITEM_CANCELLED, _now(), job_id, ITEM_QUEUED),
        )
        cancelled_count = cur.rowcount
        if cancelled_count:
            _bump_job_counts(conn, job_id, cancelled_delta=cancelled_count)
        return cancelled_count


def recover_interrupted_jobs(conn: sqlite3.Connection) -> int:
    """服务重启恢复：把上次进程遗留的 running 项标记为 failed。

    作业状态与聚合计数以 items 表为准整体重算（可能重启时作业正处在
    running 而尚无任何项落定的中间态）。返回被恢复的项数。
    """
    recovered = 0
    now = _now()
    with transaction(conn):
        job_rows = conn.execute(
            "SELECT id FROM audit_jobs WHERE status IN (?, ?)",
            (JOB_QUEUED, JOB_RUNNING),
        ).fetchall()
        for job in job_rows:
            job_id = job["id"]
            cur = conn.execute(
                """
                UPDATE audit_job_items
                SET status = ?, error_code = ?, error_message = ?,
                    finished_at = COALESCE(finished_at, ?)
                WHERE job_id = ? AND status = ?
                """,
                (
                    ITEM_FAILED,
                    "interrupted",
                    "服务重启中断，该项未完成",
                    now,
                    job_id,
                    ITEM_RUNNING,
                ),
            )
            recovered += cur.rowcount

            counts = conn.execute(
                """
                SELECT
                    SUM(status = ?) AS queued,
                    SUM(status = ?) AS succeeded,
                    SUM(status = ?) AS failed,
                    SUM(status = ?) AS cancelled,
                    COUNT(*) AS total
                FROM audit_job_items WHERE job_id = ?
                """,
                (ITEM_QUEUED, ITEM_SUCCEEDED, ITEM_FAILED, ITEM_CANCELLED, job_id),
            ).fetchone()
            queued_n = counts["queued"] or 0
            succeeded_n = counts["succeeded"] or 0
            failed_n = counts["failed"] or 0
            cancelled_n = counts["cancelled"] or 0
            completed_n = succeeded_n + failed_n + cancelled_n
            job_status = _finish_job_status(
                remaining_queued=queued_n, running=0, failed=failed_n
            )
            conn.execute(
                """
                UPDATE audit_jobs
                SET updated_at = ?, completed = ?, succeeded = ?, failed = ?,
                    cancelled = ?, status = ?
                WHERE id = ?
                """,
                (now, completed_n, succeeded_n, failed_n, cancelled_n, job_status, job_id),
            )
    return recovered

