# -*- coding: utf-8 -*-
"""红利 2C 第二轮：红利指数成分池内精选（少持股集中组合）
池：编制规则近似的中证红利成分（月度重构）
  - 条件：过去 3 个完整日历年每年均有现金分红（dividend_events 实证）
  - 选样：TTM 股息率降序 Top 100
组合（池内，等权，月末换仓，前瞻分红加回，换手×0.3% 成本敏感性）：
  - P_pool      全池等权（中证红利近似基准）
  - PROD-N      池内 rc_prod(size-1 + amihud) Top N，N=10/20/30
  - PROD+DY-N   池内 rc_prod + dy_rank Top N
  - DY-N        池内 dy Top N（对照）
"""
from pathlib import Path
import pandas as pd
import numpy as np
import duckdb

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports/dividend_facts"
con = duckdb.connect()
K = "read_parquet('data/lake/clean/mirror/kline_daily.parquet')"
DE = "read_parquet('data/lake/clean/mirror/dividend_events.parquet')"

kline = con.execute(f"SELECT code, date, close FROM {K} WHERE close>0 ORDER BY code, date").df()
kline["date"] = pd.to_datetime(kline["date"])
cash = con.execute(f"SELECT code, date, fenhong FROM {DE} WHERE fenhong>0 AND songzhuangu=0 AND peigu=0").df()
cash["date"] = pd.to_datetime(cash["date"])
cash["yr"] = cash["date"].dt.year
div_years = cash.groupby("code")["yr"].apply(set)

def load_factor(name):
    df = con.execute(f"SELECT date, code, value FROM read_parquet('data/lake/factor/{name}/part-*.parquet')").df()
    df["date"] = pd.to_datetime(df["date"])
    return df

size, amihud = load_factor("size"), load_factor("amihud_20")

month_end = kline.groupby(kline["date"].dt.to_period("M"))["date"].max()
me = kline[kline["date"].isin(month_end)].sort_values(["code", "date"]).copy()
me["fwd_close"] = me.groupby("code")["close"].shift(-1)
me = me.dropna(subset=["fwd_close"])
me = me[me["close"] >= 2.0]

# TTM 分红 + 前瞻窗口分红加回
sd = me[["code", "date"]].drop_duplicates().sort_values(["code", "date"])
sd["next_snap"] = sd.groupby("code")["date"].shift(-1)
ev = cash.drop(columns="yr").merge(sd, left_on="code", right_on="code").rename(
    columns={"date_x": "ex_date", "date_y": "snap"})
ev_ttm = ev[(ev["ex_date"] > ev["snap"] - pd.Timedelta(days=365)) & (ev["ex_date"] <= ev["snap"])]
ttm = ev_ttm.groupby(["code", "snap"], as_index=False)["fenhong"].sum().rename(columns={"fenhong": "ttm_div", "snap": "date"})
ev_fwd = ev[(ev["ex_date"] > ev["snap"]) & (ev["ex_date"] <= ev["next_snap"])]
fwd = ev_fwd.groupby(["code", "snap"], as_index=False)["fenhong"].sum().rename(columns={"fenhong": "fwd_div", "snap": "date"})
me = me.merge(ttm, on=["code", "date"], how="left")
me["ttm_div"] = me["ttm_div"].fillna(0.0)
me["dy"] = me["ttm_div"] / me["close"]
me = me.merge(fwd, on=["code", "date"], how="left")
me["fwd_div"] = me["fwd_div"].fillna(0.0)
me["fwd_ret"] = (me["fwd_close"] + me["fwd_div"]) / me["close"] - 1

for f, nm in [(size, "size"), (amihud, "amihud")]:
    sub = f[f["date"].isin(month_end)][["date", "code", "value"]].rename(columns={"value": nm})
    me = me.merge(sub, on=["date", "code"], how="inner")
me["size_r"] = me.groupby("date")["size"].rank()
me["ami_r"] = me.groupby("date")["amihud"].rank()
me["dy_r"] = me.groupby("date")["dy"].rank()
me["rc_prod"] = 0.5 * (1 - me["size_r"]) + 0.5 * me["ami_r"]
me["rc_dy"] = (1 - me["size_r"] + me["ami_r"] + me["dy_r"]) / 3
me["yr"] = me["date"].dt.year

