# -*- coding: utf-8 -*-
"""红利策略 Phase 1：基础事实研究
1. TTM 股息率分层 IC + 分位组合（2022-2026，月频，含窗口内分红加回）
2. 除息日事件 CAR(-20,+20)（市场调整，按股息率分位/年份分层）+ 填权率
3. 预案公告日漂移 CAR(0,+20)
4. 红利风格周期（指数层面 2013-2026：RS vs 十债、pe_ttm 分位）
5. 分红日历（登记日月份分布）
输出：reports/dividend_facts/*.csv + 控制台摘要
"""
from pathlib import Path
import pandas as pd
import numpy as np
import duckdb

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports/dividend_facts"
OUT.mkdir(parents=True, exist_ok=True)

con = duckdb.connect()
K = "read_parquet('data/lake/clean/mirror/kline_daily.parquet')"
DE = "read_parquet('data/lake/clean/mirror/dividend_events.parquet')"
IDX = "read_parquet('data/lake/clean/mirror/index_kline.parquet')"

kline = con.execute(f"SELECT code, date, close FROM {K} WHERE close > 0 ORDER BY code, date").df()
kline["date"] = pd.to_datetime(kline["date"])
cash = con.execute(f"SELECT code, date, fenhong FROM {DE} WHERE fenhong > 0 AND songzhuangu = 0 AND peigu = 0").df()
cash["date"] = pd.to_datetime(cash["date"])
idx300 = con.execute(f"SELECT date, close FROM {IDX} WHERE code = '000300.SH' ORDER BY date").df()
idx300["date"] = pd.to_datetime(idx300["date"])
print(f"kline {kline['code'].nunique()} codes, {len(kline)} rows; cash events {len(cash)}")

# ---------- 1. TTM 股息率分层 ----------
month_end = kline.groupby(kline["date"].dt.to_period("M"))["date"].max()
month_end = month_end[month_end >= pd.Timestamp("2022-06-30")]
me = kline[kline["date"].isin(month_end)].copy()          # 月末截面
me["ret21"] = me.groupby("code")["close"].shift(-1)  # 占位
# 前瞻 21 交易日收益：用下一个月末近似（月频调仓口径，与因子框架一致）
me = me.sort_values(["code", "date"])
me["fwd_close"] = me.groupby("code")["close"].shift(-1)
me = me.dropna(subset=["fwd_close"])
# TTM 分红：窗口 (t-365d, t]
ev = cash.merge(me[["code", "date"]].drop_duplicates(), on="code")
ev = ev.rename(columns={"date_x": "ex_date", "date_y": "snap_date"})
ev = ev[(ev["ex_date"] > ev["snap_date"] - pd.Timedelta(days=365)) & (ev["ex_date"] <= ev["snap_date"])]
ttm = ev.groupby(["code", "snap_date"], as_index=False)["fenhong"].sum().rename(
    columns={"fenhong": "ttm_div", "snap_date": "date"})
me = me.merge(ttm, on=["code", "date"], how="left")
me["ttm_div"] = me["ttm_div"].fillna(0.0)
me["dy"] = me["ttm_div"] / me["close"]
me = me[me["close"] >= 2.0].copy()
# 窗口内分红加回（简化：加回比例 = fenhong / snap0 close，分层排序不变）
fwd_div = ev.merge(me[["code", "date"]], left_on=["code", "snap_date"], right_on=["code", "date"], how="inner")
fwd_div = fwd_div.rename(columns={"date": "snap0"})
fwd_sum = fwd_div.groupby(["code", "snap0"], as_index=False)["fenhong"].sum().rename(columns={"fenhong": "fwd_div"})
me = me.merge(fwd_sum, left_on=["code", "date"], right_on=["code", "snap0"], how="left")
me["fwd_div"] = me["fwd_div"].fillna(0.0)
me["fwd_ret"] = (me["fwd_close"] + me["fwd_div"]) / me["close"] - 1
me = me.dropna(subset=["fwd_ret"])

def monthly_ic(g):
    if len(g) < 100:
        return np.nan
    return g["dy"].rank().corr(g["fwd_ret"].rank())

ic = me.groupby("date").apply(monthly_ic, include_groups=False).dropna()
ic_stats = dict(n_months=len(ic), ic_mean=ic.mean(), ic_std=ic.std(),
                icir=ic.mean() / ic.std() * np.sqrt(12), t_stat=ic.mean() / ic.std() * np.sqrt(len(ic)),
                ic_pos_rate=(ic > 0).mean())
