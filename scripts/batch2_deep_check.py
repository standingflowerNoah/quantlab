"""第二批因子深度检验：IC 衰减 / 分年度 / 冗余"""
import sys
sys.path.insert(0, '.')

import numpy as np
import pandas as pd

from quantlab.factor.quality import factor_ic, factor_ic_decay
from quantlab.factor.correlation import factor_correlation

NEW = [
    "overnight_mom_20", "intraday_mom_5",
    "overnight_ratio_20", "overnight_vol_ratio_20",
    "chip_age_short_10", "chip_age_mid_60",
    "chip_vwap_bias_250",
]

# ── 1. IC 衰减 ───────────────────────────────────────────────────
print("=" * 78)
print("1. IC 衰减分析（horizon 1/3/5/10/20/60）")
print("=" * 78)
frames = []
for name in NEW:
    decay = factor_ic_decay(name, horizons=[1, 3, 5, 10, 20, 60])
    if decay.empty:
        print(f"{name}: 无数据"); continue
    decay.insert(0, "factor", name)
    frames.append(decay)
all_decay = pd.concat(frames)
print(all_decay.pivot(index="factor", columns="horizon", values="ic_mean").round(4).to_string())
print("\nICIR:")
print(all_decay.pivot(index="factor", columns="horizon", values="icir").round(3).to_string())

# ── 2. 分年度 IC（horizon=20）────────────────────────────────────
print("\n" + "=" * 78)
print("2. 分年度 IC（horizon=20）")
print("=" * 78)
for name in NEW:
    ics = factor_ic(name, horizon=20)
    if ics.empty:
        continue
    ics["year"] = pd.to_datetime(ics["date"]).dt.year
    g = ics.groupby("year")["ic"]
    stats = pd.DataFrame({
        "ic_mean": g.mean().round(4),
        "icir": (g.mean() / g.std()).round(3),
        "win": g.apply(lambda s: (s > 0).mean()).round(3),
        "n": g.count(),
    })
    print(f"\n{name}:")
    print(stats.to_string())

# ── 3. 冗余检查 ──────────────────────────────────────────────────
print("\n" + "=" * 78)
print("3. 冗余检查（新因子 vs 现有因子 + 新因子互相，2024 年起抽样 40 日）")
print("=" * 78)
existing = ["momentum_20", "reversal_5", "price_position_250", "volatility_20",
            "turnover", "sue", "amihud_20"]
corr = factor_correlation(NEW + existing, sample_dates=40, start="2024-01-01")
if not corr.empty:
    pd.set_option("display.width", 200)
    print(corr.round(2).to_string())