# 红利池：过去 3 个完整日历年每年有现金分红 & TTM dy 降序 Top100
def build_pool(g):
    y = g["yr"].iloc[0]
    need = {y - 1, y - 2, y - 3}
    ok = [c for c in g["code"].unique() if need.issubset(div_years.get(c, set()))]
    gg = g[g["code"].isin(ok) & (g["dy"] > 0)]
    return gg.nlargest(100, "dy")

pool_parts = []
for d, g in me.groupby("date"):
    p = build_pool(g)
    if len(p):
        pool_parts.append(p.assign(date=d))
pool = pd.concat(pool_parts)
pool_sizes = pool.groupby("date").size()
print(f"红利池: {len(pool)} 行, 池均 {pool_sizes.mean():.0f} 只, 月份 {pool_sizes.shape[0]}")

# 与官方当前成分重叠校验（最新月）
try:
    cons = con.execute("SELECT stock_code FROM read_parquet('data/lake/clean/index_members_ext/dividend_cons.parquet') WHERE index_code='000922'").df()["stock_code"].tolist()
    last = pool["date"].max()
    pset = set(pool[pool["date"] == last]["code"])
    oset = {c.lstrip("0") if len(c) == 6 and c[0] == "0" else c for c in ()}  # noop
    overlap = len(pset & set(cons))
    print(f"重叠校验: 近似池({last.date()}) vs 官方当前成分: {overlap}/100")
except Exception as e:
    print("cons check fail:", str(e)[:80])

def run(score, topn, pool_df, cost=0.003):
    parts, tos, dates = [], [], sorted(pool_df["date"].unique())
    for d in dates:
        g = pool_df[pool_df["date"] == d].dropna(subset=[score])
        if len(g) < topn:
            continue
        parts.append(g.nlargest(topn, score).assign(date=d))
    if not parts:
        return None, None
    h = pd.concat(parts)
    ret = h.groupby("date")["fwd_ret"].mean()
    hd = {d: set(x) for d, x in h.groupby("date")["code"]}
    to = np.mean([1 - len(hd[a] & hd[b]) / len(hd[a] | hd[b]) for a, b in zip(dates[:-1], dates[1:]) if a in hd and b in hd])
    ret_cost = ret - to * cost
    return ret_cost, to

def stats(r):
    ann = (1 + r).prod() ** (12 / len(r)) - 1
    sharpe = r.mean() / r.std() * np.sqrt(12)
    dd = ((1 + r).cumprod() / (1 + r).cumprod().cummax() - 1).min()
    return ann * 100, sharpe, dd * 100

res, tos = {}, {}
base_ret = pool.groupby("date")["fwd_ret"].mean()
res["P_pool(基准)"] = base_ret
tos["P_pool(基准)"] = np.nan
for score, tag in [("rc_prod", "PROD"), ("rc_dy", "PROD+DY"), ("dy_r", "DY")]:
    for n in [10, 20, 30]:
        r, to = run(score, n, pool)
        res[f"{tag}-{n}"] = r
        tos[f"{tag}-{n}"] = to

print(f"\n{'组合':<14}{'年化%':>8}{'夏普':>7}{'回撤%':>8}{'月换手%':>9}  (已扣 0.3%×换手)")
for nm, r in res.items():
    if r is None or len(r) < 12:
        print(f"{nm:<14}  样本不足")
        continue
    a, s, d = stats(r)
    t = tos.get(nm, np.nan)
    print(f"{nm:<14}{a:>8.1f}{s:>7.2f}{d:>8.1f}{t*100 if t==t else 0:>9.1f}")

print("\n分年收益%（扣成本后）:")
yr = pd.DataFrame({nm: r.groupby(r.index.year).apply(lambda x: ((1 + x).prod() - 1) * 100)
                   for nm, r in res.items() if r is not None and len(r) >= 12})
print(yr.round(1).to_string())
yr.to_csv(OUT / "c2b_pool_yearly.csv")

# 相位扫描：PROD-10 vs 池基准，月内不同交易日截面（第 k 个交易日，k=-1 月末 / 5 / 10）
print("\n相位扫描（池内 Top10 vs 池基准，月内截面相位）:")

