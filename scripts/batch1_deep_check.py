"""第一批因子深度检验：IC 衰减 / 分年度 / 冗余 / 参数敏感性"""
import sys
sys.path.insert(0, '.')

import numpy as np
import pandas as pd

from quantlab.factor.quality import factor_ic, factor_ic_decay
from quantlab.factor.correlation import factor_correlation

NEW = ["sue", "earnings_accel"]

# ── 1. IC 衰减（找最优持有期）────────────────────────────────────
print("=" * 70)
print("1. IC 衰减分析")
print("=" * 70)
for name in NEW:
    decay = factor_ic_decay(name, horizons=[1, 3, 5, 10, 20, 60])
    print(f"\n{name}:")
    print(decay.to_string(index=False))

# ── 2. 分年度 IC（稳定性）────────────────────────────────────────
print("\n" + "=" * 70)
print("2. 分年度 IC（horizon=20）")
print("=" * 70)
for name in NEW:
    ics = factor_ic(name, horizon=20)
    if ics.empty:
        print(f"{name}: 无数据"); continue
    ics["year"] = pd.to_datetime(ics["date"]).dt.year
    g = ics.groupby("year")["ic"]
    stats = pd.DataFrame({
        "ic_mean": g.mean().round(4),
        "icir": (g.mean() / g.std()).round(3),
        "win_rate": (g.apply(lambda s: (s > 0).mean())).round(3),
        "n_days": g.count(),
    })
    print(f"\n{name} (horizon=20):")
    print(stats.to_string())

# ── 3. 冗余检查：与现有基本面/成长因子截面秩相关 ─────────────────
print("\n" + "=" * 70)
print("3. 冗余检查（与现有因子截面秩相关，2025 年起抽样）")
print("=" * 70)
existing = ["profit_growth_yoy", "revenue_growth_yoy", "roe", "ep", "bp",
            "momentum_20", "reversal_5"]
names = NEW + existing
corr = factor_correlation(names, sample_dates=40, start="2025-05-01")
if not corr.empty:
    for n in NEW:
        row = corr.loc[n] if n in corr.index else None
        if row is None: continue
        row = row.drop(labels=[n]).dropna()
        row = row.reindex(row.abs().sort_values(ascending=False).index)
        print(f"\n{n} vs 现有因子（按|ρ|降序）:")
        print(row.round(3).to_string())

# ── 4. SUE 参数敏感性：min_win ∈ {4, 6, 8} ─────────────────────
print("\n" + "=" * 70)
print("4. SUE min_win 参数敏感性（临时计算，不落库）")
print("=" * 70)
from quantlab.factor.registry import get_factor
from quantlab.data.store import Store
store = Store()
import quantlab.factor.library.earnings_surprise as es

for mw in [4, 6, 8]:
    f = get_factor("sue")
    f._min_win = mw
    sql, params = f._sql(None, None, None)
    df = store.q(sql, params)
    if df.empty:
        print(f"min_win={mw}: 无数据"); continue
    # 逐日 IC20（复用 quality 的 SQL 路径太重，这里直接 pandas 算）
    from quantlab.factor.quality import _fwd_return
    fwd = _fwd_return(20)
    m = df.merge(fwd, on=["date", "code"], how="inner")
    m["date"] = pd.to_datetime(m["date"])
    ics = []
    for d, g in m.groupby("date"):
        if len(g) >= 30:
            ics.append(g["value"].rank().corr(g["fwd"].rank()))
    ic = pd.Series(ics)
    print(f"min_win={mw}: dates={df['date'].min().date()}~{df['date'].max().date()} "
          f"rows={len(df)} | IC20={ic.mean():.4f} ICIR={ic.mean()/ic.std():.3f} "
          f"win={(ic>0).mean():.3f}")
