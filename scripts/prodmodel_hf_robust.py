# -*- coding: utf-8 -*-
"""HF_AMIH（size+amihud_20+hf_amihud_20）切换前稳健性：参数网格 + 滑点压力

窗口同 hf_add（2025-02-01 ~ 2026-09-04），口径 top100/20日/inverse_vol/全成本。
产出 reports/prodmodel/hf_robust.json
"""
import json
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

import pandas as pd

from quantlab.model import build_composite
from quantlab.optimize import backtest as ob

OUT = "reports/prodmodel/hf_robust.json"
START, END = "2025-02-01", "2026-09-04"


def bt(score, n=100, reb=20, method="inverse_vol", cost_mult=1.0):
    from quantlab.model.backtest import COST_PER_TURNOVER as BASE
    old = ob.COST_PER_TURNOVER
    ob.COST_PER_TURNOVER = BASE * cost_mult
    try:
        r = ob.run_optimized_backtest(score.copy(), n_stocks=n,
                                      rebalance=reb, method=method,
                                      start=START, end=END)
    finally:
        ob.COST_PER_TURNOVER = old
    m = r["metrics"]
    return {"annual": round(float(m["annual_return"]), 4),
            "sharpe": round(float(m["sharpe"]), 2),
            "mdd": round(float(m["max_drawdown"]), 4),
            "turnover": round(float(r["turnover_avg"]), 4)}


def main():
    models = {
        "PROD": ["size", "amihud_20"],
        "EQ3": ["size", "amihud_20", "sue_i"],
        "HF_AMIH": ["size", "amihud_20", "hf_amihud_20"],
    }
    scores = {k: build_composite(v, universe="ashare_ex", start=START)
              for k, v in models.items()}

    grid = {}
    for name, sc in scores.items():
        g = {}
        for n in (50, 100, 200):
            for reb in (10, 20, 40):
                g[f"n{n}_r{reb}"] = bt(sc, n=n, reb=reb)
        for method in ("equal", "min_var"):
            try:
                g[f"w_{method}"] = bt(sc, method=method)
            except Exception as e:
                g[f"w_{method}"] = {"error": str(e)[:80]}
        grid[name] = g
        anns = [v["annual"] for v in g.values() if "annual" in v]
        shs = [v["sharpe"] for v in g.values() if "sharpe" in v]
        print(f"{name}: ann {min(anns)*100:.1f}-{max(anns)*100:.1f}% "
              f"sharpe {min(shs)}-{max(shs)}", flush=True)

    cost = {}
    for mult in (2.0, 3.0):
        for name, sc in scores.items():
            cost[f"{name}_x{mult:g}"] = bt(sc, cost_mult=mult)
            print(f"cost {name} x{mult:g}: "
                  f"{cost[f'{name}_x{mult:g}']}", flush=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"grid": grid, "cost": cost}, f,
                  ensure_ascii=False, indent=1)
    print("saved", OUT, flush=True)


if __name__ == "__main__":
    main()