def run_phase(kth):
    """kth: 月内第几个交易日（-1=月末）。返回 (池基准, PROD-10) 扣成本月收益序列"""
    tk = pd.Series(sorted(kline["date"].unique()))
    mkey = pd.to_datetime(tk).dt.to_period("M")
    if kth == -1:
        snap = tk.groupby(mkey).max()
    else:
        snap = tk.groupby(mkey).nth(kth - 1) if kth else tk.groupby(mkey).max()
    snap_dates = set(snap.dropna())
    ms = kline[kline["date"].isin(snap_dates)].sort_values(["code", "date"]).copy()
    ms["fwd_close"] = ms.groupby("code")["close"].shift(-1)
    ms = ms.dropna(subset=["fwd_close"])
    ms = ms[ms["close"] >= 2.0]
    sdx = ms[["code", "date"]].drop_duplicates().sort_values(["code", "date"])
    sdx["next_snap"] = sdx.groupby("code")["date"].shift(-1)
    evx = cash.drop(columns="yr").merge(sdx, left_on="code", right_on="code").rename(
        columns={"date_x": "ex_date", "date_y": "snap"})
    t = evx[(evx["ex_date"] > evx["snap"] - pd.Timedelta(days=365)) & (evx["ex_date"] <= evx["snap"])]
    ttmx = t.groupby(["code", "snap"], as_index=False)["fenhong"].sum().rename(columns={"fenhong": "ttm_div", "snap": "date"})
    f = evx[(evx["ex_date"] > evx["snap"]) & (evx["ex_date"] <= evx["next_snap"])]
    fwdx = f.groupby(["code", "snap"], as_index=False)["fenhong"].sum().rename(columns={"fenhong": "fwd_div", "snap": "date"})
    ms = ms.merge(ttmx, on=["code", "date"], how="left")
    ms["ttm_div"] = ms["ttm_div"].fillna(0.0)
    ms["dy"] = ms["ttm_div"] / ms["close"]
    ms = ms.merge(fwdx, on=["code", "date"], how="left")
    ms["fwd_div"] = ms["fwd_div"].fillna(0.0)
    ms["fwd_ret"] = (ms["fwd_close"] + ms["fwd_div"]) / ms["close"] - 1
    for fac, nm in [(size, "size"), (amihud, "amihud")]:
        sub = fac[fac["date"].isin(snap_dates)][["date", "code", "value"]].rename(columns={"value": nm})
        ms = ms.merge(sub, on=["date", "code"], how="inner")
    ms["size_r"] = ms.groupby("date")["size"].rank()
    ms["ami_r"] = ms.groupby("date")["amihud"].rank()
    ms["rc_prod"] = 0.5 * (1 - ms["size_r"]) + 0.5 * ms["ami_r"]
    ms["yr"] = ms["date"].dt.year
    parts = []
    for d, g in ms.groupby("date"):
        y = g["yr"].iloc[0]
        need = {y - 1, y - 2, y - 3}
        ok = [c for c in g["code"].unique() if need.issubset(div_years.get(c, set()))]
        gg = g[g["code"].isin(ok) & (g["dy"] > 0)].nlargest(100, "dy")
        if len(gg) >= 10:
            parts.append(gg.assign(date=d))
    pl = pd.concat(parts)
    base = pl.groupby("date")["fwd_ret"].mean()
    parts2 = []
    for d, g in pl.groupby("date"):
        parts2.append(g.nlargest(10, "rc_prod").assign(date=d))
    h = pd.concat(parts2)
    top10 = h.groupby("date")["fwd_ret"].mean()
    hd = {d: set(x) for d, x in h.groupby("date")["code"]}
    dates = sorted(hd)
    to = np.mean([1 - len(hd[a] & hd[b]) / len(hd[a] | hd[b]) for a, b in zip(dates[:-1], dates[1:])])
    return base, top10 - to * 0.003

for kth in [-1, 5, 10]:
    base, top10 = run_phase(kth)
    if len(top10) < 12:
        continue
    ab = (1 + base).prod() ** (12 / len(base)) - 1
    at = (1 + top10).prod() ** (12 / len(top10)) - 1
    sb = base.mean() / base.std() * np.sqrt(12)
    st = top10.mean() / top10.std() * np.sqrt(12)
    print(f"  第{kth:>2}交易日截面: 池基准 {ab*100:5.1f}% (夏普{sb:.2f})  PROD-10 {at*100:5.1f}% (夏普{st:.2f})  Δ{(at-ab)*100:+.1f}pp  Δ夏普{st-sb:+.2f}")
print("DONE")
