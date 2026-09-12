"""因子评估指标每日增量更新（流水线挂钩的手动入口）
==================================================
用法：
  python scripts/factor_eval/update_metric_daily.py            # 全部注册池
  python scripts/factor_eval/update_metric_daily.py --pool zz1000
  python scripts/factor_eval/update_metric_daily.py --weekly   # 强制周全矩阵
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from quantlab.factor.metric_store import (
    active_pools, compute_full_matrix, update_daily, write_corr_weekly)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--pool", help="指定池（缺省全部注册 active 池）")
    p.add_argument("--lookback", type=int, default=130,
                   help="重算窗口交易日数（缺省 130，覆盖 h=120 成熟）")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--weekly", action="store_true",
                   help="强制跑周全矩阵（流水线周五自动）")
    args = p.parse_args()

    pools = [args.pool] if args.pool else active_pools()
    t0 = time.time()
    msg = update_daily(pools=pools, lookback=args.lookback,
                       workers=args.workers)
    print(msg)
    if args.weekly:
        fm = compute_full_matrix()
        if not fm.empty:
            write_corr_weekly(fm)
            print(f"周全矩阵：{len(fm):,} 对已入库")
    print(f"耗时 {(time.time()-t0)/60:.1f} 分钟")


if __name__ == "__main__":
    main()
