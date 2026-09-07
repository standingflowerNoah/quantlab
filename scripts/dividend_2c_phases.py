# -*- coding: utf-8 -*-
"""红利 2C 相位网格扫描：P_PROD vs P_PROD+DY 的增量是否跨相位稳健
每 20 交易日调仓 × 3 相位（offset 0/5/10），Top50 等权，分红加回（日历窗口近似）
"""
from pathlib import Path
import pandas as pd
import numpy as np
import duckdb

ROOT = Path(__file__).resolve().parent.parent
con = duckdb.connect()
K = "read_parquet('data/lake/clean/mirror/kline_daily.parquet')"
DE = "read_parquet('data/lake/clean/mirror/dividend_events.parquet')"

kline = con.execute(f"SELECT code, date, close FROM {K} WHERE close > 0 ORDER BY code, date").df()
kline["date"] = pd.to_datetime(kline["date"])
cash = con.execute(f"SELECT code, date, fenhong FROM {DE} WHERE fenhong>0 AND songzhuangu=0 AND peigu=0").df()
cash["date"] = pd.to_datetime(cash["date"])

def load_factor(name):
    df = con.execute(f"SELECT date, code, value FROM read_parquet('data/lake/factor/{name}/part-*.parquet')").df()
    df["date"] = pd.to_datetime(df["date"])
    return df

size, amihud = load_factor("size"), load_factor("amihud_20")
kd = kline.merge(size.rename(columns={"value": "size"}), on=["date", "code"], how="inner")
kd = kd.merge(amihud.rename(columns={"value": "amihud"}), on=["date", "code"], how="inner")
kd = kd.sort_values(["code", "date"]).reset_index(drop=True)
kd = kd[kd["close"] >= 2.0]
# 前瞻 20 交易日收益
g = kd.groupby("code")
kd["fwd_close"] = g["close"].shift(-20)
kd = kd.dropna(subset=["fwd_close"])

# 调仓日网格：每 20 交易日 × 3 相位
all_dates = np.sort(kd["date"].unique())
grids = {}
for off in [0, 5, 10]:
    grids[off] = all_dates[off::20]

# 分红加回：日历窗口 (d, d+28d]；TTM： (d-365d, d]
ev_by_code = {c: g[["date", "fenhong"]].values for c, g in cash.groupby("code")}

def dy_and_div(code, dates):
    ev = ev_by_code.get(code)
    if ev is None:
        return pd.Series(0.0, index=dates), pd.Series(0.0, index=dates)
    ex = ev[:, 0]
    fen = ev[:, 1]
    ttm = np.array([fen[(ex > d - np.timedelta64(365, "D")) & (ex <= d)].sum() for d in dates])
    fwd = np.array([fen[(ex > d) & (ex <= d + np.timedelta64(28, "D"))].sum() for d in dates])
    return pd.Series(ttm, index=dates), pd.Series(fwd, index=dates)

results = {}
for off, grid in grids.items():
    sub = kd[kd["date"].isin(grid)].copy()
    sub = sub.sort_values(["code", "date"])
    ttm_list, div_list = [], []
    for code, gg in sub.groupby("code"):
        t, f = dy_and_div(code, gg["date"].values)
        ttm_list.append(pd.DataFrame({"code": code, "date": gg["date"].values, "ttm": t.values, "fdiv": f.values}))
    aux = pd.concat(ttm_list)
    sub = sub.merge(aux, on=["code", "date"], how="left")
    sub["dy"] = sub["ttm"] / sub["close"]
    sub["fwd_ret"] = (sub["fwd_close"] + sub["fdiv"]) / sub["close"] - 1
    sub["size_r"] = sub.groupby("date")["size"].rank()
    sub["ami_r"] = sub.groupby("date")["amihud"].rank()
    sub["dy_r"] = sub.groupby("date")["dy"].rank()
    sub["rc_prod"] = 0.5 * (1 - sub["size_r"]) + 0.5 * sub["ami_r"]
    sub["rc_dy"] = (1 - sub["size_r"] + sub["ami_r"] + sub["dy_r"]) / 3

    def port(score, top=50):
        parts = []
        for d, gg in sub.groupby("date"):
            gg = gg.dropna(subset=[score])
            if len(gg) < 100:
                continue
            parts.append(gg.nlargest(top, score).assign(date=d))
        h = pd.concat(parts)
        return h.groupby("date")["fwd_ret"].mean()

    results[off] = (port("rc_prod"), port("rc_dy"))

print(f"{'相位':>4} {'P_PROD年化%':>12} {'P_PROD+DY年化%':>14} {'Δ年化pp':>9} {'Δ夏普':>8}")
deltas = []
for off, (a, b) in results.items():
    aa = (1 + a).prod() ** (12 / len(a)) - 1
    bb = (1 + b).prod() ** (12 / len(b)) - 1
    sa = a.mean() / a.std() * np.sqrt(12)
    sb = b.mean() / b.std() * np.sqrt(12)
    deltas.append(bb - aa)
    print(f"{off:>4} {aa*100:>12.1f} {bb*100:>14.1f} {(bb-aa)*100:>9.1f} {sb-sa:>8.2f}")
print(f"\n相位 Δ 年化全正: {all(d > 0 for d in deltas)}  （闸门二判定依据）")
