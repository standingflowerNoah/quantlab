"""P2：基本面首批 12 因子全历史计算入库（data/lake/factor/<name>/part-<year>.parquet）

首次写入触发 maybe_audit deep 全审（PIT 穿越自检 + IC），审计结果落
data/lake/factor_audit/<name>.json。

用法：
  python scripts/fundamental_factor_compute.py            # 全部 12 只
  python scripts/fundamental_factor_compute.py ep bp      # 指定因子
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quantlab.factor.compute import compute_factor  # noqa: E402

ALL = [
    # 估值
    "ep", "bp", "sp_ttm", "cfp_ttm",
    # 质量
    "roe_ttm", "roe_cut", "roic", "gpoa", "accruals2", "ocf_to_profit",
    # 成长
    "np_q_yoy", "rev_q_yoy",
]


def main():
    names = sys.argv[1:] or ALL
    for n in names:
        t0 = time.time()
        try:
            df = compute_factor(n, save=True)
            if df.empty:
                print(f"[{n}] EMPTY ⚠️", flush=True)
                continue
            print(f"[{n}] rows={len(df):,} "
                  f"{df['date'].min().date()}~{df['date'].max().date()} "
                  f"codes={df['code'].nunique():,} "
                  f"({time.time()-t0:.0f}s)", flush=True)
        except Exception as e:
            print(f"[{n}] FAIL: {e}", flush=True)


if __name__ == "__main__":
    main()
