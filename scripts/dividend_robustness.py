# -*- coding: utf-8 -*-
"""近似池（月度复现池）展开为日频成员 + 最终模型的池稳健性复验

pool_approx.parquet 只含月末截面 → 按 PIT 规则（t 月末决策，用于 (t, t+1月末]）
展开为日频成员表 pool_approx_daily.parquet。
然后在近似池上复算 M1/M2/M3/M4/M5，检验"结论对池定义不敏感"。
"""
import numpy as np
import pandas as pd
from pathlib import Path
from dividend_engine import DivData, H_GRID

OUT = Path(__file__).resolve().parent.parent / "reports/dividend_factor"
COST = 0.003

# ---------- 1. 展开日频 ----------
if not (OUT / "pool_approx_daily.parquet").exists():
    panel = pd.read_parquet(OUT / "panel.parquet")
    m = pd.read_parquet(OUT / "pool_approx.parquet")
    dates = pd.DatetimeIndex(sorted(panel["date"].unique()))
    snap = pd.DatetimeIndex(sorted(m["date"].unique()))
    parts = []
    for i, d in enumerate(snap):
        nxt = snap[i + 1] if i + 1 < len(snap) else dates[-1] + pd.Timedelta(days=1)
        win = dates[(dates > d) & (dates <= nxt)]
        if not len(win):
            continue
        codes = m.loc[m["date"] == d, "code"].unique()
        parts.append(pd.DataFrame({"date": np.repeat(win, len(codes)),
                                   "code": np.tile(codes, len(win))}))
    daily = pd.concat(parts).drop_duplicates()
    daily.to_parquet(OUT / "pool_approx_daily.parquet", index=False)
    print(f"近似池展开: {len(daily):,} 行 / {daily['code'].nunique()} 只 / "
          f"{daily['date'].min().date()} ~ {daily['date'].max().date()}")
else:
    print("pool_approx_daily.parquet 已存在，跳过展开")

# ---------- 2. 池稳健性复验 ----------
MODELS = {
    "M1 amihud_20 (单因子)": ["amihud_20"],
    "M2 size+amihud (PROD)": ["size", "amihud_20"],
    "M3 size+amihud+momentum": ["size", "amihud_20", "momentum_60"],
}
g = pd.read_csv(OUT / "greedy_loose_official.csv")
g = g[g["factors"].astype(str).str.len() > 0]
if len(g):
    i = g["is_net_ann"].idxmax()
    MODELS["M5 两段一致贪心"] = str(g.loc[i, "factors"]).split("|")
g4 = pd.read_csv(OUT / "greedy_is_official.csv")
g4 = g4[g4["factors"].astype(str).str.len() > 0]
if len(g4):
    i = g4["is_net_ann"].idxmax()
    MODELS["M4 IS贪心"] = str(g4.loc[i, "factors"]).split("|")

rows = []
for pool, kw in [("official", {}), ("approx_daily", dict(dir_pool="official", screen_pool="official"))]:
    D = DivData(pool, **kw)
    print(f"\n[{pool}] {len(D.CODES)} 只 / {len(D.DATES)} 交易日 / "
          f"{D.DATES[0].date()} ~ {D.DATES[-1].date()}")
    for nm, cols in MODELS.items():
        try:
            X = D.mat(cols)
        except Exception as e:
            print(f"  {nm} 载入失败: {str(e)[:60]}")
            continue
        for h in H_GRID:
            a = D.evaluate(X, h, 10, COST, D.IS_LIM)
            b = D.evaluate(X, h, 10, COST, D.OOS_LIM)
            f = D.evaluate(X, h, 10, COST, D.FULL_LIM)
            if not (a and b):
                continue
            rows.append(dict(pool=pool, model=nm, horizon=h,
                             is_net=a["net_ann"], oos_net=b["net_ann"], full_net=f["net_ann"],
                             is_t=a["t_nw"], oos_t=b["t_nw"], turnover=f["turnover"]))
r = pd.DataFrame(rows)
r.to_csv(OUT / "pool_robustness.csv", index=False)

print("\n=== 池稳健性：净超年化（IS / OOS / 全） ===")
for pool in r["pool"].unique():
    print(f"\n--- {pool} ---")
    s = r[r.pool == pool]
    print(s.pivot(index="model", columns="horizon", values="is_net").round(1).to_string())
    print("  OOS:")
    print(s.pivot(index="model", columns="horizon", values="oos_net").round(1).to_string())

print("\n=== 两池对照（全样本净超年化，各模型最优持有期） ===")
tab = []
for pool in r["pool"].unique():
    s = r[r.pool == pool]
    for nm in s["model"].unique():
        q = s[s.model == nm]
        if not len(q):
            continue
        i = q["full_net"].idxmax()
        tab.append(dict(pool=pool, model=nm, best_h=int(q.loc[i, "horizon"]),
                        is_net=q.loc[i, "is_net"], oos_net=q.loc[i, "oos_net"],
                        full_net=q.loc[i, "full_net"]))
print(pd.DataFrame(tab).round(2).to_string(index=False))
print("\nDONE")
