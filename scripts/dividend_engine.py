# -*- coding: utf-8 -*-
"""红利因子研究 · 矩阵回测引擎（可复用）

提供：红利池日频宽表（收益矩阵 / 因子 rank 矩阵 / 池基准）、重叠口径超额评估、
网格换手、非重叠单相位回测、Newey-West t。
"""
import json
import numpy as np
import pandas as pd
import duckdb
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports/dividend_factor"
H_GRID = [5, 10, 20, 40, 60]
ANN = 250.0
OOS_START = pd.Timestamp("2025-01-01")


def nw_t(x, lag):
    x = np.asarray(x, float)
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 20:
        return np.nan
    d = x - x.mean()
    s = (d ** 2).mean()
    for l in range(1, min(lag, n - 1) + 1):
        s += 2 * (1 - l / (lag + 1)) * (d[l:] * d[:-l]).mean()
    return x.mean() / np.sqrt(s / n) if s > 0 else np.nan


def ann_stats(r):
    if r is None or len(r) < 6:
        return (np.nan,) * 3
    span = (r.index[-1] - r.index[0]).days / 365.25
    ppy = len(r) / span if span > 0 else np.nan
    cum = (1 + r).cumprod()
    return ((1 + r).prod() ** (ppy / len(r)) - 1) * 100, \
           (r.mean() / r.std() * np.sqrt(ppy) if r.std() else np.nan), \
           (cum / cum.cummax() - 1).min() * 100


