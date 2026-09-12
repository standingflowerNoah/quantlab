"""为因子中心展示页计算 PandaAI 同款指标的真实数据（amihud_20，h=20）
输出 factor_center_demo_data.json，供 HTML 渲染。"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FACTOR_DIR = ROOT / "data" / "lake" / "factor"
DB = ROOT / "data" / "quant.duckdb"
OUT = ROOT / "reports" / "factor_center_demo_data.json"

START, END, H = "2023-01-01", "2026-09-11", 20

plist = [str(p) for p in sorted((FACTOR_DIR / "amihud_20").glob("part-*.parquet"))]
con = duckdb.connect()
con.execute(f"ATTACH '{DB}' AS maindb (READ_ONLY)")
q = f"""
WITH f AS (
    SELECT date, code, value FROM read_parquet({plist})
    WHERE date BETWEEN DATE '{START}' AND DATE '{END}'),
px AS (SELECT date, code, close * adj_factor AS c FROM maindb.kline_daily),
base AS (
    SELECT f.date, f.code, f.value AS v,
           LEAD(px.c, {H}) OVER (PARTITION BY f.code ORDER BY f.date) / px.c - 1 AS fwd
    FROM f JOIN px ON f.date = px.date AND f.code = px.code),
ranked AS (
    SELECT date, fwd, v,
           NTILE(5) OVER (PARTITION BY date ORDER BY v) AS q,
           RANK() OVER (PARTITION BY date ORDER BY v) AS rv,
           RANK() OVER (PARTITION BY date ORDER BY fwd) AS rf
    FROM base WHERE fwd IS NOT NULL)
SELECT date,
       corr(rv, rf) AS ic,
       AVG(CASE WHEN q = 1 THEN fwd END) AS q1,
       AVG(CASE WHEN q = 2 THEN fwd END) AS q2,
       AVG(CASE WHEN q = 3 THEN fwd END) AS q3,
       AVG(CASE WHEN q = 4 THEN fwd END) AS q4,
       AVG(CASE WHEN q = 5 THEN fwd END) AS q5,
       COUNT(*) AS n
