"""异步审计作业的后台执行器。

单进程内的单工作线程：按作业提交顺序、作业内项下标顺序逐项求解并持久化。
所有状态都写入 SQLite（audit_jobs / audit_job_items），因此：

* 线程本身不持有任何必须恢复的内存状态；
* 服务重启时由 :func:`app.database.recover_interrupted_jobs` 把上次遗留的
  running 项标记为 failed，本执行器启动后继续处理剩余 queued 项；
* 单项求解/持久化失败只把该项记为 failed，不影响作业内其他项。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Optional

from . import database as db
from .models import AuditResponse
from .processing import ItemError, ItemOk, process_manifest_raw

logger = logging.getLogger(__name__)


class JobRunner:
    """顺序消费未完成作业的守护工作线程。"""

    def __init__(self, db_path: str, poll_interval: float = 0.2) -> None:
        self._db_path = db_path
        self._poll_interval = poll_interval
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="audit-job-runner", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def notify(self) -> None:
        """新作业入队后唤醒工作线程（立即开始处理，而不是等轮询）。"""
        self._wake.set()

    # ---------- 主循环 ----------

    def _run(self) -> None:
        conn = db.connect(self._db_path)
        try:
            while not self._stop.is_set():
                try:
                    progressed = self._drain_once(conn)
                except Exception:  # noqa: BLE001 - 工作线程不能因单次异常退出
                    logger.exception("作业执行循环发生异常，稍后重试")
                    progressed = False
                    conn.rollback()
                if progressed:
                    continue
                # 没有待处理工作：等待唤醒或超时后再扫一次（兜底，防止丢唤醒）
                self._wake.wait(timeout=self._poll_interval)
                self._wake.clear()
        finally:
            conn.close()

    def _drain_once(self, conn: sqlite3.Connection) -> bool:
        """扫描全部未终结作业并尽可能推进；返回本次是否处理了至少一个项。"""
        progressed = False
        for job in db.list_queued_jobs(conn):
            # 每个作业开始时加载一次注册表快照（作业之间可能有新注册表写入）
            registry = db.load_registry(conn)
            while True:
                row = db.claim_next_item(conn, job["id"])
                if row is None:
                    break
                progressed = True
                self._process_item(conn, job["id"], row, registry)
                if self._stop.is_set():
                    return progressed
        return progressed

    # ---------- 单项处理 ----------

    def _process_item(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        item_row: sqlite3.Row,
        registry: dict,
    ) -> None:
        import json

        item_index = item_row["item_index"]
        try:
            raw = json.loads(item_row["manifest_json"])
        except Exception:  # noqa: BLE001 - 理论上不会发生（写入时序列化）
            logger.exception("作业 %s 第 %d 项清单无法反序列化", job_id, item_index)
            db.complete_item_failure(
                conn,
                job_id,
                item_index,
                code="invalid_manifest",
                message="清单无法解析",
            )
            return

        outcome = process_manifest_raw(raw, registry)
        if isinstance(outcome, ItemError):
            db.complete_item_failure(
                conn,
                job_id,
                item_index,
                code=outcome.code,
                message=outcome.message,
                details=outcome.details,
            )
            return

        assert isinstance(outcome, ItemOk)
        try:
            db.complete_item_success(
                conn,
                job_id=job_id,
                item_index=item_index,
                audit_id=outcome.audit_id,
                created_at=outcome.created_at,
                audit_status=outcome.result.status,
                root_plugin=outcome.root_label,
                root_version=outcome.root_version,
                dependency_count=len(outcome.manifest.dependencies),
                manifest=outcome.manifest.model_dump(mode="json"),
                result=outcome.body,
                node_count=outcome.result.node_count,
            )
        except Exception:  # noqa: BLE001 - 单项落库失败不得阻断后续项
            logger.exception("作业 %s 第 %d 项持久化失败", job_id, item_index)
            db.complete_item_failure(
                conn,
                job_id,
                item_index,
                code="internal_error",
                message="审计结果持久化失败",
            )

    # ---------- 视图组装（供 HTTP 层复用，不依赖运行状态） ----------

    @staticmethod
    def build_item_out(row: sqlite3.Row) -> dict:
        import json

        status = row["status"]
        audit = None
        if status == db.ITEM_SUCCEEDED and row["result_json"]:
            audit = AuditResponse(**json.loads(row["result_json"]))
        error = None
        if status in (db.ITEM_FAILED, db.ITEM_CANCELLED) and row["error_code"]:
            error = {
                "code": row["error_code"],
                "message": row["error_message"],
                "details": json.loads(row["error_details_json"])
                if row["error_details_json"]
                else None,
            }
        return {
            "index": row["item_index"],
            "status": status,
            "audit_id": row["audit_id"],
            "audit": audit,
            "error": error,
            "manifest": json.loads(row["manifest_json"]),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }
