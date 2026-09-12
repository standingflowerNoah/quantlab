"""universe_daily 逐日池快照：前向记录 + 历史重建（一次性/幂等）

背景：universe.py 建池用的是「当前状态」（instruments.is_st/list_date/board +
daily_snapshot 最新市值），历史回测因此带幸存者偏差。本脚本：
  ① record()      记录当日各池快照（严格 PIT，此后每日流水线自动执行）
  ② reconstruct() 用 valuation_daily.is_st（逐日真值，已验证 489 只状态随
                   时间变化）+ instruments 静态事实，PIT 重建 2021 起的
                   ashare_ex 历史池

用法：
  python scripts/backfill_universe_daily.py            # 记当日 + 重建历史
  python scripts/backfill_universe_daily.py --recon-only
  python scripts/backfill_universe_daily.py --record-only
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def wait_lock(max_tries: int = 240) -> None:
    import duckdb
    from quantlab import config
    db = str(config.DUCKDB_PATH)
    import time
    for i in range(max_tries):
        try:
            c = duckdb.connect(db)
            c.execute("SELECT 1").fetchone()
            c.close()
            print(f"[lock] 拿到写锁（第 {i + 1} 次尝试）", flush=True)
            return
        except Exception:
            time.sleep(15)
    raise SystemExit("等待写锁超时")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recon-only", action="store_true")
    ap.add_argument("--record-only", action="store_true")
    ap.add_argument("--start", default="2021-01-04")
    args = ap.parse_args()

    wait_lock()
    from quantlab.data.universe_daily import (
        coverage, record, reconstruct_ashare_ex)

    if not args.recon_only:
        n = record()
        print(f"① 当日快照已记录：{n} 行")
    if not args.record_only:
        n = reconstruct_ashare_ex(start=args.start)
        print(f"② ashare_ex 历史重建：{n} 行（{args.start} 起）")
    print("\n覆盖概况：")
    print(coverage().to_string(index=False))


if __name__ == "__main__":
    main()
