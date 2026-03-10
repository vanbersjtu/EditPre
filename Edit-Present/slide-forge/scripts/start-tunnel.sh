#!/usr/bin/env bash
# 用 localtunnel 把本机 8001 暴露为 HTTPS，供 Vercel 前端访问
# 运行后请把输出的 https://xxx.loca.lt 填到 apps/slide-web/config.js 的 apiBase 并重新部署前端
# 此进程需常驻；关闭后隧道地址会变，需重新部署前端
set -e
PORT="${1:-8001}"
echo "Exposing port $PORT. Keep this process running."
exec npx --yes localtunnel --port "$PORT"
