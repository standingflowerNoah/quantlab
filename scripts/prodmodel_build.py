#!/usr/bin/env python3
"""生产模型重构 · 第二步：因子筛选 → 候选模型构建 → 统一回测
=====================================
输入: reports/prodmodel/factor_ic20.csv + 相关矩阵/聚类
卫星两组:
  ALL 组（含 alpha191 代表，验证既往 batch7/8「a191 稀释组合」结论是否复现）
  EX  组（纯非 alpha 卫星 → 生产候选）
模型:
  PROD        size+amihud_20（现役基线）
  CORE_ALL    核心+ALL 卫星 等权
  CORE_EX     核心+EX 卫星 等权
  CORE_EX_IW  核心+EX 卫星 IC 加权
  POOL_REFINE 基线选池 400 + 最强非 alpha 卫星池内重排
  PROD_SI     size+amihud_20+sue_i+overnight_mom_20（短窗对比）
输出: reports/prodmodel/models_result.json / curves.pkl / contribution.csv
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

FULL_START = "2022-07-01"
END = "2026-09-04"
FOCUS = "2025-05-01"
OOS_SPLIT = "2025-01-01"
CORE = ["size", "amihud_20"]

IC_SQL = """
WITH fwd AS (
    SELECT date, code, c_lead / c - 1 AS fwd
    FROM (
        SELECT date, code, close * adj_factor AS c,
               LEAD(close * adj_factor, 20) OVER (
                   PARTITION BY code ORDER BY date) AS c_lead
        FROM kline_daily)
    WHERE c_lead IS NOT NULL AND c > 0),
j AS (
    SELECT CAST(s.date AS DATE) AS date, s.score, w.fwd
    FROM score_df s JOIN fwd w ON CAST(s.date AS DATE) = w.date
         AND s.code = w.code),
rk AS (
    SELECT date, score, fwd,
           rank() OVER (PARTITION BY date ORDER BY score) AS rv,
           rank() OVER (PARTITION BY date ORDER BY fwd) AS rf,
           count(*) OVER (PARTITION BY date) AS n
    FROM j WHERE score IS NOT NULL)
