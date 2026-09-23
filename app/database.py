"""SQLite 持久化层：审计记录与包注册表。"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

DEFAULT_DB_PATH = os.environ.get("AUDIT_DB_PATH", os.path.join(os.getcwd(), "data", "audits.db"))

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

CREATE INDEX IF NOT EXISTS idx_audits_created_at ON audits(created_at DESC);
"""


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
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
