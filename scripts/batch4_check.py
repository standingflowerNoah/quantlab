"""第四批因子检验：B1 股东户数（IC 衰减 / 分年度 / 冗余）"""
import sys
sys.path.insert(0, '.')

import pandas as pd

from quantlab.factor.quality import factor_ic, factor_ic_decay
from quantlab.factor.correlation import factor_correlation

NEW = ["holder_num_chg", "holder_num_chg_2"]

# ── 1. IC 衰减 ─────────────────────────────────────────────
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
if frames:
    all_decay = pd.concat(frames)
    print(all_decay.pivot(index="factor", columns="horizon", values="ic_mean").round(4).to_string())
    print("\nICIR:")
    print(all_decay.pivot(index="factor", columns="horizon", values="icir").round(3).to_string())

# ── 2. 分年度 IC ───────────────────────────────────────────
for horizon in (20, 5):
    print(f"\n{'='*78}\n2. 分年度 IC（horizon={horizon}）\n{'='*78}")
    for name in NEW:
        ics = factor_ic(name, horizon=horizon)
        if ics.empty:
            print(f"{name}: 无 IC"); continue
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

# ── 3. 冗余检查 ────────────────────────────────────────────
print("\n" + "=" * 78)
print("3. 冗余检查（新因子 vs 现有因子，2024 年起抽样 40 日）")
print("=" * 78)
existing = ["size", "turnover", "momentum_20", "reversal_5", "amihud_20",
            "volatility_20", "ep", "sue", "chip_vwap_bias_250", "ev_high_vol_20"]
corr = factor_correlation(NEW + existing, sample_dates=40, start="2024-01-01")
if not corr.empty:
    pd.set_option("display.width", 220)
    print(corr.round(2).to_string())
