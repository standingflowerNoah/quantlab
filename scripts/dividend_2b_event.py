# -*- coding: utf-8 -*-
"""红利 2B 第一轮：除息日事件套利（严格差异化红利税）
====================================================
税率（持股期限，按自然日，卖出时结算）：
  ≤30 天 20%；31~365 天 10%；>365 天 0%（税基=税前每股派息）
事件（纯现金分红，除权除息日 T，kline 不复权已实证）：
  S1 抢权  T-10→T-1    不吃息、无税——纯价格漂移捕获
  S2 短持  T-1→T+1     吃息，≤30天 → 税 20%
  S3 月持  T-5→T+20    ~57 自然日 → 税 10%
  S4 长持  T-5→T+245   ~1.1 年 → 税 0%
成本：往返 0.2%（佣金+印花+滑点）
对照：B0 不交易；各策略的"事件净收益"与分年/分层贡献。
附：预案公告日漂移（剔除与财报同日混发的样本）。
"""
from pathlib import Path
import warnings
import numpy as np
import pandas as pd
import duckdb

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports/dividend_facts"
con = duckdb.connect()
K = "read_parquet('data/lake/clean/mirror/kline_daily.parquet')"
DE = "read_parquet('data/lake/clean/mirror/dividend_events.parquet')"

kline = con.execute(f"SELECT code, date, close FROM {K} WHERE close>0 ORDER BY code, date").df()
kline["date"] = pd.to_datetime(kline["date"])
kline = kline.sort_values(["code", "date"]).reset_index(drop=True)
kline["pos"] = kline.groupby("code").cumcount()
kmap = kline.set_index(["code", "pos"])["close"]
pos_of = kline.set_index(["code", "date"])["pos"]
calendar_of = kline.set_index(["code", "pos"])["date"]

cash = con.execute(
    f"SELECT code, date, fenhong FROM {DE} "
    "WHERE fenhong>0 AND songzhuangu=0 AND peigu=0 AND date>='2022-02-01' AND date<='2026-08-31'").df()
cash["date"] = pd.to_datetime(cash["date"])
cash = cash[cash["code"].isin(kline["code"].unique())]

# 每事件：T 在 kline 的位置（除息日当天必须有 bar）
ev = cash.merge(pos_of.rename("posT"), left_on=["code", "date"], right_index=True, how="inner")
print(f"事件数: {len(ev)}（除息日有行情）")

COST = 0.002  # 往返

def event_return(code, posT, k_buy, k_sell, div=0.0, tax=0.0):
    """在 posT+k_buy 收盘买、posT+k_sell 收盘卖；税在卖出日按持股自然日计"""
    pb, ps = kmap.get((code, posT + k_buy)), kmap.get((code, posT + k_sell))
    if pb is None or ps is None or pb != pb or ps != ps or pb <= 0:
        return np.nan, None, None
    d_buy = calendar_of.get((code, posT + k_buy))
    d_sell = calendar_of.get((code, posT + k_sell))
    hold_days = (d_sell - d_buy).days
    rate = 0.20 if hold_days <= 30 else (0.10 if hold_days <= 365 else 0.0)
    rate = tax if tax is not None else rate
    gross = ps / pb - 1 + div * (1 - rate) / pb
    return gross - COST, d_buy, d_sell

strategies = [
    ("S1 抢权 T-10→T-1", -10, -1, False, 0.0),
    ("S2 短持 T-1→T+1", -1, 1, True, None),      # tax=None → 按自然日自动（≤30天→20%）
    ("S3 月持 T-5→T+20", -5, 20, True, None),    # ~57 自然日 → 10%
    ("S4 长持 T-5→T+245", -5, 245, True, None),  # ~1.1 年 → 0%
]
# S2/S3/S4 的 div=fenhong；S1 div=0 且显式税 0
res_rows = []
ev_out = ev[["code", "date", "fenhong"]].copy()
for nm, kb, ks, use_div, tax_ in strategies:
    rets = []
    for code, posT, fenh in zip(ev["code"], ev["posT"], ev["fenhong"]):
        d = fenh if use_div else 0.0
        tx = None if tax_ is None else tax_
        r, db, ds = event_return(code, posT, kb, ks, div=d, tax=tx)
        rets.append(r)
    ev_out[nm] = rets
    r = pd.Series(rets).dropna()
    res_rows.append({"策略": nm, "n": len(r), "均值%": r.mean() * 100,
                     "中位%": r.median() * 100, "胜率%": (r > 0).mean() * 100,
                     "扣成本前%": (r.mean() + COST) * 100})

