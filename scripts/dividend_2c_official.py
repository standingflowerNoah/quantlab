# -*- coding: utf-8 -*-
"""红利 2C 第二轮复验：官方编制规则复现池（年调+缓冲区）vs 近似池
官方规则（中证红利编制方案 2022 修订版）：
  - 样本空间：中证全指内市值/成交额前 80%（无历史股本→仅成交额近似，声明）
  - 条件：过去三个完整日历年连续现金分红；三年平均现金股息率
  - 选样：三年平均股息率 Top 100（分母用决策日现价近似，声明）
  - 定期调整：每年一次，12 月第二个星期五下一交易日生效 → 成分用于 (Y-11月末, Y+1-11月末] 截面
  - 缓冲区：原样本过去一年股息率>0.5% 且成交额前 90% → 保留；余额按排名补足
  - 跳过：股利支付率 (0,1) 筛选（无历史股本，声明）
校准：Y=2025 决策成分 vs 官方当前成分（2025-12 调样生效）重叠率
回测：官方规则池内 PROD Top N + 相位扫描；2022 年缺失期由近似池（dividend_2c_pool.py）覆盖
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

kline = con.execute(f"SELECT code, date, close, amount FROM {K} WHERE close>0 ORDER BY code, date").df()
kline["date"] = pd.to_datetime(kline["date"])
cash = con.execute(f"SELECT code, date, fenhong FROM {DE} WHERE fenhong>0 AND songzhuangu=0 AND peigu=0").df()
cash["date"] = pd.to_datetime(cash["date"])
cash["yr"] = cash["date"].dt.year
# 每股年度分红（除息日年度归集）
div_by_year = cash.groupby(["code", "yr"])["fenhong"].sum()
div_years = cash.groupby("code")["yr"].apply(set)

def load_factor(name):
    df = con.execute(f"SELECT date, code, value FROM read_parquet('data/lake/factor/{name}/part-*.parquet')").df()
    df["date"] = pd.to_datetime(df["date"])
    return df

size, amihud = load_factor("size"), load_factor("amihud_20")

month_end = kline.groupby(kline["date"].dt.to_period("M"))["date"].max()

# ---------- 官方规则成分：年度决策 ----------
# 决策日 t = Y 年 11 月最后一个交易日；成分用于 (Y-11末, Y+1-11末] 的月末截面
decision_years = [2022, 2023, 2024, 2025]
official = {}  # year -> set(codes)
for Y in decision_years:
    t = month_end[month_end.index.astype(str).str.startswith(f"{Y}-11")]
    t = t.iloc[-1]
    snap = kline[kline["date"] == t].set_index("code").copy()  # index=code
    # 过去一年日均成交额（截至 t 的 250 个交易日）→ 流动性排名
    kd = kline[kline["date"] <= t].groupby("code").tail(250)
    amt = kd.groupby("code")["amount"].mean().rename("amt_avg")
    snap = snap.drop(columns=["amt_avg", "amt_r"], errors="ignore").join(amt, how="left")
    snap["amt_r"] = snap["amt_avg"].rank(ascending=False, pct=True)
    # 三年连续分红 + 三年平均股息率（现价分母近似）
    need = {Y - 1, Y - 2, Y - 3}
    snap["div3"] = [np.mean([div_by_year.get((c, y), 0.0) for y in need]) for c in snap.index]
    snap["cont3"] = [need.issubset(div_years.get(c, set())) for c in snap.index]
    # 过去一年股息率（缓冲区用）
    d1 = cash[(cash["date"] > t - pd.Timedelta(days=365)) & (cash["date"] <= t)]
    div1y = d1.groupby("code")["fenhong"].sum().rename("div_1y")
    snap = snap.join(div1y, how="left")
    snap["div_1y"] = snap["div_1y"].fillna(0.0)
    snap["dy1"] = snap["div_1y"] / snap["close"]
    # 样本空间：连续三年分红 + 成交额前 80%（市值筛选缺数据，声明跳过）
    elig = snap[snap["cont3"] & (snap["amt_r"] <= 0.80) & (snap["close"] >= 2.0)].copy()
    elig["score"] = elig["div3"] / elig["close"]  # 三年平均每股分红 / 现价
    elig = elig.sort_values("score", ascending=False)
    prev = official.get(Y - 1, None)
    if prev:
        # 缓冲区：原样本 dy1>0.5% 且成交额前 90% → 保留
        keep = [c for c in prev
                if c in snap.index and snap.loc[c, "dy1"] > 0.005 and snap.loc[c, "amt_r"] <= 0.90]
        n_fill = 100 - len(keep)
        new_add = [c for c in elig.index if c not in keep][:max(n_fill, 0)]
        official[Y] = set(keep + new_add)
    else:
        official[Y] = set(elig.head(100).index)
    print(f"决策 {Y}-11-30: 连续分红+流动性合格 {len(elig)} 只, 官方池 {len(official[Y])} 只")

# 校准：2025 决策 vs 官方当前成分
try:
    cons = con.execute("SELECT stock_code FROM read_parquet('data/lake/clean/index_members_ext/dividend_cons.parquet') WHERE index_code='000922'").df()["stock_code"].tolist()
    norm = lambda s: "".join(ch for ch in str(s) if ch.isdigit())[-6:]
    cons = {norm(c) for c in cons}
    ov = len({norm(c) for c in official[2025]} & cons)
    print(f"样本 code 示例: {sorted(official[2025])[:3]}  官方成分示例: {sorted(cons)[:3]}")
    print(f"校准: 官方规则池(2025决策) vs 中证官网当前成分: {ov}/100")
except Exception as e:
    print("cons check fail:", str(e)[:80])

# ---------- 展开成月度截面池 ----------
me = kline[kline["date"].isin(month_end)].sort_values(["code", "date"]).copy()
me["fwd_close"] = me.groupby("code")["close"].shift(-1)
me = me.dropna(subset=["fwd_close"])
me = me[me["close"] >= 2.0]

sd = me[["code", "date"]].drop_duplicates().sort_values(["code", "date"])
sd["next_snap"] = sd.groupby("code")["date"].shift(-1)
ev = cash.drop(columns="yr").merge(sd, left_on="code", right_on="code").rename(
    columns={"date_x": "ex_date", "date_y": "snap"})
ev_fwd = ev[(ev["ex_date"] > ev["snap"]) & (ev["ex_date"] <= ev["next_snap"])]
fwd = ev_fwd.groupby(["code", "snap"], as_index=False)["fenhong"].sum().rename(columns={"fenhong": "fwd_div", "snap": "date"})
me = me.merge(fwd, on=["code", "date"], how="left")
me["fwd_div"] = me["fwd_div"].fillna(0.0)
me["fwd_ret"] = (me["fwd_close"] + me["fwd_div"]) / me["close"] - 1

for f, nm in [(size, "size"), (amihud, "amihud")]:
    sub = f[f["date"].isin(month_end)][["date", "code", "value"]].rename(columns={"value": nm})
    me = me.merge(sub, on=["date", "code"], how="inner")
me["size_r"] = me.groupby("date")["size"].rank()
me["ami_r"] = me.groupby("date")["amihud"].rank()
me["rc_prod"] = 0.5 * (1 - me["size_r"]) + 0.5 * me["ami_r"]

# 月末截面 → 所属决策年
def decision_year(d):
    y = d.year
    return y - 1 if d.month <= 11 else y

me["dec_y"] = me["date"].map(decision_year)
me["in_pool"] = [c in official.get(y, set()) for c, y in zip(me["code"], me["dec_y"])]
pool = me[me["in_pool"]].copy()
# 官方纯口径：2022-12 月末截面起（2022 决策成分），即 fwd_ret 从 2023-01 开始
first_valid = pd.Timestamp(f"{decision_years[0]}-12-31")
pool = pool[pool["date"] >= first_valid]
ps = pool.groupby("date").size()
print(f"官方规则池: {len(pool)} 行, 池均 {ps.mean():.0f} 只, 月份 {ps.shape[0]} ({pool['date'].min().date()} ~ {pool['date'].max().date()})")

# ---------- 回测 ----------
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
    return ret - to * cost, to

def stats(r):
    ann = (1 + r).prod() ** (12 / len(r)) - 1
    sharpe = r.mean() / r.std() * np.sqrt(12)
    dd = ((1 + r).cumprod() / (1 + r).cumprod().cummax() - 1).min()
    return ann * 100, sharpe, dd * 100

res, tos = {}, {}
res["P_pool(基准)"] = pool.groupby("date")["fwd_ret"].mean()
tos["P_pool(基准)"] = np.nan
for n in [10, 20, 30]:
    r, to = run("rc_prod", n, pool)
    res[f"PROD-{n}"] = r
    tos[f"PROD-{n}"] = to

print(f"\n官方规则池内组合（{'月频'}，已扣 0.3%×换手）:")
print(f"{'组合':<14}{'年化%':>8}{'夏普':>7}{'回撤%':>8}{'月换手%':>9}")
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
yr.to_csv(OUT / "c2_official_yearly.csv")

# ---------- 相位扫描：Top10 vs 池基准 ----------
print("\n相位扫描（官方池内 Top10 vs 池基准）:")
ab_all, at_all = [], []
for kth in [-1, 5, 10]:
    tk = pd.Series(sorted(kline["date"].unique()))
    mkey = pd.to_datetime(tk).dt.to_period("M")
    snap = tk.groupby(mkey).max() if kth == -1 else tk.groupby(mkey).nth(kth - 1)
    snap_dates = set(snap.dropna())
    ms = kline[kline["date"].isin(snap_dates)].sort_values(["code", "date"]).copy()
    ms["fwd_close"] = ms.groupby("code")["close"].shift(-1)
    ms = ms.dropna(subset=["fwd_close"])
    ms = ms[ms["close"] >= 2.0]
    sdx = ms[["code", "date"]].drop_duplicates().sort_values(["code", "date"])
    sdx["next_snap"] = sdx.groupby("code")["date"].shift(-1)
    evx = cash.drop(columns="yr").merge(sdx, left_on="code", right_on="code").rename(
        columns={"date_x": "ex_date", "date_y": "snap"})
    f = evx[(evx["ex_date"] > evx["snap"]) & (evx["ex_date"] <= evx["next_snap"])]
    fwdx = f.groupby(["code", "snap"], as_index=False)["fenhong"].sum().rename(columns={"fenhong": "fwd_div", "snap": "date"})
    ms = ms.merge(fwdx, on=["code", "date"], how="left")
    ms["fwd_div"] = ms["fwd_div"].fillna(0.0)
    ms["fwd_ret"] = (ms["fwd_close"] + ms["fwd_div"]) / ms["close"] - 1
    for fac, nm in [(size, "size"), (amihud, "amihud")]:
        sub = fac[fac["date"].isin(snap_dates)][["date", "code", "value"]].rename(columns={"value": nm})
        ms = ms.merge(sub, on=["date", "code"], how="inner")
    ms["size_r"] = ms.groupby("date")["size"].rank()
    ms["ami_r"] = ms.groupby("date")["amihud"].rank()
    ms["rc_prod"] = 0.5 * (1 - ms["size_r"]) + 0.5 * ms["ami_r"]
    ms["dec_y"] = ms["date"].map(decision_year)
    ms["in_pool"] = [c in official.get(y, set()) for c, y in zip(ms["code"], ms["dec_y"])]
    pl = ms[ms["in_pool"] & (ms["date"] >= first_valid)]
    base = pl.groupby("date")["fwd_ret"].mean()
    h = pd.concat([g.nlargest(10, "rc_prod") for _, g in pl.groupby("date")])
    top10 = h.groupby("date")["fwd_ret"].mean()
    hd = {d: set(x) for d, x in h.groupby("date")["code"]}
    dates = sorted(hd)
    to = np.mean([1 - len(hd[a] & hd[b]) / len(hd[a] | hd[b]) for a, b in zip(dates[:-1], dates[1:])])
    at = top10 - to * 0.003
    if len(at) < 12:
        continue
    ab, atv = stats(base), stats(at)
    ab_all.append(ab); at_all.append(atv)
    print(f"  第{kth:>2}交易日截面: 池基准 {ab[0]:5.1f}% (夏普{ab[1]:.2f})  PROD-10 {atv[0]:5.1f}% (夏普{atv[1]:.2f})  Δ{atv[0]-ab[0]:+.1f}pp  Δ夏普{atv[1]-ab[1]:+.2f}")

if ab_all:
    deltas = [a[0] - b[0] for a, b in zip(at_all, ab_all)]
    print(f"  Δ年化相位序列: {['%+.1f' % d for d in deltas]}  → {'全正 ✓ 过闸门二' if all(d > 0 for d in deltas) else '不全正 ✗'}")
print("DONE")
