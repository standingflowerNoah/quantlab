#!/usr/bin/env bash
# 按价值倒序串行回填 2022-2024 分钟线（2024 先跑，尽早拿到完整可用年份）。
# 每年一个独立区间，进度互不干扰，可随时中断续跑。
#
# 参数已按实测最优固定：
#  - 服务端硬并发上限 5（超出返回 429 "请不要超过5个线程"）
#  - 速率天花板约 190 req/min（超出返回 429 "您请求速度过快"）
#  - 服务端单次响应延迟与 payload 大小**无关**（289B 和 399KB 都是 7~9s）
#    → 唯一提速杠杆是「每次请求塞满行数」：按 32 个交易日切段（7953 行 < 8000 上限）
#      请求数从 36 次/年 降到 8 次/年
#  - 单请求超时 25s：服务端偶尔长挂，超时太大会让 worker 卡住数分钟
#
# 用法: bash scripts/run_xiaodefa_backfill.sh [workers] [rate] [chunk_tdays] [years]
#   years 默认 "2024 2023"（2022 因服务端延迟飙升、按价值排序最低，暂缓；
#   令牌续期或延迟恢复后补跑即可，断点续跑不会重复拉取）
set -u

cd "$(dirname "$0")/.." || exit 1
PY="C:/Users/53497/.workbuddy/binaries/python/envs/quantlab/Scripts/python.exe"
WORKERS="${1:-5}"
RATE="${2:-185}"
CHUNK_TDAYS="${3:-32}"
YEARS="${4:-2024 2023}"
LOG="logs/xiaodefa_backfill_driver.log"

mkdir -p logs
echo "=== 启动 $(date '+%F %T')  workers=$WORKERS rate=$RATE chunk_tdays=$CHUNK_TDAYS years=[$YEARS] ===" | tee -a "$LOG"

for Y in $YEARS; do
    echo "--- 开始 $Y $(date '+%F %T') ---" | tee -a "$LOG"
    # 逐年重试：2026-09-12 00:09 曾出现子进程静默崩溃（rc=1、无异常栈、无"完成"行），
    # 旧版调度会直接跳到下一年 → 2024 只跑完 2520/5329 就"结束"了。
    # 现在：① -X faulthandler 捕获段错误栈 ② 非零退出自动重试（进度文件保证续跑不重拉）
    for ATTEMPT in 1 2 3 4 5 6; do
        echo "  [尝试 $ATTEMPT] $(date '+%F %T')" | tee -a "$LOG"
        "$PY" -X faulthandler scripts/backfill_minute_xiaodefa.py \
            --start "${Y}-01" --end "${Y}-12" \
            --workers "$WORKERS" --rate "$RATE" --chunk-tdays "$CHUNK_TDAYS" 2>&1 | tee -a "$LOG"
        RC=${PIPESTATUS[0]}
        if [ "$RC" -eq 0 ]; then
            echo "--- 结束 $Y rc=0 $(date '+%F %T') ---" | tee -a "$LOG"
            break
        fi
        if [ "$RC" -eq 2 ]; then
            echo "🛑 $Y 因【token 过期/无效】中止（rc=2），不再重试。请更换 token 后续跑。" | tee -a "$LOG"
            break
        fi
        echo "--- $Y 第 $ATTEMPT 次异常退出 rc=$RC，5s 后续跑 $(date '+%F %T') ---" | tee -a "$LOG"
        sleep 5
    done
    # 收尾核对：落盘股票数 vs kline_daily 该年宇宙，不足则明确告警（不静默跳过）
    "$PY" - "$Y" <<'PYEOF' 2>&1 | tee -a "$LOG"
import sys, duckdb
y = sys.argv[1]
con = duckdb.connect()
got = con.execute(f"SELECT count(DISTINCT code) FROM read_parquet('data/lake/clean/kline_1min/year={y}/*.parquet')").fetchone()[0]
want = con.execute(f"""SELECT count(DISTINCT code) FROM read_parquet('data/lake/clean/mirror/kline_daily.parquet')
                       WHERE date >= '{y}-01-01' AND date <= '{y}-12-31'""").fetchone()[0]
n = con.execute(f"SELECT count(*) FROM read_parquet('data/lake/clean/kline_1min/year={y}/*.parquet')").fetchone()[0]
flag = "✅ 完整" if got >= want - 5 else "⚠️ 不完整"
print(f"[核对] {y}: 分钟 {got} 只 / 日线宇宙 {want} 只 | {n:,} 行 | {flag}")
PYEOF
done

echo "=== 全部完成 $(date '+%F %T') ===" | tee -a "$LOG"
