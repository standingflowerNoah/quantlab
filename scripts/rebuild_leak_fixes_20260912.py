#!/usr/bin/env python3
"""rebuild_leak_fixes_20260912.py — 泄露修复后 4 因子整年重写（replace=True）

口径变更（泄露修复）后必须整年重写，否则 append 模式残留旧行：
- alpha046 / alpha069: 布尔 NaN→False 陷阱修复（未上市股票 cnt=0 → NaN）
- sue / earnings_accel: 记账日改 GREATEST(当期, 窗口上期 notice_eff)

单写者纪律：四因子顺序执行，不并行。
"""
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import pandas as pd

from quantlab.config import get_logger

log = get_logger("rebuild_leak_fixes")

# ---------- 1) alpha046 / alpha069（宽表公式引擎，手动 replace=True 落库） ----------
def rebuild_alpha(names=("alpha046", "alpha069")):
    from quantlab.data.store import Store
    from quantlab.factor.alpha191 import ALPHAS, _sanity, _to_long, build_wide

    store = Store()
    wide = build_wide(store, "2022-06-01")
    rows = []
    for name in names:
        fn, conf = ALPHAS[name]
        res = fn(wide)
        ok, note = _sanity(res)
        if not ok:
            raise RuntimeError(f"{name} sanity FAIL: {note}")
        long = _to_long(res)
        n = store.append_factor(name, long, replace=True)
        rows.append((name, n, str(long["date"].min().date()), str(long["date"].max().date())))
        log.info(f"{name} rebuild ok: {n} 行 {long['date'].min().date()} ~ {long['date'].max().date()} ({note})")
    return rows


# ---------- 2) sue / earnings_accel（SQL 因子，compute_factor rebuild=True） ----------
def rebuild_sql(names=("sue", "earnings_accel")):
    from quantlab.factor.compute import compute_factor

    rows = []
    for name in names:
        df = compute_factor(name, rebuild=True)   # 全市场全史 + 整年重写
        rows.append((name, len(df), str(df["date"].min().date()), str(df["date"].max().date())))
        log.info(f"{name} rebuild ok: {len(df)} 行 {df['date'].min().date()} ~ {df['date'].max().date()}")
    return rows


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    out = []
    if which in ("all", "alpha"):
        out += rebuild_alpha()
    if which in ("all", "sql"):
        out += rebuild_sql()
    print("\n=== rebuild 结果 ===")
    for name, n, d0, d1 in out:
        print(f"{name:16s} {n:>9d} 行  {d0} ~ {d1}")
    print("ALL DONE")
