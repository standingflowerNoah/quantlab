# -*- coding: utf-8 -*-
"""复现因子 vs 库内已有因子的截面相关去重检验（月末截面 spearman 平均）。

对齐 hf_r4_dedup 口径：取月末截面，与核心风格因子 + 同编号 191 计算 pairwise spearman，
输出平均 |rho| 与最大 |rho|（对哪个因子）。

用法:
  quantlab venv python panda_pull/corr_check.py
"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, ".")

PA_FACTORS = ["pa_a101_040", "pa_a101_044", "pa_a101_088",
              "pa_5d_min_low_ratio", "pa_30d_close_avg_ratio",
              "pa_30d_vol_dec_ratio", "pa_early_afternoon_ret"]
# 对照集：核心生产/风格 + 逻辑近邻 + 同编号 191 版
REF = ["size", "amihud_20", "momentum_20", "momentum_60", "reversal_5", "reversal_10",
       "turnover", "amount_std_20", "volatility_20", "price_position_250",
       "anchor_reversal_20", "morning_mist_20", "overnight", "volvol_20",
       "alpha040", "alpha044", "alpha088", "alpha140"]


def load_month_end(name: str) -> pd.DataFrame:
    from quantlab.config import FACTOR_DIR
    parts = sorted((FACTOR_DIR / name).glob("part-*.parquet"))
    if not parts:
        return pd.DataFrame()
    df = pd.read_parquet(parts)
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["date"] >= "2023-01-01"]
    # 月末截面：每月最后一个可用日
    df = df.sort_values("date")
    last_days = df.groupby(df["date"].dt.to_period("M"))["date"].max()
    df = df[df["date"].isin(set(last_days))]
    return df.pivot(index="date", columns="code", values="value")


def mean_rank_corr(A: pd.DataFrame, B: pd.DataFrame) -> float:
    """共同月末截面 spearman 均值（按行 rank 后对齐列算 corr）"""
    common_dates = A.index.intersection(B.index)
    if len(common_dates) < 6:
        return np.nan
    rs = []
    for d in common_dates:
        a = A.loc[d].rank()
        b = B.loc[d].rank()
        m = pd.concat([a, b], axis=1, join="inner").dropna()
        if len(m) < 50:
            continue
        rs.append(m.corr(method="spearman").iloc[0, 1])
    return float(np.mean(rs)) if rs else np.nan


def main():
    from quantlab.config import FACTOR_DIR
    print(f"{'pa_factor':26s} {'ref':22s} {'mean_rho':>9s}")
    summary = []
    for pa in PA_FACTORS:
        if not (FACTOR_DIR / pa).exists():
            print(f"{pa}: NOT IN LAKE, skip")
            continue
        A = load_month_end(pa)
        best = ("", 0.0)
        for ref in REF:
            B = load_month_end(ref)
            if B.empty:
                continue
            r = mean_rank_corr(A, B)
            if np.isnan(r):
                continue
            summary.append({"pa_factor": pa, "ref": ref, "rho": round(r, 3)})
            mark = " <-- MAX" if abs(r) > abs(best[1]) else ""
            if abs(r) > abs(best[1]):
                best = (ref, r)
            print(f"{pa:26s} {ref:22s} {r:9.3f}{mark}")
        print(f"{pa:26s} => max |rho| = {abs(best[1]):.3f} vs {best[0]}")
        print()
    pd.DataFrame(summary).to_csv("reports/_tmp/pa_corr_check.csv", index=False)
    print("[out] reports/_tmp/pa_corr_check.csv")


if __name__ == "__main__":
    main()
