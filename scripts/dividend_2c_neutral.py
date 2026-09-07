# -*- coding: utf-8 -*-
"""红利策略 2C 第一轮：股息率正交化检验 + 组合级增量
- 截面 rank corr(dy, size/amihud)
- dy 原始 IC vs dy⊥size 残差 IC（月度截面 rank 回归取残差）
- 组合对比（月末等权 Top50，月度换仓，窗口内分红加回）：
    P_PROD   = rank(-size) + rank(amihud_20)   （生产口径，size 方向 -1）
    P_PROD_DY= 三者 rank 等权
    P_DY     = 股息率单因子
- 分年收益/夏普/回撤/换手
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

kline = con.execute(f"SELECT code, date, close FROM {K} WHERE close > 0 ORDER BY code, date").df()
kline["date"] = pd.to_datetime(kline["date"])
cash = con.execute(f"SELECT code, date, fenhong FROM {DE} WHERE fenhong > 0 AND songzhuangu=0 AND peigu=0").df()
cash["date"] = pd.to_datetime(cash["date"])

def load_factor(name):
    df = con.execute(f"SELECT date, code, value FROM read_parquet('data/lake/factor/{name}/part-*.parquet')").df()
    df["date"] = pd.to_datetime(df["date"])
    return df

size = load_factor("size")
amihud = load_factor("amihud_20")

# 月末截面
month_end = kline.groupby(kline["date"].dt.to_period("M"))["date"].max()
me = kline[kline["date"].isin(month_end)].sort_values(["code", "date"]).copy()
me["fwd_close"] = me.groupby("code")["close"].shift(-1)
me = me.dropna(subset=["fwd_close"])
me = me[me["close"] >= 2.0]

# 窗口内分红加回：ex_date ∈ (t, t_next]，加 fenhong/close_t
snap_dates = me[["code", "date"]].drop_duplicates()
ev = cash.merge(snap_dates, left_on="code", right_on="code")
ev = ev.rename(columns={"date_x": "ex_date", "date_y": "snap"})
ev["next_snap"] = ev.groupby("code")["snap"].shift(-1)
w = ev[(ev["ex_date"] > ev["snap"]) & (ev["ex_date"] <= ev["next_snap"])]
fwd_div = w.groupby(["code", "snap"], as_index=False)["fenhong"].sum().rename(columns={"fenhong": "fwd_div"})
me = me.merge(fwd_div, left_on=["code", "date"], right_on=["code", "snap"], how="left")
me["fwd_div"] = me["fwd_div"].fillna(0.0)
me = me.drop(columns=["snap"], errors="ignore")

# TTM 股息率
ev2 = cash.merge(snap_dates, left_on="code", right_on="code").rename(columns={"date_x": "ex_date", "date_y": "snap"})
ev2 = ev2[(ev2["ex_date"] > ev2["snap"] - pd.Timedelta(days=365)) & (ev2["ex_date"] <= ev2["snap"])]
ttm = ev2.groupby(["code", "snap"], as_index=False)["fenhong"].sum().rename(columns={"fenhong": "ttm_div", "snap": "date"})
me = me.merge(ttm, on=["code", "date"], how="left")
me["ttm_div"] = me["ttm_div"].fillna(0.0)
me["dy"] = me["ttm_div"] / me["close"]

# 因子对齐到月末
for f, nm in [(size, "size"), (amihud, "amihud")]:
    sub = f[f["date"].isin(month_end)][["date", "code", "value"]].rename(columns={"value": nm})
    me = me.merge(sub, on=["date", "code"], how="inner")
print(f"截面: {me['date'].nunique()} 个月, 每月均值 {me.groupby('date').size().mean():.0f} 只")

# ---------- 截面相关 ----------
me["dy_r"] = me.groupby("date")["dy"].rank()
me["size_r"] = me.groupby("date")["size"].rank()
me["ami_r"] = me.groupby("date")["amihud"].rank()
corr_s = me.groupby("date").apply(lambda g: g["dy_r"].corr(g["size_r"]), include_groups=False)
corr_a = me.groupby("date").apply(lambda g: g["dy_r"].corr(g["ami_r"]), include_groups=False)
print(f"\nrank corr(dy, size): mean={corr_s.mean():.3f} (分年: {corr_s.groupby(corr_s.index.year).mean().round(2).to_dict()})")
print(f"rank corr(dy, amihud): mean={corr_a.mean():.3f}")

# ---------- IC: 原始 vs 中性化 ----------
me["fwd_ret"] = (me["fwd_close"] + me["fwd_div"]) / me["close"] - 1
def ic_of(col):
    return me.groupby("date").apply(lambda g: g[col].rank().corr(g["fwd_ret"].rank()) if len(g) > 100 else np.nan, include_groups=False).dropna()

def neutralize(g, col, by="size_r"):
    x = g[by].values
    y = g[col].values
    if len(g) < 50:
        return pd.Series(np.nan, index=g.index)
    beta = np.polyfit(x, y, 1)
    return pd.Series(y - beta[0] * x - beta[1], index=g.index)

me["dy_resid"] = me.groupby("date", group_keys=False).apply(lambda g: neutralize(g, "dy_r"), include_groups=False)
ic_raw = ic_of("dy_r")
ic_res = ic_of("dy_resid")
for nm, ic in [("原始 dy", ic_raw), ("dy⊥size 残差", ic_res)]:
    print(f"{nm}: IC={ic.mean():.4f}  t={ic.mean()/ic.std()*np.sqrt(len(ic)):.1f}  "
          f"分年: {ic.groupby(ic.index.year).mean().round(3).to_dict()}")

# ---------- 组合对比 ----------
def portfolio(score_col, asc=False, top=50):
    """月末按 score 选 top，等权，次月持有"""
    parts = []
    for d, g in me.groupby("date"):
        g = g.dropna(subset=[score_col])
        if len(g) < 100:
            continue
        sel = g.nlargest(top, score_col) if not asc else g.nsmallest(top, score_col)
        parts.append(sel.assign(date=d))
    hold = pd.concat(parts)
    ret = hold.groupby("date")["fwd_ret"].mean()
    # 换手率：持仓集合月度重合度
    tos = []
    dates = sorted(hold["date"].unique())
    for a, b in zip(dates[:-1], dates[1:]):
        sa = set(hold[hold["date"] == a]["code"])
        sb = set(hold[hold["date"] == b]["code"])
        tos.append(1 - len(sa & sb) / len(sa | sb))
    return ret, np.mean(tos)

rc = me.groupby("date", group_keys=False).apply(
    lambda g: pd.Series(0.5 * (1 - g["size_r"]) + 0.5 * g["ami_r"], index=g.index), include_groups=False)
me["rc_prod"] = rc
me["rc_dy"] = me.groupby("date", group_keys=False).apply(
    lambda g: pd.Series((1 - g["size_r"] + g["ami_r"] + g["dy_r"]) / 3, index=g.index), include_groups=False)

rets, tos = {}, {}
rets["P_PROD(size+amihud)"], tos["P_PROD(size+amihud)"] = portfolio("rc_prod")
rets["P_PROD+DY"], tos["P_PROD+DY"] = portfolio("rc_dy")
rets["P_DY单因子"], tos["P_DY单因子"] = portfolio("dy_r")

print("\n组合对比（月末等权 Top50，月度换仓，分红加回，未扣成本）:")
print(f"{'组合':<22}{'年化%':>8}{'夏普':>7}{'回撤%':>8}{'胜率%':>7}{'月换手%':>8}")
rows = {}
for nm, r in rets.items():
    ann = (1 + r).prod() ** (12 / len(r)) - 1
    sharpe = r.mean() / r.std() * np.sqrt(12)
    dd = ((1 + r).cumprod() / (1 + r).cumprod().cummax() - 1).min()
    rows[nm] = dict(ann=ann * 100, sharpe=sharpe, dd=dd * 100, win=(r > 0).mean() * 100, to=tos[nm] * 100)
    print(f"{nm:<22}{ann*100:>8.1f}{sharpe:>7.2f}{dd*100:>8.1f}{(r>0).mean()*100:>7.0f}{tos[nm]*100:>8.1f}")

print("\n分年收益%:")
yr = pd.DataFrame({nm: r.groupby(r.index.year).apply(lambda x: ((1+x).prod()-1)*100) for nm, r in rets.items()})
print(yr.round(1).to_string())
yr.to_csv(OUT / "c2_portfolio_yearly.csv")
pd.DataFrame(rows).T.to_csv(OUT / "c2_portfolio_stats.csv")
me[["date", "code", "dy", "size", "amihud", "dy_resid", "fwd_ret"]].to_parquet(OUT / "c2_cross_section.parquet", index=False)
print("\nDONE")
