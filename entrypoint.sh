#!/bin/sh
# 容器入口：先做一次性初始化（建表 / 播种 / 恢复未完成作业），再启动服务。
set -e

echo "[entrypoint] 初始化数据库与作业恢复..."
python -m app.bootstrap

echo "[entrypoint] 启动 uvicorn..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
