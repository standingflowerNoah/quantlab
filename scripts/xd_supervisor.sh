#!/usr/bin/env bash
# 回填监督器：驱动器意外退出（静默崩溃/被回收）时自动重启。
# 进度文件保证续跑不重拉；全部完成后 rc=0 自动退出。
#
# 用法: bash scripts/xd_supervisor.sh [workers] [rate] [chunk_tdays] [years]
#   MAX_RESTARTS 上限 60 次，防止无限循环。
set -u
cd "$(dirname "$0")/.." || exit 1

MAX_RESTARTS="${XD_MAX_RESTARTS:-60}"
LOG="logs/xd_supervisor.log"

echo "=== 监督器启动 $(date '+%F %T')  args=$* ===" | tee -a "$LOG"
i=0
while [ "$i" -lt "$MAX_RESTARTS" ]; do
    i=$((i + 1))
    echo "--- 监督器第 $i 轮 $(date '+%F %T') ---" | tee -a "$LOG"
    bash scripts/run_xiaodefa_backfill.sh "$@"
    RC=$?
    if [ "$RC" -eq 0 ]; then
        echo "=== 监督器：全部完成，退出 $(date '+%F %T') ===" | tee -a "$LOG"
        exit 0
    fi
    echo "⚠️ 监督器：驱动退出 rc=$RC，30s 后自动重启 $(date '+%F %T')" | tee -a "$LOG"
    sleep 30
done
echo "🛑 监督器：重启次数达上限 $MAX_RESTARTS，人工介入 $(date '+%F %T')" | tee -a "$LOG"
exit 1