me["bucket"] = me.groupby("date")["dy"].transform(lambda x: pd.qcut(x.rank(method="first"), 5, labels=False)) + 1
buckets = me.groupby(["date", "bucket"])["fwd_ret"].mean().unstack()
bucket_mean = buckets.mean() * 100
ann = (1 + buckets.mean()) ** 12 - 1
print("\n[1] TTM 股息率月频（等权，21 交易日前瞻，分红加回）")
print(f"    IC mean={ic_stats['ic_mean']:.4f}  ICIR(年化)={ic_stats['icir']:.2f}  t={ic_stats['t_stat']:.1f}  IC>0比例={ic_stats['ic_pos_rate']:.0%}  N={ic_stats['n_months']}")
print(f"    Q1(无分红)~Q5(高股息) 月均收益%: {bucket_mean.round(2).to_dict()}")
print(f"    年化(复利口径)%: {ann.mul(100).round(1).to_dict()}")
print(f"    Q5-Q1 月均价差: {(bucket_mean[5]-bucket_mean[1]):.2f}pp")
buckets.to_csv(OUT / "dy_buckets_monthly.csv")
ic.to_frame("ic").to_csv(OUT / "dy_monthly_ic.csv")

# 分年度 IC
ic_y = ic.groupby(ic.index.year).agg(["mean", "count"])
print("    分年 IC:", {int(y): round(m, 3) for y, m in zip(ic_y.index, ic_y["mean"])})

# ---------- 2. 除息日事件 CAR ----------
print("\n[2] 除息日事件研究（市场调整 CAR，%)")
kd = kline.sort_values(["code", "date"]).copy()
kd["ret"] = kd.groupby("code")["close"].pct_change()
mret = idx300.set_index("date")["close"].pct_change()
kd["mret"] = kd["date"].map(mret).fillna(0.0)
kd["ar"] = kd["ret"] - kd["mret"]
groups = {c: g for c, g in kd.groupby("code")}
LO, HI = -20, 20
HCOLS = [f"d{h}" for h in range(LO, HI + 1)]

def event_car(events):
    """events: DataFrame[code, date] -> DataFrame CAR 列 + ex_close/prev_close"""
    out = pd.DataFrame(np.nan, index=events.index, columns=HCOLS)
    ex_close = pd.Series(np.nan, index=events.index)
    prev_close = pd.Series(np.nan, index=events.index)
    for i, (code, exd) in enumerate(zip(events["code"], events["date"])):
        g = groups.get(code)
        if g is None:
            continue
        dates = g["date"].values
        pos = dates.searchsorted(np.datetime64(exd))
        if pos < abs(LO) or pos + HI + 1 > len(g):
            continue
        seg = g["ar"].values[pos + LO: pos + HI + 1]
        out.iloc[i] = np.cumsum(np.nan_to_num(seg))
        v = g["close"].values
        ex_close.iloc[i] = v[pos]
        prev_close.iloc[i] = v[pos - 1]
    return out.assign(ex_close=ex_close, prev_close=prev_close)

ev2 = cash[(cash["date"] >= pd.Timestamp("2022-02-01")) & (cash["code"].isin(groups))].copy()
car = event_car(ev2)
valid = car["ex_close"].notna()
print(f"    有效事件 n={valid.sum()}")
summary = {h: car.loc[valid, f"d{h}"].mean() * 100 for h in [-20, -10, -5, -1, 0, 1, 5, 10, 20]}
print("    CAR:", {k: round(v, 3) for k, v in summary.items()})
sample = car[valid]
fill10 = (sample["d10"].notna())
# 填权率：+10 交易日收盘 >= 除息前收盘（用 CAR 无法直接算，改用价格）
def fill_rate(events_df, horizon=10):
    ok = hit = 0
    for code, exd in zip(events_df["code"], events_df["date"]):
        g = groups.get(code)
        if g is None:
            continue
        pos = g["date"].values.searchsorted(np.datetime64(exd))
        if pos < 1 or pos + horizon >= len(g):
            continue
        ok += 1
        if g["close"].values[pos + horizon] >= g["close"].values[pos - 1]:
            hit += 1
    return hit / ok if ok else np.nan, ok

fr, n_ok = fill_rate(ev2[valid].reset_index(), 10)
print(f"    填权率(+10交易日回到除息前收盘): {fr:.1%} (n={n_ok})")
ev2v = ev2[valid].copy()
for c in ["d0", "d5", "d20"]:
    ev2v[c] = sample[c].values
ev2v["yld"] = ev2v["fenhong"] / sample["ex_close"].values
ev2v["ybucket"] = pd.qcut(ev2v["yld"].rank(method="first"), 3, labels=[1, 2, 3])
by_y = ev2v.groupby("ybucket")[["d0", "d5", "d20"]].mean().mul(100).round(3)
by_year = ev2v.groupby(ev2v["date"].dt.year)[["d0", "d5", "d20"]].mean().mul(100).round(3)
print("    按股息率三分位 CAR(d0/d5/d20)%:")
print(by_y.to_string())
print("    分年 CAR(d0/d5/d20)%:")
print(by_year.to_string())
car[valid].to_csv(OUT / "exdate_car_paths.csv")
by_y.to_csv(OUT / "exdate_car_by_yield.csv")
by_year.to_csv(OUT / "exdate_car_by_year.csv")

