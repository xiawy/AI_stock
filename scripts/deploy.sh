#!/usr/bin/env bash
# 一键部署（在生产机以 root 执行）。
#
#   /deploy/AI_stock/scripts/deploy.sh              # 全量：拉代码 + 构建前端 + 重启
#   /deploy/AI_stock/scripts/deploy.sh --no-frontend
#   /deploy/AI_stock/scripts/deploy.sh --no-restart
#
# 为什么要写成脚本：前端 dist 一直是**手工**构建的，2026-10 那次改动就漏了
# 构建，代码上了线但页面还是 9 月的旧产物。另外 `sudo -u www-data npm run build`
# 会因 node_modules/.vite-temp 的属主是 root 而失败，必须以 root 构建，
# 构建完再把 dist 交给 www-data 读 —— 这些坑不写进脚本就会重复踩。
set -euo pipefail

REPO="${REPO:-/deploy/AI_stock}"
SERVICE="${SERVICE:-ai-stock-backend}"
DO_FRONTEND=1
DO_RESTART=1

for arg in "$@"; do
  case "$arg" in
    --no-frontend) DO_FRONTEND=0 ;;
    --no-restart)  DO_RESTART=0 ;;
    -h|--help)     sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 执行（需要构建前端并重启 systemd 服务）" >&2
  exit 1
fi

step "1/5 拉取代码"
BEFORE=$(git -C "$REPO" -c safe.directory="$REPO" rev-parse --short HEAD)
git -C "$REPO" -c safe.directory="$REPO" pull --ff-only
AFTER=$(git -C "$REPO" -c safe.directory="$REPO" rev-parse --short HEAD)
echo "$BEFORE -> $AFTER"
if [ "$BEFORE" = "$AFTER" ]; then
  echo "（代码无变化）"
fi

step "2/5 后端依赖"
VENV="$REPO/backend/.venv"
if [ -x "$VENV/bin/pip" ] && [ "$BEFORE" != "$AFTER" ]; then
  # 依赖没变就是空跑，几秒钟的事；做一次比漏装强
  "$VENV/bin/pip" install -q -r "$REPO/backend/requirements.txt" 2>&1 | tail -3 || true
else
  echo "跳过（venv 不存在或代码无变化）"
fi

step "3/5 数据库结构"
# init_db 会在服务启动时跑；这里先跑一次，让补列/建表在重启前完成，
# 避免第一个请求打到还没迁移的表上。
cd "$REPO/backend"
sudo -u www-data env PROJECT_ROOT="$REPO" PYTHONPATH="$REPO:$REPO/backend" \
  "$VENV/bin/python" - <<'PY'
from app.core.database import init_db
init_db()
from ai_stock.pipeline.db_models import ensure_news_impact_schema, ensure_support_tables
ensure_news_impact_schema()
ensure_support_tables()
print("schema ready")
PY

step "4/5 前端构建"
if [ "$DO_FRONTEND" -eq 1 ]; then
  cd "$REPO/frontend"
  if [ -d dist ]; then
    BAK="dist.bak.$(date +%Y%m%d%H%M%S)"
    mv dist "$BAK"
    echo "旧产物备份 -> $BAK"
    # 只留最近 3 份，避免磁盘慢慢被撑满
    ls -1dt dist.bak.* 2>/dev/null | tail -n +4 | xargs -r rm -rf
  fi
  if [ ! -d node_modules ]; then
    echo "node_modules 缺失，安装依赖（可能需要几分钟）..."
    npm ci --no-audit --no-fund
  fi
  if ! npm run build 2>&1 | tail -6; then
    echo "前端构建失败，回滚 dist" >&2
    [ -d dist ] && rm -rf dist
    [ -n "${BAK:-}" ] && [ -d "$BAK" ] && mv "$BAK" dist
    exit 1
  fi
  chown -R www-data:www-data dist
  chmod -R a+rX dist
  echo "dist 已更新并交给 www-data"
else
  echo "跳过（--no-frontend）"
fi

step "5/5 重启并检查"
if [ "$DO_RESTART" -eq 1 ]; then
  systemctl restart "$SERVICE"
  for i in $(seq 1 20); do
    sleep 1
    if systemctl is-active --quiet "$SERVICE"; then break; fi
  done
  if ! systemctl is-active --quiet "$SERVICE"; then
    echo "服务未能启动，最近日志：" >&2
    journalctl -u "$SERVICE" -n 40 --no-pager >&2
    exit 1
  fi
  sleep 3
  CODE=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/health || true)
  echo "服务 $(systemctl is-active "$SERVICE")，健康检查 HTTP $CODE"
  journalctl -u "$SERVICE" -n 8 --no-pager | tail -8
else
  echo "跳过（--no-restart）"
fi

printf '\n\033[1;32m部署完成\033[0m %s\n' "$AFTER"