class DivData:
    def __init__(self, pool="official", start="2022-06-01", dir_pool=None, screen_pool=None):
        self.pool = pool
        dir_pool = dir_pool or pool
        panel = pd.read_parquet(OUT / "panel.parquet")
        p = pd.read_parquet(OUT / f"pool_{pool}.parquet")
        base = panel.merge(p, on=["date", "code"], how="inner")
        self.base = base[base["date"] >= pd.Timestamp(start)].copy()
        self.screen = pd.read_csv(OUT / f"factor_screen_{screen_pool or pool}.csv")
        self.DATES = pd.DatetimeIndex(sorted(self.base["date"].unique()))
        self.CODES = np.array(sorted(self.base["code"].unique()))
        self.RETM = {}
        for h in H_GRID:
            pv = self.base.pivot(index="date", columns="code", values=f"tot_ret_{h}")
            self.RETM[h] = pv.reindex(index=self.DATES, columns=self.CODES).values
        self.BENCH = {}
        for h in H_GRID:
            with np.errstate(invalid="ignore"):
                self.BENCH[h] = pd.Series(np.nanmean(self.RETM[h], axis=1), index=self.DATES).dropna()
        # 方向（仅 IS 段判定）
        full = self.screen[(self.screen["coverage"] > 0.8) & (self.screen["n_dates"] > 100)].copy()
        full["side"] = np.where(full["head_bp"] >= full["tail_bp"], 1, -1)
        self.DIR = {f: (1 if g["side"].sum() >= 0 else -1)
                    for f, g in full[full["segment"] == "is"].groupby("factor")}
        if dir_pool != pool:  # 用另一池的因子表补方向
            ref = pd.read_csv(OUT / f"factor_screen_{dir_pool}.csv")
            ref = ref[(ref["coverage"] > 0.8) & (ref["n_dates"] > 100)].copy()
            ref["side"] = np.where(ref["head_bp"] >= ref["tail_bp"], 1, -1)
            for f, g in ref[ref["segment"] == "is"].groupby("factor"):
                self.DIR[f] = 1 if g["side"].sum() >= 0 else -1
        self._cache = {}
        self.con = duckdb.connect()

    def factor(self, f, directed=True):
        key = (f, directed)
        if key in self._cache:
            return self._cache[key]
        code_sql = ",".join(f"'{c}'" for c in self.CODES)
        fac = self.con.execute(
            f"SELECT date, code, value FROM read_parquet("
            f"'{str(ROOT / f'data/lake/factor/{f}').replace(chr(92), '/')}/part-*.parquet') "
            f"WHERE date >= DATE '2022-06-01' AND code IN ({code_sql})").df()
        fac["date"] = pd.to_datetime(fac["date"])
        d = self.base[["date", "code"]].merge(fac, on=["date", "code"], how="left")
        d["r"] = d.groupby("date")["value"].rank(pct=True)
        if directed:
            d["r"] *= float(self.DIR.get(f, 1))
        M = d.pivot(index="date", columns="code", values="r").reindex(
            index=self.DATES, columns=self.CODES).values
        self._cache[key] = M
        return M

    def mat(self, cols):
        return np.nanmean(np.stack([self.factor(c) for c in cols]), axis=0)

    @staticmethod
    def topn_idx(x, r, topn):
        m = ~(np.isnan(x) | np.isnan(r))
        if m.sum() < max(topn, 30):
            return None
        idx = np.where(m)[0]
        return idx, idx[np.argsort(-x[idx], kind="stable")][:topn]

    def gross_ex(self, X, h, topn=10, lim=None):
        R = self.RETM[h]
        out = np.full(len(self.DATES), np.nan)
        for i in range(len(self.DATES)):
            if lim is not None and not (lim[0] <= self.DATES[i] <= lim[1]):
                continue
            t = self.topn_idx(X[i], R[i], topn)
            if t is None:
                continue
            out[i] = R[i][t[1]].mean() - R[i][t[0]].mean()
        return pd.Series(out, index=self.DATES).dropna()

    def turnover_grid(self, X, h, topn=10):
        R, tos = self.RETM[h], []
        for ph in range(h):
            sets = []
            for i in range(ph, len(self.DATES), h):
                t = self.topn_idx(X[i], R[i], topn)
                if t is None:
                    continue
                sets.append(set(self.CODES[t[1]]))
            if len(sets) > 1:
                tos.append(np.mean([1 - len(a & b) / len(a | b) for a, b in zip(sets[:-1], sets[1:])]))
        return np.mean(tos) if tos else np.nan

    def evaluate(self, X, h, topn=10, cost=0.003, lim=None):
        ex = self.gross_ex(X, h, topn, lim)
        if len(ex) < 30:
            return None
        to = self.turnover_grid(X, h, topn)
        g = ex.mean() * ANN / h * 100
        return dict(n=len(ex), gross_ann=g, net_ann=g - to * cost * ANN / h * 100,
                    turnover=to, t_nw=nw_t(ex.values, h), win=(ex > 0).mean(), ex=ex)

    def simulate_grid(self, X, h, phase, topn=10, cost=0.003):
        R = self.RETM[h]
        dates, rets, sets = [], [], []
        for i in range(phase, len(self.DATES), h):
            t = self.topn_idx(X[i], R[i], topn)
            if t is None:
                continue
            dates.append(self.DATES[i])
            rets.append(R[i][t[1]].mean())
            sets.append(set(self.CODES[t[1]]))
        if len(rets) < 6:
            return None
        r = pd.Series(rets, index=pd.DatetimeIndex(dates))
        to = (np.mean([1 - len(a & b) / len(a | b) for a, b in zip(sets[:-1], sets[1:])])
              if len(sets) > 1 else np.nan)
        net = r - to * cost
        b = self.BENCH[h].reindex(r.index)
        a, s, d = ann_stats(net)
        return dict(phase=phase, n=len(r), ann=a, sharpe=s, mdd=d, bench_ann=ann_stats(b)[0],
                    ann_ex=ann_stats(net - b)[0], turnover=to, net=net, bench=b)

    def multi(self, X, n, h, cost=0.003):
        return [x for x in (self.simulate_grid(X, h, ph, n, cost) for ph in range(h)) if x]

    # ---- 常用区间 ----
    @property
    def IS_LIM(self):
        return (self.DATES[0], pd.Timestamp("2024-12-31"))

    @property
    def OOS_LIM(self):
        return (pd.Timestamp("2025-01-01"), self.DATES[-1])

    @property
    def FULL_LIM(self):
        return (self.DATES[0], self.DATES[-1])

    def max_fwd_date(self, h):
        """最后一个有 h 日前瞻收益的日期"""
        ok = ~np.isnan(self.RETM[h]).all(axis=1)
        return self.DATES[np.where(ok)[0][-1]]
