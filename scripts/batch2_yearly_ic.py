"""第二批: 分年度 IC(h=20) + 冗余矩阵 → reports/batch2_yearly_ic.json (含对照现有因子)"""
import sys
sys.path.insert(0, '.')

import json
import numpy as np
import pandas as pd

from quantlab.factor.quality import factor_ic
from quantlab.factor.correlation import factor_correlation

NEW = [
    "overnight_mom_20", "intraday_mom_5",
    "overnight_ratio_20", "overnight_vol_ratio_20",
    "chip_age_short_10", "chip_age_mid_60",
    "chip_vwap_bias_250",
]
EXISTING = ["momentum_20", "reversal_5", "price_position_250", "volatility_20", "turnover"]

out = {"yearly_ic20": {}, "corr_matrix": None}

# ── 1. 分年度 IC（horizon=20），新 7 + 现有 5 ──
for name in NEW + EXISTING:
    ics = factor_ic(name, horizon=20)
    if ics.empty:
        print(f"{name}: 无数据")
        continue
    ics["year"] = pd.to_datetime(ics["date"]).dt.year
    g = ics.groupby("year")["ic"]
    stats = {
        str(y): {
            "ic": round(float(m), 4),
            "icir": round(float(m / s), 3) if s and np.isfinite(s) and s > 0 else None,
            "n": int(n),
        }
        for y, (m, s, n) in zip(g.mean().index, zip(g.mean(), g.std(), g.count()))
    }
    out["yearly_ic20"][name] = stats
    print(f"{name}: " + "  ".join(f"{y}:{v['ic']:+.4f}" for y, v in stats.items()))

# ── 2. 冗余矩阵（新 7 + 现有 7，2024 起抽样 40 日）──
corr = factor_correlation(NEW + ["sue", "amihud_20"] + EXISTING, sample_dates=40, start="2024-01-01")
if not corr.empty:
    out["corr_matrix"] = {str(k): {str(kk): (None if pd.isna(v) else round(float(v), 3))
                                   for kk, v in row.items()}
                          for k, row in corr.iterrows()}
    pd.set_option("display.width", 220)
    print("\n冗余矩阵（|ρ|>0.7 标记）:")
    print(corr.round(2).to_string())

with open("reports/batch2_yearly_ic.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("\n已写入 reports/batch2_yearly_ic.json")
