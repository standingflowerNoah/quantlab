#!/usr/bin/env bash
# 按价值倒序串行回填 2022-2024 分钟线（2024 先跑，尽早拿到完整可用年份）。
# 每年一个独立区间，进度互不干扰，可随时中断续跑。
#
# 参数已按实测最优固定：服务端硬并发上限 5（超出返回 429 "请不要超过5个线程"），
# 速率天花板约 190 req/min（超出返回 429 "您请求速度过快"）。
# 5 并发直连实测 192 req/min / 14,638 行/秒 / 零错误。
#
# 用法: bash scripts/run_xiaodefa_backfill.sh
set -u

cd "$(dirname "$0")/.." || exit 1
PY="C:/Users/53497/.workbuddy/binaries/python/envs/quantlab/Scripts/python.exe"
WORKERS="${1:-5}"
RATE="${2:-185}"
LOG="logs/xiaodefa_backfill_driver.log"

mkdir -p logs
echo "=== 启动 $(date '+%F %T')  workers=$WORKERS rate=$RATE ===" | tee -a "$LOG"

for Y in 2024 2023 2022; do
    echo "--- 开始 $Y $(date '+%F %T') ---" | tee -a "$LOG"
    "$PY" scripts/backfill_minute_xiaodefa.py \
        --start "${Y}-01" --end "${Y}-12" \
        --workers "$WORKERS" --rate "$RATE" 2>&1 | tee -a "$LOG"
    RC=${PIPESTATUS[0]}
    echo "--- 结束 $Y rc=$RC $(date '+%F %T') ---" | tee -a "$LOG"
done

echo "=== 全部完成 $(date '+%F %T') ===" | tee -a "$LOG"