tab = pd.DataFrame(res_rows)
print("\n[1] 事件策略（往返成本 0.2%，税按自然日差异化）")
print(tab.round(2).to_string(index=False))
tab.to_csv(OUT / "2b_event_summary.csv", index=False)

# 分层：股息率三分位（fenhong/T-1 收盘）
prev_close = []
for code, posT in zip(ev["code"], ev["posT"]):
    prev_close.append(kmap.get((code, posT - 1), np.nan))
ev_out["prev_close"] = prev_close
ev_out["yld"] = ev_out["fenhong"] / ev_out["prev_close"]
ev_out["ybucket"] = pd.qcut(ev_out["yld"].rank(method="first"), 3, labels=[1, 2, 3])
strat_cols = [c for c in ev_out.columns if c.startswith("S")]
print("\n[2] 按股息率三分位：事件净收益均值%")
lay = ev_out.dropna(subset=["prev_close"]).groupby("ybucket", observed=True)[strat_cols].mean() * 100
print(lay.round(2).to_string())
lay.to_csv(OUT / "2b_by_yield.csv")

print("\n[3] 分年：事件净收益均值%")
ev_out["yr"] = ev_out["date"].dt.year
yr = ev_out.groupby("yr")[strat_cols].mean() * 100
print(yr.round(2).to_string())
yr.to_csv(OUT / "2b_by_year.csv")

# ---- 附：预案公告日漂移（剔除财报同日混发）----
print("\n[4] 预案公告日漂移（条件化）")
try:
    fh = con.execute(
        "SELECT code, announce_date, ex_date, cash_per10 FROM read_parquet("
        "'data/lake/clean/dividend_announce/part-*.parquet') "
        "WHERE announce_date IS NOT NULL").df()
    fh["announce_date"] = pd.to_datetime(fh["announce_date"])
    fh = fh.drop_duplicates(["code", "announce_date"])
    import sys
    sys.path.insert(0, ".")
    from quantlab.data.store import Store
    st = Store()
    fin = st.q("SELECT DISTINCT code, notice_eff FROM finance_history WHERE notice_eff IS NOT NULL")
    fin["notice_eff"] = pd.to_datetime(fin["notice_eff"])
    fin_set = set(zip(fin["code"], fin["notice_eff"]))
    fh["mixed"] = [ (c, d) in fin_set or (c, d + pd.Timedelta(days=1)) in fin_set
                    or (c, d - pd.Timedelta(days=1)) in fin_set
                    for c, d in zip(fh["code"], fh["announce_date"]) ]
    print(f"  公告事件 {len(fh)} 条，其中与财报同日(±1天)混发 {fh['mixed'].sum()} 条")
    pos_ann = pos_of
    for tag, sub in [("全部", fh), ("剔除财报混发", fh[~fh["mixed"]])]:
        rets = []
        for code, d in zip(sub["code"], sub["announce_date"]):
            p = pos_ann.get((code, d))
            if p is None:
                continue
            r, _, _ = event_return(code, p, 0, 20, div=0.0, tax=0.0)
            if r == r:
                rets.append(r)
        r = pd.Series(rets)
        if len(r) > 30:
            print(f"  {tag}: n={len(r)}, CAR(0→+20)净均值 {r.mean()*100:+.3f}%, 胜率 {(r>0).mean()*100:.0f}%")
except Exception as e:
    print("  [4] 失败:", str(e)[:120])
print("DONE")
