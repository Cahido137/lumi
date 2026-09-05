#!/bin/sh
set -e

echo "正在执行数据库迁移 (alembic upgrade head)"
alembic upgrade head

echo "正在启动后端服务"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000