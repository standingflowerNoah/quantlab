# -*- coding: utf-8 -*-
"""红利因子研究 · 阶段 0：成分池 + 多期限前瞻收益面板

产出（reports/dividend_factor/）：
  panel.parquet          全市场日频面板 (date, code, close, amount, tot_ret_{h})  h∈{5,10,20,40,60}
  pool_official.parquet  中证红利官方编制规则复现池（年调+缓冲区）日频成员
  pool_approx.parquet    官方规则月度复现池（近似，覆盖更长样本）日频成员

收益口径：kline_daily.close 为不复权；adj_factor 为含分红的复权因子
（实证：纯现金分红样本 close*adj_factor 环比 = 含息收益；含送转样本亦正确）
  tot_ret_h = close_{t+h}*adj_{t+h}/(close_t*adj_t) - 1
"""
from pathlib import Path
import pandas as pd
import numpy as np
import duckdb

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports/dividend_factor"
OUT.mkdir(parents=True, exist_ok=True)
con = duckdb.connect()
K = "read_parquet('data/lake/clean/mirror/kline_daily.parquet')"
DE = "read_parquet('data/lake/clean/mirror/dividend_events.parquet')"
H_GRID = [5, 10, 20, 40, 60]

# ---------------- 1. 面板 ----------------
print("=== 构建全市场面板 ===")
sel = ",\n  ".join(
    [f"LEAD(close,{h}) OVER w AS close_{h}, LEAD(adj_factor,{h}) OVER w AS adj_{h}, "
     f"LEAD(date,{h}) OVER w AS date_{h}" for h in H_GRID])
panel = con.execute(f"""
SELECT code, date, close, amount, adj_factor,
  {sel}
FROM {K}
WHERE close > 0 AND adj_factor IS NOT NULL
WINDOW w AS (PARTITION BY code ORDER BY date)
""").df()
panel["date"] = pd.to_datetime(panel["date"])
for h in H_GRID:
    panel[f"tot_ret_{h}"] = panel[f"close_{h}"] * panel[f"adj_{h}"] / (panel["close"] * panel["adj_factor"]) - 1
keep = ["code", "date", "close", "amount", "adj_factor"] + [f"tot_ret_{h}" for h in H_GRID]
panel = panel[keep + [f"date_{h}" for h in H_GRID]]
print(f"  面板 {len(panel):,} 行, {panel['code'].nunique()} 只, {panel['date'].min().date()} ~ {panel['date'].max().date()}")

# ---------------- 2. 官方规则池（年调） ----------------
print("=== 官方编制规则复现池（年调） ===")
kline = panel[["code", "date", "close", "amount"]].copy()
cash = con.execute(f"SELECT code, date, fenhong FROM {DE} "
                   f"WHERE fenhong>0 AND songzhuangu=0 AND peigu=0").df()
cash["date"] = pd.to_datetime(cash["date"])
cash["yr"] = cash["date"].dt.year
div_by_year = cash.groupby(["code", "yr"])["fenhong"].sum()
div_years = cash.groupby("code")["yr"].apply(set)

month_end = kline.groupby(kline["date"].dt.to_period("M"))["date"].max()

official = {}
for Y in [2022, 2023, 2024, 2025]:
    tl = month_end[month_end.index.astype(str).str.startswith(f"{Y}-11")]
    if not len(tl):
        print(f"  决策 {Y}-11 无数据，跳过")
        continue
    t = tl.iloc[-1]
    snap = kline[kline["date"] == t].set_index("code").copy()
    kd = kline[kline["date"] <= t].groupby("code").tail(250)
    amt = kd.groupby("code")["amount"].mean().rename("amt_avg")
    snap = snap.drop(columns=["amount", "amt_avg"], errors="ignore").join(amt, how="left")
    snap["amt_r"] = snap["amt_avg"].rank(ascending=False, pct=True)
    need = {Y - 1, Y - 2, Y - 3}
    snap["div3"] = [np.mean([div_by_year.get((c, y), 0.0) for y in need]) for c in snap.index]
    snap["cont3"] = [need.issubset(div_years.get(c, set())) for c in snap.index]
    d1 = cash[(cash["date"] > t - pd.Timedelta(days=365)) & (cash["date"] <= t)]
    div1y = d1.groupby("code")["fenhong"].sum().rename("div_1y")
    snap = snap.join(div1y, how="left")
    snap["div_1y"] = snap["div_1y"].fillna(0.0)
    snap["dy1"] = snap["div_1y"] / snap["close"]
    elig = snap[snap["cont3"] & (snap["amt_r"] <= 0.80) & (snap["close"] >= 2.0)].copy()
    elig["score"] = elig["div3"] / elig["close"]
    elig = elig.sort_values("score", ascending=False)
    prev = official.get(Y - 1)
    if prev:
        keep_ = [c for c in prev
                 if c in snap.index and snap.loc[c, "dy1"] > 0.005 and snap.loc[c, "amt_r"] <= 0.90]
        n_fill = 100 - len(keep_)
        add = [c for c in elig.index if c not in keep_][:max(n_fill, 0)]
        official[Y] = set(keep_ + add)
    else:
        official[Y] = set(elig.head(100).index)
    print(f"  决策 {Y}-11: 合格 {len(elig):4d} 只 → 池 {len(official[Y])} 只")

