#!/usr/bin/env bash
# 一键启动（零依赖，仅需 Python 3.9+，数据存 SQLite）
set -e
cd "$(dirname "$0")/.."
exec python3 run.py "${1:-8050}"
