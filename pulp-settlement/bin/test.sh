#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."

echo "=== Python 服务层 + HTTP 端到端（含并发付款、重启恢复）==="
python3 -m unittest discover -s tests -v

echo
echo "=== 浏览器回归（jsdom 真实执行页面脚本：登录/录入/计价/复核/付款/冲正/重启）==="
if [ -d node_modules/jsdom ]; then
  node tests/browser_regression.mjs
else
  echo "未安装 jsdom，跳过浏览器回归。执行以下命令后可运行："
  echo "  npm install"
  exit 1
fi