# 校准
try:
    cons = con.execute("SELECT stock_code FROM read_parquet("
                       "'data/lake/clean/index_members_ext/dividend_cons.parquet') "
                       "WHERE index_code='000922'").df()["stock_code"].tolist()
    ov = len(official[2025] & set(cons))
    print(f"  校准：官方规则池(2025决策) vs 中证官网当前成分 = {ov}/100")
except Exception as e:
    print("  校准失败:", str(e)[:80])


def decision_year(d):
    return d.year - 1 if d.month <= 11 else d.year


po = kline[["code", "date"]].copy()
po["dec_y"] = po["date"].map(decision_year)
po["in_pool"] = [c in official.get(y, set()) for c, y in zip(po["code"], po["dec_y"])]
pool_off = po[po["in_pool"]][["date", "code"]].drop_duplicates()
ps = pool_off.groupby("date").size()
print(f"  官方池日频: {len(pool_off):,} 行, 日均 {ps.mean():.0f} 只, "
      f"{pool_off['date'].min().date()} ~ {pool_off['date'].max().date()}")

# ---------------- 3. 近似池（月度重构） ----------------
print("=== 官方规则月度复现池（近似，长样本） ===")
me = kline[kline["date"].isin(month_end)].sort_values(["code", "date"]).copy()
me = me[me["close"] >= 2.0]
sd = me[["code", "date"]].drop_duplicates().sort_values(["code", "date"])
sd["prev_snap"] = sd.groupby("code")["date"].shift(1)
ev = cash.drop(columns="yr").merge(sd, left_on="code", right_on="code").rename(
    columns={"date_x": "ex_date", "date_y": "snap"})
ttm = ev[(ev["ex_date"] > ev["snap"] - pd.Timedelta(days=365)) & (ev["ex_date"] <= ev["snap"])]
ttm = ttm.groupby(["code", "snap"], as_index=False)["fenhong"].sum().rename(
    columns={"fenhong": "ttm_div", "snap": "date"})
me = me.merge(ttm, on=["code", "date"], how="left")
me["ttm_div"] = me["ttm_div"].fillna(0.0)
me["dy"] = me["ttm_div"] / me["close"]
me["yr"] = me["date"].dt.year

parts = []
for d, g in me.groupby("date"):
    y = g["yr"].iloc[0]
    need = {y - 1, y - 2, y - 3}
    ok = [c for c in g["code"].unique() if need.issubset(div_years.get(c, set()))]
    gg = g[g["code"].isin(ok) & (g["dy"] > 0)].nlargest(100, "dy")
    if len(gg) >= 50:
        parts.append(gg[["date", "code"]])
pool_ap = pd.concat(parts).drop_duplicates()
psa = pool_ap.groupby("date").size()
print(f"  近似池月末: {len(pool_ap):,} 行, 均 {psa.mean():.0f} 只, "
      f"{pool_ap['date'].min().date()} ~ {pool_ap['date'].max().date()}")

# ---------------- 4. 落盘 ----------------
panel.to_parquet(OUT / "panel.parquet", index=False)
pool_off.to_parquet(OUT / "pool_official.parquet", index=False)
pool_ap.to_parquet(OUT / "pool_approx.parquet", index=False)
print(f"\n落盘完成 → {OUT}")

# ---------------- 5. 体检 ----------------
print("\n=== 恒等式体检（池内等权 持有 vs 区间累计） ===")
sub = panel.merge(pool_off, on=["date", "code"], how="inner")
for h in H_GRID:
    ok_rows = sub.dropna(subset=[f"tot_ret_{h}"])
    print(f"  h={h:2d}: 池内 {ok_rows.groupby('date').size().mean():.0f} 只/日, "
          f"区间均收益 {ok_rows[f'tot_ret_{h}'].mean()*100:+.2f}%, "
          f"日频池均值时序 {ok_rows.groupby('date')[f'tot_ret_{h}'].mean().mean()*100:+.2f}%")
