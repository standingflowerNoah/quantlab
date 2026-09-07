# -*- coding: utf-8 -*-
"""红利 2B 第一轮补充：市场调整口径（向量化）
对 S1/S2/S3 与预案公告漂移给出减 000300 同窗收益的超额。
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

kline = con.execute(f"SELECT code, date, close FROM {K} WHERE close>0 ORDER BY code, date").df()
kline["date"] = pd.to_datetime(kline["date"])
kline = kline.sort_values(["code", "date"]).reset_index(drop=True)
kline["pos"] = kline.groupby("code").cumcount()
close_by = kline.set_index(["code", "pos"])["close"]
date_by = kline.set_index(["code", "pos"])["date"]
pos_of = kline.set_index(["code", "date"])["pos"]

b300 = con.execute(
    "SELECT date, close FROM read_parquet('data/lake/clean/mirror/index_kline.parquet') "
    "WHERE code='000300.SH'").df()
b300["date"] = pd.to_datetime(b300["date"])
b300 = b300.sort_values("date").reset_index(drop=True)

# 市场收益：事件窗口日 → 300 asof 收盘，向量化 merge_asof
def add_mkt(df, d1_col, d2_col):
    lo = pd.merge_asof(df[[d1_col]].sort_values(d1_col).rename(columns={d1_col: "date"}),
                       b300.rename(columns={"date": "date"}), on="date", direction="backward")
    hi = pd.merge_asof(df[[d2_col]].sort_values(d2_col).rename(columns={d2_col: "date"}),
                       b300.rename(columns={"date": "date"}), on="date", direction="backward")
    df["m1"] = lo["close"].values
    df["m2"] = hi["close"].values
    df["mkt"] = df["m2"] / df["m1"] - 1
    return df

cash = con.execute(
    f"SELECT code, date, fenhong FROM read_parquet('data/lake/clean/mirror/dividend_events.parquet') "
    "WHERE fenhong>0 AND songzhuangu=0 AND peigu=0 AND date>='2022-02-01' AND date<='2026-08-31'").df()
cash["date"] = pd.to_datetime(cash["date"])
ev = cash.merge(pos_of.rename("posT"), left_on=["code", "date"], right_index=True, how="inner")

def strat(kb, ks, use_div, label):
    w = ev.copy()
    for tag, off in [("b", kb), ("s", ks)]:
        w[f"p_{tag}"] = close_by.reindex(list(zip(w["code"], w["posT"] + off))).values
        w[f"d_{tag}"] = date_by.reindex(list(zip(w["code"], w["posT"] + off))).values
    w = w.dropna(subset=["p_b", "p_s"])
    w = w[w["p_b"] > 0]
    hold = (w["d_s"] - w["d_b"]).dt.days
    rate = np.where(hold <= 30, 0.20, np.where(hold <= 365, 0.10, 0.0))
    div_net = w["fenhong"] * (1 - rate) if use_div else 0.0
    gross = w["p_s"] / w["p_b"] - 1 + div_net / w["p_b"] - 0.002
    w = add_mkt(w, "d_b", "d_s")
    exc = gross - w["mkt"]
    ok = exc.notna()
    print(f"{label}: n={ok.sum()} 原始均值 {gross[ok].mean()*100:+.3f}% | "
          f"市场调整 {exc[ok].mean()*100:+.3f}% | 超额胜率 {(exc[ok]>0).mean()*100:.0f}%")
    return exc[ok]

e1 = strat(-10, -1, False, "S1 抢权 T-10→T-1")
e2 = strat(-1, 1, True, "S2 短持 T-1→T+1")
e3 = strat(-5, 20, True, "S3 月持 T-5→T+20")
e1.to_csv(OUT / "2b_s1_excess.csv", index=False)

# 预案公告漂移（市场调整）
fh = con.execute(
    "SELECT code, announce_date FROM read_parquet('data/lake/clean/dividend_announce/part-*.parquet') "
    "WHERE announce_date IS NOT NULL").df()
fh["announce_date"] = pd.to_datetime(fh["announce_date"])
fh = fh.drop_duplicates(["code", "announce_date"])
w = fh.copy()
w["posT"] = pos_of.reindex(list(zip(w["code"], w["announce_date"]))).values
w = w.dropna(subset=["posT"])
w["posT"] = w["posT"].astype(int)
for tag, off in [("b", 0), ("s", 20)]:
    w[f"p_{tag}"] = close_by.reindex(list(zip(w["code"], w["posT"] + off))).values
    w[f"d_{tag}"] = date_by.reindex(list(zip(w["code"], w["posT"] + off))).values
w = w.dropna(subset=["p_b", "p_s"])
w = w[w["p_b"] > 0]
gross = w["p_s"] / w["p_b"] - 1 - 0.002
w = add_mkt(w, "d_b", "d_s")
exc = gross - w["mkt"]
ok = exc.notna()
print(f"公告漂移 CAR(0→+20): n={ok.sum()} 原始 {gross[ok].mean()*100:+.3f}% | "
      f"市场调整 {exc[ok].mean()*100:+.3f}% | 胜率 {(exc[ok]>0).mean()*100:.0f}%")
# 剔除财报混发
import sys
sys.path.insert(0, ".")
from quantlab.data.store import Store
st = Store()
fin = st.q("SELECT DISTINCT code, notice_eff FROM finance_history WHERE notice_eff IS NOT NULL")
fin["notice_eff"] = pd.to_datetime(fin["notice_eff"])
fset = set(zip(fin["code"], fin["notice_eff"])) | set(zip(fin["code"], fin["notice_eff"] + pd.Timedelta(days=1))) | set(zip(fin["code"], fin["notice_eff"] - pd.Timedelta(days=1)))
w["mixed"] = [(c, d) in fset for c, d in zip(w["code"], w["announce_date"])]
exc_nm = exc[w["mixed"].values]
exc_cl = exc[~w["mixed"].values]
print(f"  全部 n={len(exc_nm)+len(exc_cl)} 调整均值 {pd.concat([exc_nm, exc_cl]).mean()*100:+.3f}%")
print(f"  剔除财报混发 n={len(exc_cl)} 调整均值 {exc_cl.mean()*100:+.3f}% | 胜率 {(exc_cl>0).mean()*100:.0f}%")
print("DONE")
