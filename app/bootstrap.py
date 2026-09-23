"""容器启动初始化：建表、播种示例注册表、恢复遗留的未完成作业。

在 uvicorn 启动前由 entrypoint 执行一次，保证：

* SQLite 数据卷是全新卷时，schema 与示例注册表就绪；
* 上次容器异常退出时正在运行（running）的作业项被标记为 failed
  （error_code=interrupted），其余 queued 项由服务启动后的后台工作线程续跑。

幂等：重复执行不会覆盖用户数据，也不会重复恢复。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from . import database as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("app.bootstrap")

SEED_PATH = os.environ.get("AUDIT_SEED_PATH", "/app/data/seed_registry.json")
DB_PATH = os.environ.get("AUDIT_DB_PATH", db.DEFAULT_DB_PATH)


def bootstrap(db_path: str | None = None, seed_path: str | None = None) -> None:
    path = db_path or DB_PATH
    seed_file = Path(seed_path or SEED_PATH)

    conn = db.connect(path)
    try:
        db.init_db(conn)
        if db.package_count(conn) == 0 and seed_file.is_file():
            seed = json.loads(seed_file.read_text(encoding="utf-8"))
            for name, payload in seed.items():
                db.upsert_package(conn, name, payload["versions"])
            logger.info("已播种示例注册表：%d 个包（%s）", len(seed), seed_file)
        recovered = db.recover_interrupted_jobs(conn)
        if recovered:
            logger.warning("启动恢复：%d 个中断的运行中作业项已标记为 failed", recovered)
        logger.info("初始化完成：数据库 %s", path)
    finally:
        conn.close()


if __name__ == "__main__":
    bootstrap()
