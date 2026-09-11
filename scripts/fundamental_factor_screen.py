# -*- coding: utf-8 -*-
"""基本面因子 P3：首批 12 因子全市场多期限评估（IC + top100 价差 + 分层 + IS/OOS）

复用红利评估框架（dividend_factor_screen.py），差异：
  - 宇宙 = 全市场 panel（5,556 只，2022-01 起），非红利池
  - TOPN = 100（对齐生产 top100 口径）
  - 增加 Q1~Q5 五分位分层（等权前瞻收益，分年输出）
  - IS/OOS 分段：is(<2025-01-01) / oos(>=2025-01-01)

方向约定：表中 head_bp 均为"因子值 top100 − 全市场等权"。方向为负的因子
（如 accruals2 低应计为优）head_bp 为负属预期，abs(head_bp) 衡量强度。

用法: python scripts/fundamental_factor_screen.py
输出: reports/fundamental_factor/factor_screen.csv / layer_screen.csv
"""
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports/fundamental_factor"
OUT.mkdir(parents=True, exist_ok=True)

H_GRID = [5, 10, 20, 40, 60]
TOPN = 100
START = pd.Timestamp("2022-01-04")
OOS_START = pd.Timestamp("2025-01-01")
_BATCH1 = [
    "ep", "bp", "sp_ttm", "cfp_ttm",
    "roe_ttm", "roe_cut", "roic", "gpoa", "accruals2", "ocf_to_profit",
    "np_q_yoy", "rev_q_yoy",
]
_BATCH2 = ["sue_v2", "sur", "sue_gpoa", "roe_chg", "gpoa_chg", "ep_z5", "garp"]
FACTORS = sys.argv[1:] if len(sys.argv) > 1 else _BATCH1
_SUFFIX = "_b2" if FACTORS == _BATCH2 else ""

con = duckdb.connect()
panel = pd.read_parquet(ROOT / "reports/dividend_factor/panel.parquet")
panel = panel[panel["date"] >= START]
print(f"panel: {len(panel):,} 行 / {panel['code'].nunique()} 只 / "
      f"{panel['date'].min().date()} ~ {panel['date'].max().date()}", flush=True)


def rowcorr(A, B):
    m = ~(np.isnan(A) | np.isnan(B))
    A2, B2 = np.where(m, A, 0.0), np.where(m, B, 0.0)
    n = m.sum(1)
    ok = n >= 5
    ns = np.where(n == 0, 1, n)
    ma, mb = A2.sum(1) / ns, B2.sum(1) / ns
    da, db = (A2 - ma[:, None]) * m, (B2 - mb[:, None]) * m
    cov = (da * db).sum(1)
    va, vb = (da * da).sum(1), (db * db).sum(1)
    with np.errstate(invalid="ignore", divide="ignore"):
        r = cov / np.sqrt(va * vb)
    r[~ok] = np.nan
    return r


def nw_t(x, lag):
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 20:
        return np.nan
    d = x - x.mean()
    s = (d ** 2).mean()
    for l in range(1, lag + 1):
        if l >= n:
            break
        s += 2 * (1 - l / (lag + 1)) * (d[l:] * d[:-l]).mean()
    if s <= 0:
        return np.nan
    return x.mean() / np.sqrt(s / n)


