# -*- coding: utf-8 -*-
"""红利因子研究 · 阶段 1：全因子湖在红利池内的多期限评估（含 IS/OOS 分段）

对因子湖全部可用因子，在红利成分池内做：
  - 池内截面 Rank IC（vs 前瞻 h 日含息收益），Newey-West 调整 t（lag=h 处理重叠）
  - 头部价差 head_spread = top10 等权收益 − 池内全成员等权收益（主指标；IC 仅参考）
  - 尾部价差 / 头部胜率 / 覆盖率
分段：all / is(<2025-01-01) / oos(>=2025-01-01)

用法: python scripts/dividend_factor_screen.py [official|approx]
输出: reports/dividend_factor/factor_screen_{pool}.csv
"""
import sys
import json
import glob
from pathlib import Path
import numpy as np
import pandas as pd
import duckdb

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports/dividend_factor"
H_GRID = [5, 10, 20, 40, 60]
TOPN = 10
POOL = sys.argv[1] if len(sys.argv) > 1 else "official"
START = pd.Timestamp("2022-06-01")
OOS_START = pd.Timestamp("2025-01-01")

con = duckdb.connect()
panel = pd.read_parquet(OUT / "panel.parquet")
pool = pd.read_parquet(OUT / f"pool_{POOL}.parquet")
print(f"池={POOL}: {len(pool):,} 行 / {pool['code'].nunique()} 只 / "
      f"{pool['date'].min().date()} ~ {pool['date'].max().date()}", flush=True)

# ---- 因子清单 ----
meta = {}
for p in glob.glob(str(ROOT / "data/lake/factor_audit/*.json")):
    try:
        j = json.load(open(p, encoding="utf-8"))
    except Exception:
        continue
    st = j.get("checks", {}).get("structure", {})
    dmin = st.get("date_min")
    if dmin and pd.Timestamp(dmin) <= pd.Timestamp("2022-12-01"):
        meta[j["factor"]] = dict(category=j.get("category"), dmin=dmin, n=st.get("n_rows"))
EXCLUDE = {"dragon_net_20", "total_mcap"}
factors = sorted(set(meta) - EXCLUDE)
print(f"待评估因子 {len(factors)} 个", flush=True)

# ---- 池内面板 ----
base = panel.merge(pool, on=["date", "code"], how="inner")
base = base[base["date"] >= START]
pool_codes = sorted(base["code"].unique())
print(f"池内面板 {len(base):,} 行, {base['date'].nunique()} 个交易日, {len(pool_codes)} 只", flush=True)
code_sql = ",".join(f"'{c}'" for c in pool_codes)


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


rows = []
for i, f in enumerate(factors):
    fp = ROOT / f"data/lake/factor/{f}"
    if not fp.exists():
        continue
    try:
        fac = con.execute(
            f"SELECT date, code, value FROM read_parquet('{str(fp).replace(chr(92), '/')}/part-*.parquet') "
            f"WHERE date >= DATE '2022-06-01' AND code IN ({code_sql})").df()
    except Exception as e:
        print(f"  [{f}] 读取失败 {str(e)[:60]}", flush=True)
        continue
    fac["date"] = pd.to_datetime(fac["date"])
    d = base.merge(fac, on=["date", "code"], how="inner")
    if len(d) < 5000:
        continue
    cov = len(d) / len(base)
    F = d.pivot(index="date", columns="code", values="value")
    Fr = F.rank(axis=1).values
    dates_sub = F.index
    is_mask = np.asarray(dates_sub < OOS_START)
    for h in H_GRID:
        R = d.pivot(index="date", columns="code", values=f"tot_ret_{h}").rank(axis=1)
        R = R.reindex(index=F.index, columns=F.columns)
        ic = pd.Series(rowcorr(Fr, R.values), index=dates_sub)
        rets = d.pivot(index="date", columns="code",
                       values=f"tot_ret_{h}").reindex(index=F.index, columns=F.columns).values
        fv = F.values
        hs, ls, hr, hd = [], [], [], []
        for k in range(len(dates_sub)):
            v, r = fv[k], rets[k]
            m = ~(np.isnan(v) | np.isnan(r))
            if m.sum() < 30:
                continue
            idx = np.where(m)[0]
            order = idx[np.argsort(-v[idx], kind="stable")]
            top, bot = order[:TOPN], order[-TOPN:]
            pmk = r[idx].mean()
            hs.append(r[top].mean() - pmk)
            ls.append(r[bot].mean() - pmk)
            hr.append(r[top].mean())
            hd.append(dates_sub[k])
        if not hd:
            continue
        hd = pd.DatetimeIndex(hd)
        hs, ls, hr = np.array(hs), np.array(ls), np.array(hr)
        for tag, sel in [("all", np.ones(len(hd), bool)),
                         ("is", hd < OOS_START), ("oos", hd >= OOS_START)]:
            hs_s, ls_s, hr_s = hs[sel], ls[sel], hr[sel]
            hd_s = hd[sel]
            ics = ic.reindex(hd_s)
            rows.append(dict(
                factor=f, category=meta[f]["category"], horizon=h, segment=tag,
                n_dates=len(ics), coverage=round(cov, 3),
                ic_mean=ics.mean(), icir=ics.mean() / ics.std() if ics.std() else np.nan,
                t_nw=nw_t(ics.values, 20), ic_pos=(ics > 0).mean(),
                head_bp=hs_s.mean() * 1e4 if len(hs_s) else np.nan,
                head_day_bp=hs_s.mean() * 1e4 / h if len(hs_s) else np.nan,
                head_t_nw=nw_t(hs_s, h) if len(hs_s) else np.nan,
                head_win=np.mean(hr_s > 0) if len(hr_s) else np.nan,
                tail_bp=ls_s.mean() * 1e4 if len(ls_s) else np.nan,
                spread_bp=(hs_s.mean() - ls_s.mean()) * 1e4 if len(hs_s) else np.nan))
    if (i + 1) % 20 == 0:
        print(f"  ... {i+1}/{len(factors)}", flush=True)

df = pd.DataFrame(rows)
df.to_csv(OUT / f"factor_screen_{POOL}.csv", index=False)
print(f"\n完成 {len(df)} 条记录 → factor_screen_{POOL}.csv", flush=True)

for h in H_GRID:
    sub = df[(df["horizon"] == h) & (df["segment"] == "all")].copy()
    t = sub.reindex(sub["head_bp"].abs().sort_values(ascending=False).index).head(10)
    print(f"\n=== h={h} 日 · 头部价差 Top10 ===")
    print(t[["factor", "category", "ic_mean", "head_bp", "head_t_nw", "head_win", "coverage"]].round(3).to_string(index=False))
