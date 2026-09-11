# -*- coding: utf-8 -*-
"""基本面因子 P3b：size 中性化后再评估（分离基本面 alpha 与 size 镜像）

方法：每日横截面 factor_rank ~ size_rank OLS 取残差（rank 残差，稳健），
再按同一框架计算 Rank IC / top100 价差（全期 + IS/OOS）。

size = log(float_mv)，来源 valuation_daily（fsdb，PIT 安全：当日已知）。

用法: python scripts/fundamental_factor_neutral.py
输出: reports/fundamental_factor/factor_screen_neutral.csv
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
VD = str((ROOT / "data/lake/clean/fundamental/valuation_daily").as_posix())

panel = pd.read_parquet(ROOT / "reports/dividend_factor/panel.parquet")
panel = panel[panel["date"] >= START]

# size：log float_mv（当日已知，无前视）
size = con.execute(f"""
    SELECT date, code, ln(float_mv) AS lsize
    FROM read_parquet('{VD}/part-*.parquet')
    WHERE date >= DATE '2022-01-04' AND float_mv > 0
""").df()
size["date"] = pd.to_datetime(size["date"])
base = panel.merge(size, on=["date", "code"], how="inner")
print(f"panel+size: {len(base):,} 行 / {base['date'].nunique()} 日", flush=True)


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


def neutralize_by_row(F, S):
    """逐行 rank 残差：F_rank ~ S_rank 回归取残差（NaN 全掩码行返回 NaN）"""
    out = np.full_like(F, np.nan)
    for k in range(F.shape[0]):
        f, s = F[k], S[k]
        m = ~(np.isnan(f) | np.isnan(s))
        if m.sum() < 300:
            continue
        fr = np.full_like(f, np.nan)
        fr[m] = pd.Series(f[m]).rank().values
        sr = np.full_like(s, np.nan)
        sr[m] = pd.Series(s[m]).rank().values
        x = sr[m]
        A = np.column_stack([x, np.ones_like(x)])
        beta, *_ = np.linalg.lstsq(A, fr[m], rcond=None)
        out[k, m] = fr[m] - A @ beta
    return out


rows = []
for f in FACTORS:
    fp = ROOT / f"data/lake/factor/{f}"
    if not fp.exists():
        continue
    fac = con.execute(
        f"SELECT date, code, value FROM read_parquet('{str(fp).replace(chr(92), '/')}/part-*.parquet') "
        f"WHERE date >= DATE '2022-01-04'").df()
    fac["date"] = pd.to_datetime(fac["date"])
    d = base.merge(fac, on=["date", "code"], how="inner")
    if len(d) < 50000:
        continue
    cov = len(d) / len(base)
    F = d.pivot(index="date", columns="code", values="value")
    S = d.pivot(index="date", columns="code", values="lsize").reindex(
        index=F.index, columns=F.columns)
    Fn = neutralize_by_row(F.values, S.values)   # 中性化后因子
    dates_sub = F.index

    for h in H_GRID:
        R = d.pivot(index="date", columns="code", values=f"tot_ret_{h}").reindex(
            index=F.index, columns=F.columns)
        ic = pd.Series(rowcorr(Fn, R.rank(axis=1).values), index=dates_sub)
        rets = R.values
        hs, ls, hr, hd = [], [], [], []
        for k in range(len(dates_sub)):
            v, r = Fn[k], rets[k]
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
    print(f"  [{f}] 中性化完成", flush=True)

df = pd.DataFrame(rows)
df.to_csv(OUT / f"factor_screen_neutral{_SUFFIX}.csv", index=False)
print(f"\n完成 {len(df)} 条 → factor_screen_neutral.csv", flush=True)

print("\n=== 中性化后 h=20 · 全期 ===")
sub = df[(df["horizon"] == 20) & (df["segment"] == "all")].copy()
sub = sub.reindex(sub["head_bp"].abs().sort_values(ascending=False).index)
print(sub[["factor", "ic_mean", "t_nw", "head_bp", "head_t_nw",
           "head_win", "spread_bp"]].round(3).to_string(index=False))