SELECT date, corr(rv, rf) AS ic FROM rk WHERE n >= 300 GROUP BY date
"""


def score_ic(score: pd.DataFrame) -> dict:
    """复合评分的日度 Rank IC20 摘要（经 Store 只读查询）"""
    from quantlab.data.store import Store
    store = Store()
    sc = score.copy()
    sc["date"] = pd.to_datetime(sc["date"]).dt.date
    store.con.register("score_df", sc)
    df = store.q(IC_SQL)
    ic = df["ic"].dropna()
    df["date"] = pd.to_datetime(df["date"])
    df["year"] = df["date"].dt.year
    yearly = df.groupby("year")["ic"].mean()
    return {
        "ic_mean": round(float(ic.mean()), 5),
        "icir": round(float(ic.mean() / ic.std()), 3) if ic.std() > 0 else 0,
        "ic_win": round(float((ic > 0).mean()), 4),
        "yearly_ic": {int(y): round(float(v), 5) for y, v in yearly.items()},
        "series": df.set_index("date")["ic"],
    }


def select_satellites(allow_alpha: bool):
    summ = pd.read_csv(OUT_DIR / "factor_ic20.csv")
    corr = pd.read_csv(ROOT / "reports/factor_corr_matrix_20260907.csv",
                       index_col=0)
    clusters = pd.read_csv(ROOT / "reports/factor_clusters_20260907.csv")

    banned = set(CORE)
    for _, r in clusters.iterrows():
        try:
            members = eval(r["members"])
        except Exception:
            continue
        if any(m in CORE for m in members):
            banned.update(members)

    cand = summ[
        (summ["icir"].abs() >= 0.20)
        & (summ["coverage"] >= 0.80)
        & (summ["n_days"] >= 900)
        & (summ["data_start"] <= FULL_START)
        & (~summ["factor"].str.endswith("_score"))
        & (~summ["factor"].isin(banned))
    ].copy()
    if not allow_alpha:
        cand = cand[~cand["factor"].str.startswith("alpha")]
    cand["absicir"] = cand["icir"].abs()
    cand = cand.sort_values("absicir", ascending=False)

    picked = list(CORE)
    detail = []
    for _, r in cand.iterrows():
        f = r["factor"]
        if f not in corr.index:
            continue
        max_rho = max((abs(corr.loc[f, p]) if p in corr.columns else 0)
                      for p in picked)
        if max_rho >= 0.7:
            detail.append({"factor": f, "icir": r["icir"], "picked": False,
                           "reason": f"corr {max_rho:.2f} with picked"})
            continue
        picked.append(f)
        detail.append({"factor": f, "icir": r["icir"], "picked": True,
                       "reason": f"max_rho {max_rho:.2f}"})
        if len(picked) >= len(CORE) + 8:
            break
    return pd.DataFrame(detail), picked[len(CORE):]


def build_score(factors: list[str], method="equal", start=FULL_START):
    from quantlab.model import build_composite
    return build_composite(factors, universe="ashare_ex", method=method,
                           start=start)


def _pack_run(r: dict) -> dict:
    m, ex = r["metrics"], r["excess"]
    return {"annual": m["annual_return"], "sharpe": m["sharpe"],
            "mdd": m["max_drawdown"], "calmar": m["calmar"],
            "vol": m["annual_vol"], "excess": ex["excess_annual"],
            "ir": ex["information_ratio"],
            "excess_mdd": ex["excess_max_drawdown"],
            "turnover": r["turnover_avg"]}


def backtest_pack(score: pd.DataFrame) -> dict:
    from quantlab.optimize.backtest import run_optimized_backtest
    from quantlab.model.walkforward import split_backtest
    pack = {}
    full = run_optimized_backtest(score.copy(), n_stocks=100, rebalance=20,
                                  method="inverse_vol",
                                  start=FULL_START, end=END)
    pack["full"] = _pack_run(full)
    focus = run_optimized_backtest(score.copy(), n_stocks=100, rebalance=20,
                                   method="inverse_vol",
                                   start=FOCUS, end=END)
    pack["focus"] = _pack_run(focus)
    c = full["curve"].copy()
    c["year"] = pd.to_datetime(c["date"]).dt.year
    pack["yearly"] = {int(y): round(float((1 + g["ret"]).prod() - 1), 4)
                      for y, g in c.groupby("year")}
    phases = []
    for k in range(4):
        st = str((pd.Timestamp(FOCUS) + pd.Timedelta(days=7 * k)).date())
        try:
            r = run_optimized_backtest(score.copy(), n_stocks=100,
                                       rebalance=20, method="inverse_vol",
                                       start=st, end=END)
            m = r["metrics"]
            phases.append({"start": st, "annual": m["annual_return"],
                           "sharpe": m["sharpe"], "mdd": m["max_drawdown"]})
        except RuntimeError:
            pass
    pack["phases"] = phases
    inr, outr = split_backtest(score.copy(), split_date=OOS_SPLIT,
                               n_stocks=100, rebalance=20)
    pack["is_oos"] = {"in": _pack_run(inr), "out": _pack_run(outr)}
    icr = score_ic(score)
    pack["ic"] = {k: v for k, v in icr.items() if k != "series"}
    pack["ic_series"] = icr.pop("series")
    pack["curve"] = full["curve"][["date", "nav", "nav_bm"]].copy()
    pack["turnover_avg"] = full["turnover_avg"]
    return pack


def main():
    t0 = time.time()
    detail_all, sats_all = select_satellites(allow_alpha=True)
    detail_ex, sats_ex = select_satellites(allow_alpha=False)
    print("卫星 ALL:", sats_all)
    print("卫星 EX :", sats_ex)
    detail_all.to_csv(OUT_DIR / "satellite_selection_all.csv", index=False)
    detail_ex.to_csv(OUT_DIR / "satellite_selection_ex.csv", index=False)

    models, curves = {}, {}
    score_prod = build_score(CORE)

    # —— 贡献度：leave-one-in（ALL 组与 EX 组都做，直接比较）——
    base_ic = score_ic(score_prod)
    contrib = []
    for f in sats_all:
        r = score_ic(build_score(CORE + [f]))
        contrib.append({"factor": f, "group": "ALL",
                        "icir_core": base_ic["icir"], "icir_add": r["icir"],
                        "ic_mean_add": r["ic_mean"],
                        "d_icir": round(r["icir"] - base_ic["icir"], 3)})
        print(f"  +{f}: ICIR {base_ic['icir']} -> {r['icir']} "
              f"({time.time()-t0:.0f}s)")
    for f in sats_ex:
        if f in sats_all:
            continue
        r = score_ic(build_score(CORE + [f]))
        contrib.append({"factor": f, "group": "EX",
                        "icir_core": base_ic["icir"], "icir_add": r["icir"],
                        "ic_mean_add": r["ic_mean"],
                        "d_icir": round(r["icir"] - base_ic["icir"], 3)})
        print(f"  +{f}: ICIR {base_ic['icir']} -> {r['icir']}")
    pd.DataFrame(contrib).to_csv(OUT_DIR / "contribution.csv", index=False)

    # —— 模型回测 ——
    models["PROD"] = backtest_pack(score_prod)
    curves["PROD"] = {"curve": models["PROD"].pop("curve"),
                      "ic_series": models["PROD"].pop("ic_series")}
    print(f"PROD {json.dumps(models['PROD']['full'])} ({time.time()-t0:.0f}s)")

    models["CORE_ALL"] = backtest_pack(build_score(CORE + sats_all))
    models["CORE_ALL"]["config"] = {"factors": CORE + sats_all, "method": "equal"}
    curves["CORE_ALL"] = {"curve": models["CORE_ALL"].pop("curve"),
                          "ic_series": models["CORE_ALL"].pop("ic_series")}
    print(f"CORE_ALL {json.dumps(models['CORE_ALL']['full'])} "
          f"({time.time()-t0:.0f}s)")

    models["CORE_EX"] = backtest_pack(build_score(CORE + sats_ex))
    models["CORE_EX"]["config"] = {"factors": CORE + sats_ex, "method": "equal"}
    curves["CORE_EX"] = {"curve": models["CORE_EX"].pop("curve"),
                         "ic_series": models["CORE_EX"].pop("ic_series")}
    print(f"CORE_EX {json.dumps(models['CORE_EX']['full'])} "
          f"({time.time()-t0:.0f}s)")

    models["CORE_EX_IW"] = backtest_pack(
        build_score(CORE + sats_ex, method="ic_weighted"))
    models["CORE_EX_IW"]["config"] = {"factors": CORE + sats_ex,
                                      "method": "ic_weighted"}
    curves["CORE_EX_IW"] = {"curve": models["CORE_EX_IW"].pop("curve"),
                            "ic_series": models["CORE_EX_IW"].pop("ic_series")}
    print(f"CORE_EX_IW {json.dumps(models['CORE_EX_IW']['full'])} "
          f"({time.time()-t0:.0f}s)")

    # 池内精选：基线选池 + 最强非 alpha 卫星重排（按 d_icir）
    ex_contrib = [c for c in contrib
                  if c["group"] == "EX" or c["factor"] in sats_ex]
    best_sat = max(ex_contrib, key=lambda x: x["d_icir"])["factor"]
    from quantlab.model.pool_select import build_pool_refined_score
    sc_pr = build_pool_refined_score(score_prod,
                                     build_score([best_sat]), pool_n=400)
    models["POOL_REFINE"] = backtest_pack(sc_pr)
    models["POOL_REFINE"]["config"] = {"base": CORE, "refine": best_sat,
                                       "pool_n": 400}
    curves["POOL_REFINE"] = {"curve": models["POOL_REFINE"].pop("curve"),
                             "ic_series": models["POOL_REFINE"].pop("ic_series")}
    print(f"POOL_REFINE {json.dumps(models['POOL_REFINE']['full'])} "
          f"({time.time()-t0:.0f}s)")

    # PROD_SI 短窗对比（sue_i 2025-04 起）
    try:
        score_si = build_score(["size", "amihud_20", "sue_i",
                                "overnight_mom_20"], start="2025-05-01")
        models["PROD_SI"] = backtest_pack(score_si)
        models["PROD_SI"]["config"] = {
            "factors": ["size", "amihud_20", "sue_i", "overnight_mom_20"],
            "window": "2025-05-01+"}
        curves["PROD_SI"] = {"curve": models["PROD_SI"].pop("curve"),
                             "ic_series": models["PROD_SI"].pop("ic_series")}
        print(f"PROD_SI {json.dumps(models['PROD_SI']['full'])} "
              f"({time.time()-t0:.0f}s)")
    except Exception as e:
        print("PROD_SI failed:", e)

    (OUT_DIR / "models_result.json").write_text(
        json.dumps(models, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    pd.to_pickle(curves, OUT_DIR / "curves.pkl")
    print(f"ALL DONE {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
