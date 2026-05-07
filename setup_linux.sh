#!/usr/bin/env bash
# Market Track — Linux/WSL 一鍵設定腳本
# 執行後完成：venv 建立 + 套件安裝 + crontab（每天 09:00 台北時間）

set -e
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$PROJECT_DIR/.venv"
PYTHON="$VENV/bin/python"
CRON_CMD="$PYTHON $PROJECT_DIR/scripts/daily_pipeline.py >> $PROJECT_DIR/logs/cron.log 2>&1"
# 台北時間 09:00 = UTC 01:00
CRON_LINE="0 1 * * 1-5 $CRON_CMD"

echo ""
echo "[1/3] 建立 venv 並安裝套件..."
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip -q
"$VENV/bin/pip" install -r "$PROJECT_DIR/requirements.txt" -q
echo "      完成"

echo ""
echo "[2/3] 建立 crontab（週一至五 UTC 01:00 / 台北 09:00）..."
# 移除舊的同名項目再新增
(crontab -l 2>/dev/null | grep -v "daily_pipeline.py"; echo "$CRON_LINE") | crontab -
echo "      完成"

echo ""
echo "[3/3] 驗證 crontab..."
crontab -l | grep daily_pipeline.py

echo ""
echo "============================================================"
echo " 全部完成！"
echo " 下一步：手動跑一次確認正常"
echo "   $PYTHON $PROJECT_DIR/scripts/daily_pipeline.py --dry-run"
echo "============================================================"
