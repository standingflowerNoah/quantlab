"""Alpha191 抽样 PIT 实证重放。

对 |IC|>0.03 的 alpha191 因子中 IC 最高的 5 个，在 3 个 t0 时点
把宽表截断到 <=t0 重算因子截面，与因子库全量值对比。
任何不一致 = 公式使用了 t0 之后的数据（穿越）或结果路径依赖。

框架静态审查结论（另见 pit_audit_ic03 汇总）：
- 全部算子为 shift(正)/rolling 回看，formulas 中 shift(- 出现次数 = 0
- 输入宽表仅来自 kline_daily（date, code, OHLC, vwap, volume, amount）
"""
from __future__ import annotations

import glob
import sys

sys.path.insert(0, ".")

import duckdb
import numpy as np
import pandas as pd

from quantlab.factor.alpha191 import WIDE_KEYS, build_wide
from quantlab.factor.alpha191_formulas import ALPHAS
from quantlab.data.store import Store

PICK = ["alpha041", "alpha168", "alpha118", "alpha187", "alpha124"]
T0S = ["2025-06-30", "2026-01-30", "2026-06-30"]


def lake截面(name: str, t0: str) -> pd.Series:
    files = [f.replace("\\", "/") for f in
             glob.glob(f"data/lake/factor/{name}/part-*.parquet")]
    con = duckdb.connect()
    df = con.execute(
        f"SELECT CAST(date AS DATE) date, code, value "
        f"FROM read_parquet({str(files)}) "
        f"WHERE CAST(date AS DATE) = DATE '{t0}'").df()
    con.close()
    return df.set_index("code")["value"] if len(df) else pd.Series(dtype=float)


def main() -> None:
    store = Store()
    for t0 in T0S:
        wide = build_wide(store, "2022-06-01")
        for k in WIDE_KEYS:
            wide[k] = wide[k].loc[:t0]
        print(f"\n===== t0 = {t0}（宽表截至 {wide['close'].index.max().date()}）=====")
        for name in PICK:
            fn = ALPHAS[name][0]
            res = fn({k: v.copy() for k, v in wide.items()})
            cut = res.loc[t0].dropna() if t0 in res.index else pd.Series(dtype=float)
            lake = lake截面(name, t0)
            if lake.empty:
                print(f"  {name:9s} 因子库无 {t0} 截面（跳过）")
                continue
            common = cut.index.intersection(lake.index)
            a, b = cut.reindex(common), lake.reindex(common)
            scale = np.abs(a).max() if len(a) and np.abs(a).max() > 0 else 1.0
            diff = (a - b).abs() / scale
            n_diff = int((diff > 1e-6).sum())
            print(f"  {name:9s} 重放 {len(common):5d} 只 | 不一致 {n_diff:4d} "
                  f"| 最大相对差 {diff.max():.2e}")


if __name__ == "__main__":
    main()
