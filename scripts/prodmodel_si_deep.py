#!/usr/bin/env python3
"""生产模型重构 · 第四步：PROD_SI 切换前深化验证（短窗 2025-05+）
=====================================
1. 变体/权重扫描：两块式 (1-w)·core + w·si，w ∈ {0.4,0.3,0.2}；
   equal 4 因子（PROD_SI 本体）；只留 sue_i；equal 3 因子
2. 敏感性网格：PROD 与 PROD_SI 在 n×reb + 权重法
3. 成本压力：滑点×2 / ×3（monkeypatch COST_PER_TURNOVER）
4. 月度稳定性：月度超额、SI-PROD 配对月差胜率、回撤段
输出: reports/prodmodel/si_deep.json
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
W0, W1 = "2025-05-01", "2026-09-04"
CORE = ["size", "amihud_20"]
SI = ["sue_i", "overnight_mom_20"]


def blend(core_score, si_score, w):
    m = core_score.rename(columns={"score": "rc"}).merge(
        si_score.rename(columns={"score": "rs"}), on=["date", "code"],
        how="inner")
    m["score"] = (1 - w) * m["rc"] + w * m["rs"]
    return m[["date", "code", "score"]]


def bt(score, n=100, reb=20, method="inverse_vol", cost_mult=1.0):
    from quantlab.optimize import backtest as ob
    from quantlab.model.backtest import COST_PER_TURNOVER as BASE
    old = ob.COST_PER_TURNOVER
    ob.COST_PER_TURNOVER = BASE * cost_mult
    try:
        r = ob.run_optimized_backtest(score.copy(), n_stocks=n,
                                      rebalance=reb, method=method,
                                      start=W0, end=W1)
    finally:
        ob.COST_PER_TURNOVER = old
    m, ex = r["metrics"], r["excess"]
    return {"annual": m["annual_return"], "sharpe": m["sharpe"],
            "mdd": m["max_drawdown"], "calmar": m["calmar"],
            "excess": ex["excess_annual"], "ir": ex["information_ratio"],
            "turnover": r["turnover_avg"],
            "curve": r["curve"][["date", "ret", "bench", "nav"]].copy()}


def ic_of(score):
    sys.path.insert(0, str(ROOT / "scripts"))
    from prodmodel_build import score_ic
    return score_ic(score)["icir"]


def monthly(curve):
    c = curve.copy()
    c["date"] = pd.to_datetime(c["date"])
    c["ym"] = c["date"].dt.strftime("%Y-%m")
    g = c.groupby("ym").apply(
        lambda x: (1 + x["ret"]).prod() - (1 + x["bench"].fillna(0)).prod())
    return g


def main():
    t0 = time.time()
    from quantlab.model import build_composite
    core = build_composite(CORE, universe="ashare_ex", start=W0)
    si = build_composite(SI, universe="ashare_ex", start=W0)
    sue = build_composite(["sue_i"], universe="ashare_ex", start=W0)
    print(f"scores built ({time.time()-t0:.0f}s)")

    variants = {
        "PROD": core,
        "PROD_SI": build_composite(CORE + SI, universe="ashare_ex",
                                   start=W0),
        "V3_W40": blend(core, si, 0.4),          # = V3_SI 口径
        "W30": blend(core, si, 0.3),
        "W20": blend(core, si, 0.2),
        "SUE_ONLY_W40": blend(core, sue, 0.4),
        "EQ3": build_composite(CORE + ["sue_i"], universe="ashare_ex",
                               start=W0),
    }
    out = {"variants": {}, "icir": {}}
    curves = {}
    for name, sc in variants.items():
        r = bt(sc)
        out["variants"][name] = {k: v for k, v in r.items() if k != "curve"}
        out["icir"][name] = ic_of(sc)
        curves[name] = r["curve"]
        print(f"{name}: ann {r['annual']:.3f} sharpe {r['sharpe']} "
              f"mdd {r['mdd']:.3f} icir {out['icir'][name]} "
              f"({time.time()-t0:.0f}s)")

    # 敏感性网格：PROD vs PROD_SI
    sens = {}
    for name in ("PROD", "PROD_SI"):
        sc = variants[name]
        grid = {}
        for n in (50, 100, 200):
            for reb in (10, 20, 40):
                r = bt(sc, n=n, reb=reb)
                grid[f"n{n}_reb{reb}"] = {k: r[k] for k in
                                          ("annual", "sharpe", "mdd")}
        for meth in ("equal", "min_var"):
            r = bt(sc, method=meth)
            grid[f"n100_reb20_{meth}"] = {k: r[k] for k in
                                          ("annual", "sharpe", "mdd")}
        sens[name] = grid
        print(f"sens {name} done ({time.time()-t0:.0f}s)")
    out["sensitivity"] = sens

    # 成本压力：滑点×2 / ×3
    cost = {}
    for name in ("PROD", "PROD_SI"):
        for mult in (1.0, 2.0, 3.0):
            r = bt(variants[name], cost_mult=mult)
            cost[f"{name}_x{mult:g}"] = {
                "annual": r["annual"], "sharpe": r["sharpe"],
                "mdd": r["mdd"]}
            print(f"cost {name} x{mult}: ann {r['annual']:.3f} "
                  f"sharpe {r['sharpe']} ({time.time()-t0:.0f}s)")
    out["cost_stress"] = cost

    # 月度稳定性（PROD vs PROD_SI）
    mp = monthly(curves["PROD"])
    ms = monthly(curves["PROD_SI"])
    diff = ms - mp
    out["monthly"] = {
        "months": list(mp.index),
        "prod": [round(float(v), 4) for v in mp],
        "si": [round(float(v), 4) for v in ms],
        "diff_win_rate": round(float((diff > 0).mean()), 3),
        "diff_mean": round(float(diff.mean()), 4),
        "n_months": int(len(mp)),
        "worst_si_month": {"ym": str(ms.idxmin()),
                           "ret": round(float(ms.min()), 4)},
    }
    # 日收益相关（两模型信号重叠度参考）
    a = curves["PROD"].set_index("date")["ret"]
    b = curves["PROD_SI"].set_index("date")["ret"]
    j = pd.concat([a, b], axis=1, keys=["p", "s"]).dropna()
    out["daily_ret_corr"] = round(float(j["p"].corr(j["s"])), 3)

    (OUT_DIR / "si_deep.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"done {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
