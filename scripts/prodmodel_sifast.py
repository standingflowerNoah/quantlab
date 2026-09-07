#!/usr/bin/env python3
"""生产模型重构 · 第五步：SUE 快窗变体（sue_i_fast）回补历史 → PROD_SI 全期验证
=====================================
背景：标准 sue 框架（8 季窗 + min 6 观测）在 finance_history 2021Q1 起的约束下
最早 2025-04 出值，PROD_SI 只有 ~1.3 年证据。把窗口缩到 4 季（min 4 观测），
有效起点提前到 ~2023 中——多出 2 年含 2024 微盘股灾年的检验窗。
注意：sue_i_fast 是**因子定义变体**（σ 用 4 期观测更噪），结论用于稳健性
三角验证，不替代 sue_i 本体。

模型（rank 等权，ashare_ex）：
  PROD        size+amihud_20（基线）
  PROD_OM     size+amihud_20+overnight_mom_20（全历史单腿检验 2022-07+）
  EQ3F        核心+sue_i_fast（2023-07+）
  PROD_SIF    核心+sue_i_fast+overnight_mom_20（2023-07+）
输出: reports/prodmodel/si_fast.json + sue_i_fast.parquet
"""
from __future__ import annotations

import json
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "reports/prodmodel"
W0 = "2023-07-01"
W0_OM = "2022-07-01"
END = "2026-09-04"
FOCUS = "2025-05-01"
OOS = "2025-01-01"


def sue_fast_events():
    from quantlab.factor.library.earnings_surprise import (
        SueFactor, SuePred, _Q_BASE, _ASOF_TAIL)

    class SueFast(SueFactor):
        _min_win = 4

        def _body(self):
            return super()._body().replace("8 PRECEDING", "4 PRECEDING")

    class SuePredFast(SuePred):
        _min_win = 4

        def _body(self):
            return super()._body().replace("8 PRECEDING", "4 PRECEDING")

    def events(factor):
        sql = (factor._body()
               .format(min_win=factor._min_win, q_base=_Q_BASE,
                       asof_tail="")
               .replace("{usql}", "")) + """
SELECT code, CAST(notice_date AS DATE) AS event_date, value FROM g
"""
        from quantlab.data.store import Store
        return Store().q(sql)

    return events(SueFast()), events(SuePredFast())


def sue_i_fast(ev_fin, ev_pred):
    """90 日线性衰减（与 SueI 完全同逻辑）"""
    ev = pd.concat([ev_fin, ev_pred], ignore_index=True)
    ev["event_date"] = pd.to_datetime(ev["event_date"])
    ev = ev.dropna(subset=["value"]).groupby(
        ["code", "event_date"], as_index=False)["value"].mean()
    from quantlab.data.store import Store
    cal = Store().q("SELECT DISTINCT CAST(date AS DATE) AS date "
                    "FROM kline_daily ORDER BY date")
    cal_days = pd.to_datetime(cal["date"]).to_numpy(dtype="datetime64[ns]")
    w = 90
    frames = []
    for code, g in ev.groupby("code", sort=False):
        ed = g["event_date"].to_numpy(dtype="datetime64[ns]")
        s = g["value"].to_numpy(dtype=float)
        lo = np.searchsorted(ed, cal_days - np.timedelta64(w - 1, "D"))
        hi = np.searchsorted(ed, cal_days, side="right")
        valid = hi > lo
        if not valid.any():
            continue
        dates = cal_days[valid]
        li, ri = lo[valid], hi[valid]
        vals = np.empty(len(dates))
        for i in range(len(dates)):
            l, r = int(li[i]), int(ri[i])
            ag = (dates[i] - ed[l:r]).astype("timedelta64[D]").astype(float)
            wv = w - ag
            vals[i] = float((wv * s[l:r]).sum() / wv.sum())
        frames.append(pd.DataFrame({"date": dates, "code": code,
                                    "value": vals}))
    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"])
    print(f"sue_i_fast: 事件 {len(ev)} → 日频 {len(out)} 行, "
          f"{out['date'].min().date()} ~ {out['date'].max().date()}")
    return out


def load_lake_factor(name):
    import duckdb
    files = sorted((ROOT / f"data/lake/factor/{name}").glob("part-*.parquet"))
    paths = ", ".join("'" + str(f).replace("\\", "/") + "'" for f in files)
    df = duckdb.sql(f"SELECT date, code, value FROM read_parquet([{paths}]) "
                    f"WHERE value IS NOT NULL AND isfinite(value) "
                    f"AND abs(value)<1e300").fetchdf()
    df["date"] = pd.to_datetime(df["date"])
    return df


def rank_score(factors: dict[str, pd.DataFrame], start, directions=None):
    """各因子每日截面 rank [0,1]（×方向）→ 等权均值"""
    uni = set(__import__("quantlab.data.universe", fromlist=["get_universe"])
              .get_universe("ashare_ex"))
    frames = []
    for name, df in factors.items():
        d = 1 if not directions else directions.get(name, 1)
        f = df[df["code"].isin(uni)].copy()
        f["r"] = f.groupby("date")["value"].rank(pct=True) * d
        frames.append(f[["date", "code", "r"]])
    allf = pd.concat(frames)
    sc = allf.groupby(["date", "code"])["r"].mean().reset_index()
    sc = sc.rename(columns={"r": "score"})
    return sc[sc["date"] >= pd.to_datetime(start)]


