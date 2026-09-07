#!/usr/bin/env python3
"""生产模型重构 · 第三步：敏感性分析（参数网格）
=====================================
对 PROD 基线与指定新模型做：
  - 选股数 n ∈ {50,100,200}
  - 调仓频率 reb ∈ {10,20,40}
  - 权重法 method ∈ {inverse_vol, equal, min_var}（n=100, reb=20）
输出: reports/prodmodel/sensitivity.json
用法: python scripts/prodmodel_sensitivity.py [MODEL_A MODEL_B]
      默认 PROD + CORE_EX（若存在）
"""
from __future__ import annotations

import json
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "reports/prodmodel"
FULL_START = "2022-07-01"
END = "2026-09-04"


def build(model: str):
    from quantlab.model import build_composite
    from quantlab.model.pool_select import build_pool_refined_score
    if model == "PROD":
        return build_composite(["size", "amihud_20"], universe="ashare_ex",
                               start=FULL_START)
    cfg = json.loads((OUT_DIR / "models_result.json").read_text(
        encoding="utf-8"))[model].get("config", {})
    if cfg.get("method") == "two_block_rank":     # 两块式混合
        rc = build_composite(cfg["factors"], universe="ashare_ex",
                             start=FULL_START)
        rs = build_composite(cfg["sats"], universe="ashare_ex",
                             start=FULL_START)
        m = rc.rename(columns={"score": "rc"}).merge(
            rs.rename(columns={"score": "rs"}), on=["date", "code"],
            how="inner")
        w = cfg["w_sat"]
        m["score"] = (1 - w) * m["rc"] + w * m["rs"]
        return m[["date", "code", "score"]]
    if "refine" in cfg:      # 池内精选
        base = build_composite(cfg["base"], universe="ashare_ex",
                               start=FULL_START)
        refine = build_composite([cfg["refine"]], universe="ashare_ex",
                                 start=FULL_START)
        return build_pool_refined_score(base, refine, pool_n=cfg["pool_n"])
    if model == "PROD_SI":
        return build_composite(cfg["factors"], universe="ashare_ex",
                               start="2025-05-01")
    return build_composite(cfg["factors"], universe="ashare_ex",
                           method=cfg.get("method", "equal"),
                           start=FULL_START)


def main():
    from quantlab.optimize.backtest import run_optimized_backtest
    res = json.loads((OUT_DIR / "models_result.json").read_text(
        encoding="utf-8"))
    names = sys.argv[1:] or [
        m for m in ("PROD", "CORE_EX", "CORE_ALL") if m in res][:2]
    print("sensitivity on:", names)
    out = {}
    t0 = time.time()
    for name in names:
        score = build(name)
        grid = {}
        for n in (50, 100, 200):
            for reb in (10, 20, 40):
                r = run_optimized_backtest(score.copy(), n_stocks=n,
                                           rebalance=reb,
                                           method="inverse_vol",
                                           start=FULL_START, end=END)
                m = r["metrics"]
                grid[f"n{n}_reb{reb}"] = {
                    "annual": m["annual_return"], "sharpe": m["sharpe"],
                    "mdd": m["max_drawdown"], "calmar": m["calmar"]}
                print(f"  {name} n{n} reb{reb}: ann {m['annual_return']:.3f} "
                      f"sharpe {m['sharpe']} ({time.time()-t0:.0f}s)")
        for meth in ("equal", "min_var"):
            r = run_optimized_backtest(score.copy(), n_stocks=100,
                                       rebalance=20, method=meth,
                                       start=FULL_START, end=END)
            m = r["metrics"]
            grid[f"n100_reb20_{meth}"] = {
                "annual": m["annual_return"], "sharpe": m["sharpe"],
                "mdd": m["max_drawdown"], "calmar": m["calmar"]}
            print(f"  {name} {meth}: ann {m['annual_return']:.3f} "
                  f"sharpe {m['sharpe']} ({time.time()-t0:.0f}s)")
        out[name] = grid
    (OUT_DIR / "sensitivity.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"done {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
