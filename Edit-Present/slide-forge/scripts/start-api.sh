#!/usr/bin/env bash
# 在 slide-forge 仓库根目录运行后端 API（端口 8001）
set -e
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -n "$VIRTUAL_ENV" ]]; then
  : # 已激活虚拟环境
elif [[ -d "$REPO_ROOT/.venv" ]]; then
  source "$REPO_ROOT/.venv/bin/activate"
else
  echo "Warning: no .venv found. Using system python."
fi

exec uvicorn main:app --app-dir apps/slide-api --host 0.0.0.0 --port "${SLIDE_API_PORT:-8001}"