def bt(score, start, n=100, reb=20, method="inverse_vol", end=END):
    from quantlab.optimize.backtest import run_optimized_backtest
    r = run_optimized_backtest(score.copy(), n_stocks=n, rebalance=reb,
                               method=method, start=start, end=end)
    m, ex = r["metrics"], r["excess"]
    c = r["curve"].copy()
    c["year"] = pd.to_datetime(c["date"]).dt.year
    return {"annual": m["annual_return"], "sharpe": m["sharpe"],
            "mdd": m["max_drawdown"], "calmar": m["calmar"],
            "excess": ex["excess_annual"], "ir": ex["information_ratio"],
            "turnover": r["turnover_avg"],
            "yearly": {int(y): round(float((1 + g["ret"]).prod() - 1), 4)
                       for y, g in c.groupby("year")},
            "curve": c[["date", "ret", "bench", "nav"]].copy()}


def main():
    t0 = time.time()
    # 1. sue_i_fast 构建与落盘
    pkl = OUT_DIR / "sue_i_fast.parquet"
    if pkl.exists():
        sif = pd.read_parquet(pkl)
        print(f"sue_i_fast 从缓存载入 {len(sif)} 行")
    else:
        ev_fin, ev_pred = sue_fast_events()
        print(f"events fin={len(ev_fin)} pred={len(ev_pred)} "
              f"({time.time()-t0:.0f}s)")
        sif = sue_i_fast(ev_fin, ev_pred)
        sif.to_parquet(pkl, index=False)
    sys.path.insert(0, str(ROOT / "scripts"))

    # 2. 评分
    size = load_lake_factor("size")
    amihud = load_lake_factor("amihud_20")
    omom = load_lake_factor("overnight_mom_20")
    from prodmodel_build import score_ic  # noqa

    # 全历史单腿检验（2022-07+）
    DIRT = {"size": -1}          # FACTOR_DIRECTION 口径：size 做多低值
    prod = rank_score({"size": size, "amihud_20": amihud}, W0_OM, DIRT)
    prod_om = rank_score({"size": size, "amihud_20": amihud,
                          "overnight_mom_20": omom}, W0_OM, DIRT)
    res = {"OM_LEG": {}, "SIF": {}}
    for name, sc in (("PROD", prod), ("PROD_OM", prod_om)):
        r = bt(sc, W0_OM)
        r.pop("curve")
        r["icir"] = score_ic(sc)["icir"]
        res["OM_LEG"][name] = r
        print(f"{name}(2022-07+): ann {r['annual']:.3f} sharpe {r['sharpe']} "
              f"mdd {r['mdd']:.3f} icir {r['icir']} ({time.time()-t0:.0f}s)")

    # sue_i_fast 全期检验（2023-07+）
    sif_start = sif["date"].min()
    print(f"sue_i_fast 有效起点 {sif_start.date()}")
    start3 = max(pd.Timestamp(W0), sif_start + pd.Timedelta(days=90)).strftime("%Y-%m-%d")
    prod3 = rank_score({"size": size, "amihud_20": amihud}, start3, DIRT)
    eq3f = rank_score({"size": size, "amihud_20": amihud,
                       "sue_i_fast": sif}, start3, DIRT)
    prod_sif = rank_score({"size": size, "amihud_20": amihud,
                           "sue_i_fast": sif, "overnight_mom_20": omom},
                          start3, DIRT)
    res["SIF"]["_window_start"] = start3
    for name, sc in (("PROD", prod3), ("EQ3F", eq3f), ("PROD_SIF", prod_sif)):
        r = bt(sc, start3)
        c = r.pop("curve")
        r["icir"] = score_ic(sc)["icir"]
        # 4 相位 + OOS
        phases = []
        for k in range(4):
            st = str((pd.Timestamp(FOCUS) + pd.Timedelta(days=7 * k)).date())
            p = bt(sc, st)
            phases.append({"start": st, "annual": p["annual"],
                           "sharpe": p["sharpe"], "mdd": p["mdd"]})
        r["phases"] = phases
        oos_in = bt(sc, start3, end=OOS)
        oos_out = bt(sc, OOS)
        r["is_oos"] = {"in": {k: oos_in[k] for k in
                              ("annual", "sharpe", "mdd")},
                       "out": {k: oos_out[k] for k in
                               ("annual", "sharpe", "mdd")}}
        res["SIF"][name] = r
        print(f"{name}({start3}+): ann {r['annual']:.3f} sharpe "
              f"{r['sharpe']} mdd {r['mdd']:.3f} icir {r['icir']} "
              f"({time.time()-t0:.0f}s)")

    # 曲线存 pickle 供报告
    curves = {}
    for name, sc in (("PROD_OM", prod_om), ("EQ3F", eq3f),
                     ("PROD_SIF", prod_sif)):
        r = bt(sc, W0_OM if name == "PROD_OM" else start3)
        curves[name] = r["curve"][["date", "nav"]]
    pd.to_pickle(curves, OUT_DIR / "si_fast_curves.pkl")

    (OUT_DIR / "si_fast.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    print(f"done {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