rows, layer_rows = [], []
for f in FACTORS:
    fp = ROOT / f"data/lake/factor/{f}"
    if not fp.exists():
        print(f"  [{f}] 无因子数据，跳过", flush=True)
        continue
    fac = con.execute(
        f"SELECT date, code, value FROM read_parquet('{str(fp).replace(chr(92), '/')}/part-*.parquet') "
        f"WHERE date >= DATE '2022-01-04'").df()
    fac["date"] = pd.to_datetime(fac["date"])
    d = panel.merge(fac, on=["date", "code"], how="inner")
    if len(d) < 50000:
        print(f"  [{f}] 覆盖过少 {len(d):,}，跳过", flush=True)
        continue
    cov = len(d) / len(panel)
    F = d.pivot(index="date", columns="code", values="value")
    Fr = F.rank(axis=1).values
    dates_sub = F.index
    is_mask = np.asarray(dates_sub < OOS_START)
    years = dates_sub.year

    for h in H_GRID:
        R = d.pivot(index="date", columns="code", values=f"tot_ret_{h}")
        R = R.reindex(index=F.index, columns=F.columns)
        ic = pd.Series(rowcorr(Fr, R.rank(axis=1).values), index=dates_sub)
        rets = R.values
        fv = F.values
        hs, ls, hr, hd = [], [], [], []
        qret = []  # 每日五分位均值
        for k in range(len(dates_sub)):
            v, r = fv[k], rets[k]
            m = ~(np.isnan(v) | np.isnan(r))
            if m.sum() < 300:
                continue
            idx = np.where(m)[0]
            order = idx[np.argsort(-v[idx], kind="stable")]
            top, bot = order[:TOPN], order[-TOPN:]
            pmk = r[idx].mean()
            hs.append(r[top].mean() - pmk)
            ls.append(r[bot].mean() - pmk)
            hr.append(r[top].mean())
            hd.append(dates_sub[k])
            # 五分位：按值降序 Q1(最高)~Q5(最低)
            qs = np.array_split(order, 5)
            qret.append([r[q].mean() for q in qs])
        if not hd:
            continue
        hd = pd.DatetimeIndex(hd)
        hs, ls, hr = np.array(hs), np.array(ls), np.array(hr)
        qret = np.array(qret)  # (n_dates, 5)
        for tag, sel in [("all", np.ones(len(hd), bool)),
                         ("is", hd < OOS_START), ("oos", hd >= OOS_START)]:
            hs_s, ls_s, hr_s = hs[sel], ls[sel], hr[sel]
            hd_s = hd[sel]
            ics = ic.reindex(hd_s)
            rows.append(dict(
                factor=f, horizon=h, segment=tag,
                n_dates=len(ics), coverage=round(cov, 3),
                ic_mean=ics.mean(),
                icir=ics.mean() / ics.std() if ics.std() else np.nan,
                t_nw=nw_t(ics.values, 20), ic_pos=(ics > 0).mean(),
                head_bp=hs_s.mean() * 1e4 if len(hs_s) else np.nan,
                head_day_bp=hs_s.mean() * 1e4 / h if len(hs_s) else np.nan,
                head_t_nw=nw_t(hs_s, h) if len(hs_s) else np.nan,
                head_win=np.mean(hr_s > 0) if len(hr_s) else np.nan,
                tail_bp=ls_s.mean() * 1e4 if len(ls_s) else np.nan,
                spread_bp=(hs_s.mean() - ls_s.mean()) * 1e4 if len(hs_s) else np.nan))
        # 分层：全期 + 分年
        qdf = pd.DataFrame(qret, index=hd,
                           columns=["Q1", "Q2", "Q3", "Q4", "Q5"])
        for tag, sub in [("all", qdf)] + [(str(y), qdf[qdf.index.year == y])
                                          for y in sorted(set(hd.year))]:
            if len(sub) < 20:
                continue
            layer_rows.append(dict(
                factor=f, horizon=h, segment=tag, n_dates=len(sub),
                **{c: sub[c].mean() * 1e4 for c in qdf.columns},
                q1_q5_bp=(sub["Q1"].mean() - sub["Q5"].mean()) * 1e4))
    print(f"  [{f}] 完成 coverage={cov:.2f}", flush=True)

df = pd.DataFrame(rows)
df.to_csv(OUT / f"factor_screen{_SUFFIX}.csv", index=False)
ldf = pd.DataFrame(layer_rows)
ldf.to_csv(OUT / f"layer_screen{_SUFFIX}.csv", index=False)
print(f"\n完成 {len(df)} 条评估 / {len(ldf)} 条分层 → reports/fundamental_factor/", flush=True)

# 摘要：h=20 全期
sub = df[(df["horizon"] == 20) & (df["segment"] == "all")].copy()
sub = sub.reindex(sub["head_bp"].abs().sort_values(ascending=False).index)
print("\n=== h=20 · 全期（按 |head_bp| 排序）===")
print(sub[["factor", "coverage", "ic_mean", "t_nw", "head_bp", "head_t_nw",
           "head_win", "spread_bp"]].round(3).to_string(index=False))
