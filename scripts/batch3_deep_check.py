"""第三批 A5 深度检验：方向性闸门 / IC 衰减 / 分年度 / 冗余
=====================================
方向性闸门（skill 强制）：事件首次发生前因子值必须为 0。
实现方式为 rolling 过去窗口求和（天然因果），但仍在数据层独立复核：
抽样股票用 pandas 独立重算滚动事件计数，与落库值逐日对比。
"""
import sys
sys.path.insert(0, '.')

import numpy as np
import pandas as pd

from quantlab.data.store import Store, query
from quantlab.factor.quality import factor_ic, factor_ic_decay
from quantlab.factor.correlation import factor_correlation

NEW = ["ev_high_vol_20", "ev_low_vol_20"]
STORE = Store()


# ── 1. 方向性闸门：独立重算复核 ─────────────────────────────
print("=" * 78)
print("1. 方向性闸门：pandas 独立重算 vs 落库值（抽样 30 股全历史）")
print("=" * 78)
sample_codes = [r["code"] for r in query(
    "SELECT code FROM (SELECT DISTINCT code FROM kline_daily) USING SAMPLE 30"
).to_dict("records")]
fk = STORE.read_factor("ev_high_vol_20")
fk["date"] = pd.to_datetime(fk["date"])
fl = STORE.read_factor("ev_low_vol_20")
fl["date"] = pd.to_datetime(fl["date"])
smp = set(sample_codes)

k = query("""
    SELECT date, code, close, vol FROM kline_daily
    WHERE close > 0 AND vol > 0
    ORDER BY code, date
""")
k["date"] = pd.to_datetime(k["date"])

mismatch = 0
checked = 0
for code, g in k[k["code"].isin(smp)].groupby("code"):
    g = g.sort_values("date")
    q80v = g["vol"].rolling(60, min_periods=45).quantile(0.8)
    q80p = g["close"].rolling(60, min_periods=45).quantile(0.8)
    q20p = g["close"].rolling(60, min_periods=45).quantile(0.2)
    ev_hi = ((g["vol"].values >= q80v.values) &
             (g["close"].values >= q80p.values) &
             q80v.notna().values).astype(int)
    ev_lo = ((g["vol"].values >= q80v.values) &
             (g["close"].values <= q20p.values) &
             q80v.notna().values).astype(int)
    ref_hi = pd.Series(ev_hi, index=g["date"]).rolling(20, min_periods=15).sum()
    ref_lo = pd.Series(ev_lo, index=g["date"]).rolling(20, min_periods=15).sum()
    gh = fk[fk["code"] == code].set_index("date")["value"].reindex(g["date"])
    gl = fl[fl["code"] == code].set_index("date")["value"].reindex(g["date"])
    ok_h = np.allclose(gh.values, ref_hi.values, equal_nan=True)
    ok_l = np.allclose(gl.values, ref_lo.values, equal_nan=True)
    checked += 1
    if not (ok_h and ok_l):
        mismatch += 1
        print(f"  MISMATCH {code}: hi={ok_h} lo={ok_l}")
print(f"抽样 {checked} 股：{'全部一致 ✓（事件计数 = 过去20日滚动和，无前视）' if mismatch == 0 else f'{mismatch} 股不一致 ✗'}")

# ── 2. IC 衰减 ─────────────────────────────────────────────
print("\n" + "=" * 78)
print("2. IC 衰减分析（horizon 1/3/5/10/20/60）")
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

# ── 3. 分年度 IC（horizon=20 与 5）──────────────────────────
for horizon in (20, 5):
    print(f"\n{'='*78}\n3. 分年度 IC（horizon={horizon}）\n{'='*78}")
    for name in NEW:
        ics = factor_ic(name, horizon=horizon)
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

# ── 4. 冗余检查 ─────────────────────────────────────────────
print("\n" + "=" * 78)
print("4. 冗余检查（新因子 vs 现有因子，2024 年起抽样 40 日）")
print("=" * 78)
existing = ["momentum_20", "reversal_5", "reversal_10", "price_position_250",
            "volatility_20", "turnover", "amihud_20", "vol_ratio",
            "overnight_mom_20", "chip_vwap_bias_250", "sue"]
corr = factor_correlation(NEW + existing, sample_dates=40, start="2024-01-01")
if not corr.empty:
    pd.set_option("display.width", 220)
    sub = corr.loc[NEW, :] if hasattr(corr, "loc") else corr
    print(corr.round(2).to_string())