# ---------- 3. 预案公告日漂移 ----------
try:
    fh = con.execute("SELECT code, announce_date, ex_date, cash_per10 FROM read_parquet('data/lake/clean/dividend_announce/part-*.parquet') WHERE announce_date IS NOT NULL AND cash_per10 > 0").df()
    fh["announce_date"] = pd.to_datetime(fh["announce_date"])
    fh = fh[fh["announce_date"] >= pd.Timestamp("2022-01-01")].drop_duplicates(["code", "announce_date"])
    fh = fh[fh["code"].isin(groups)]
    car3 = event_car(fh.rename(columns={"announce_date": "date"})[["code", "date"]])
    v3 = car3["ex_close"].notna()
    print(f"\n[3] 预案公告日漂移（市场调整%，n={v3.sum()}）:",
          {h: round(car3.loc[v3, f"d{h}"].mean() * 100, 3) for h in [0, 1, 5, 10, 20]})
    car3[v3].to_csv(OUT / "announce_car_paths.csv")
except Exception as e:
    print("[3] FAIL:", str(e)[:120])

# ---------- 4. 红利风格周期（2013-2026 指数层面） ----------
idx_ext = con.execute("SELECT code, date, close, pe_ttm FROM read_parquet('data/lake/clean/index_kline_ext/part-*.parquet')").df()
idx_ext["date"] = pd.to_datetime(idx_ext["date"])
piv = idx_ext.pivot_table(index="date", columns="code", values="close")
piv = piv.dropna()
rs = (piv["000922"] / piv["000300.SH"] if "000300.SH" in piv.columns else None)
if rs is None:
    base = con.execute(f"SELECT date, close FROM {IDX} WHERE code='000300.SH'").df()
    base["date"] = pd.to_datetime(base["date"])
    base = base.set_index("date")["close"]
    rs = piv["000922"] / base.reindex(piv.index)
rs_m = rs.resample("ME").last()
rs_ret = rs_m.pct_change().dropna()
bond = con.execute("SELECT date, cgb_10y FROM read_parquet('data/lake/clean/bond_yield/cgb.parquet')").df()
bond["date"] = pd.to_datetime(bond["date"])
b_m = bond.set_index("date")["cgb_10y"].resample("ME").last()
b_chg = b_m.diff().dropna()
al = pd.concat([rs_ret.rename("rs"), b_m.reindex(rs_ret.index).rename("y10"), b_chg.reindex(rs_ret.index).rename("dy10")], axis=1).dropna()
print("\n[4] 红利风格周期（月频 2013-2026, n=%d）" % len(al))
print(f"    corr(RS月收益, 十债水平)={al['rs'].corr(al['y10']):.3f}   corr(RS月收益, 十债变动)={al['rs'].corr(al['dy10']):.3f}")
pe = idx_ext[idx_ext["code"] == "000922"].set_index("date")["pe_ttm"].resample("ME").last()
pe_pct = pe.rolling(60, min_periods=24).apply(lambda x: (x[-1] <= x).mean(), raw=True)
fwd6 = rs_m.shift(-6) / rs_m - 1
al2 = pd.concat([pe_pct.rename("pe_pct"), fwd6.rename("fwd_rs_6m")], axis=1).dropna()
q = pd.qcut(al2["pe_pct"], 3, labels=["低", "中", "高"])
print("    000922 PE分位 三分位 → 未来6个月 RS 收益%:")
print((al2.groupby(q, observed=True)["fwd_rs_6m"].mean() * 100).round(2).to_dict())
season = rs_ret.groupby(rs_ret.index.month).mean() * 100
print("    RS 月度季节性%（1-12月）:", {int(m): round(v, 2) for m, v in season.items()})
al.to_csv(OUT / "style_cycle_monthly.csv")

# ---------- 5. 分红日历 ----------
fh2 = con.execute("SELECT record_date FROM read_parquet('data/lake/clean/dividend_announce/part-*.parquet') WHERE record_date IS NOT NULL AND cash_per10 > 0").df()
fh2["m"] = pd.to_datetime(fh2["record_date"]).dt.month
cal = fh2["m"].value_counts(normalize=True).sort_index() * 100
print("\n[5] 股权登记日月份分布%（2021-2025）:")
print({int(m): round(v, 1) for m, v in cal.items()})
cal.to_frame("pct").to_csv(OUT / "dividend_calendar.csv")
print("\nDONE ->", OUT)