FROM ranked GROUP BY date ORDER BY date
"""
df = con.execute(q).df()
con.execute("DETACH maindb")
con.close()
df["date"] = pd.to_datetime(df["date"])

ic = df["ic"].dropna()
n = len(ic)
rho = float(ic.autocorr(1))
n_eff = n * (1 - rho) / (1 + rho)
t_adj = float(ic.mean() / ic.std() * np.sqrt(n_eff))

def group_stats(r: pd.Series) -> dict:
    r = r.dropna()
    ann = float(r.mean() * 252)
    vol = float(r.std() * np.sqrt(252))
    nav = (1 + r).cumprod()
    mdd = float((nav / nav.cummax() - 1).min())
    mon = r.groupby([r.index.year, r.index.month]).apply(
        lambda s: (1 + s).prod() - 1)
    return {"ann_ret": round(ann, 4), "ann_vol": round(vol, 4),
            "sharpe": round(ann / vol, 2) if vol else None,
            "mdd": round(mdd, 3), "mon_win": round(float((mon > 0).mean()), 3)}

df = df.set_index("date")

def group_stats(r: pd.Series) -> dict:
    """r 为 20 日远期收益序列；展示口径=逐日摊薄（r/20）的日频组合序列"""
    daily = (r / H).dropna()
    ann = float(daily.mean() * 252)
    vol = float(daily.std() * np.sqrt(252))
    nav = (1 + daily).cumprod()
    mdd = float((nav / nav.cummax() - 1).min())
    mon = daily.groupby([daily.index.year, daily.index.month]).apply(
        lambda s: (1 + s).prod() - 1)
    return {"ann_ret": round(ann, 4), "ann_vol": round(vol, 4),
            "sharpe": round(ann / vol, 2) if vol else None,
            "mdd": round(mdd, 3), "mon_win": round(float((mon > 0).mean()), 3),
            "mean_fwd_bp": round(float(r.mean()) * 1e4, 1)}

groups = {f"Q{i}": group_stats(df[f"q{i}"]) for i in range(1, 6)}
ls = df["q5"] - df["q1"]
groups["LS"] = group_stats(ls)
# 摊薄日频净值曲线（供图表）
navs = {}
for i in range(1, 6):
    d = (df[f"q{i}"] / H).dropna()
    navs[f"Q{i}"] = [round(float(v), 4) for v in (1 + d).cumprod()]
navs["LS"] = [round(float(v), 4) for v in (1 + (ls / H).dropna()).cumprod()]

# IC 月度矩阵
ic_s = df["ic"].dropna()
monthly = (ic_s.groupby([ic_s.index.year, ic_s.index.month]).mean()
           .unstack().round(4))
monthly.index = [int(i) for i in monthly.index]
monthly.columns = [int(c) for c in monthly.columns]

out = {
    "ic_stats": {
        "mean": round(float(ic.mean()), 4), "std": round(float(ic.std()), 4),
        "skew": round(float(ic.skew()), 3), "kurt": round(float(ic.kurt()), 3),
        "lag1_autocorr": round(rho, 3), "n_days": n, "n_eff": round(n_eff, 0),
        "t_adj": round(t_adj, 2),
        "p_ic_pos": round(float((ic > 0.02).mean()), 4),
        "p_ic_neg": round(float((ic < -0.02).mean()), 4),
        "win_rate": round(float((ic > 0).mean()), 4),
    },
    "groups": groups,
    "navs": navs,
    "nav_dates": [f"{d:%Y-%m-%d}" for d in df.index],
    "monthly_ic": {"years": list(monthly.index),
                   "months": list(monthly.columns),
                   "values": [[None if pd.isna(v) else round(float(v), 4)
                               for v in row]
                              for row in monthly.values]},
    "n_days_quintile": int(len(df)),
    # ── 以下为展示页补充：IC 序列 / 自相关 / Pearson IC / 最新 Top 值 ──
    "ic_series": [round(float(v), 4) for v in ic.values],
    "ic_acf": [round(float(ic.autocorr(k)), 3) for k in range(1, 11)],
}
OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1),
               encoding="utf-8")

# Pearson IC（PandaAI 卡片 IC_MEAN 口径）+ 最新日 Top 因子值
con = duckdb.connect()
con.execute(f"ATTACH '{DB}' AS maindb (READ_ONLY)")
pear = con.execute(f"""
    WITH f AS (SELECT date, code, value FROM read_parquet({plist})
               WHERE date BETWEEN DATE '{START}' AND DATE '{END}'),
    px AS (SELECT date, code, close * adj_factor AS c FROM maindb.kline_daily),
    base AS (
        SELECT f.date, f.code, f.value AS v,
               LEAD(px.c, {H}) OVER (PARTITION BY f.code ORDER BY f.date)
                   / px.c - 1 AS fwd
        FROM f JOIN px ON f.date = px.date AND f.code = px.code)
    SELECT date, corr(v, fwd) AS pic FROM base
    WHERE fwd IS NOT NULL GROUP BY date
""").df()
out["ic_pearson_mean"] = round(float(pear["pic"].mean()), 4)

last_day = con.execute(
    f"SELECT max(date) AS d FROM read_parquet({plist})").df()["d"][0]
top = con.execute(f"""
    SELECT value, code FROM read_parquet({plist})
    WHERE date = DATE '{last_day}' ORDER BY value DESC LIMIT 10
""").df()
out["latest"] = {"date": f"{pd.to_datetime(last_day):%Y-%m-%d}",
                 "top": [{"code": r["code"],
                          "value": round(float(r["value"]), 4)}
                         for _, r in top.iterrows()]}
con.execute("DETACH maindb")
con.close()
OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1),
               encoding="utf-8")
print("pearson_ic:", out["ic_pearson_mean"], "| latest:", out["latest"]["date"])
print(json.dumps(out["ic_stats"], ensure_ascii=False, indent=1))
print({k: v["ann_ret"] for k, v in groups.items()})
print(f"[saved] {OUT}")
