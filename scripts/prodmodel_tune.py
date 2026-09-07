#!/usr/bin/env python3
"""生产模型重构 · 2b：两块式混合调优 + PROD_SI 补测
=====================================
发现：全等权卫星稀释核心 alpha（IC↑但组合收益↓，换手↑）。
假设：两块式 (1-w)·rank(核心) + w·rank(卫星块) 在小 w 下保倾斜、
借卫星降回撤。网格 w ∈ {0.1,0.2,0.3} × 卫星块组合。
PROD_SI 补测：跳过 IS/OOS（短窗无样本内段）。
结果合并写入 models_result.json / curves.pkl（新增 DUAL_* / PROD_SI）。
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
FOCUS = "2025-05-01"

sys.path.insert(0, str(ROOT / "scripts"))
from prodmodel_build import (backtest_pack, build_score, score_ic)  # noqa


def dual_score(core, sats, w):
    rc = build_score(core)
    rs = build_score(sats)
    m = rc.rename(columns={"score": "rc"}).merge(
        rs.rename(columns={"score": "rs"}), on=["date", "code"], how="inner")
    m["score"] = (1 - w) * m["rc"] + w * m["rs"]
    return m[["date", "code", "score"]]


def main():
    t0 = time.time()
    res = json.loads((OUT_DIR / "models_result.json").read_text(
        encoding="utf-8"))
    curves = pd.read_pickle(OUT_DIR / "curves.pkl")

    grid = {
        "DUAL_A": ([ "max_return_20"], 0.2),
        "DUAL_B": (["max_return_20", "amount_std_20"], 0.2),
        "DUAL_C": (["max_return_20", "amount_std_20"], 0.3),
        "DUAL_D": (["max_return_20", "amount_std_20", "ev_high_vol_20",
                    "turnover_std_20"], 0.2),
        "DUAL_E": (["max_return_20", "amount_std_20", "ev_high_vol_20",
                    "turnover_std_20"], 0.1),
    }
    for name, (sats, w) in grid.items():
        sc = dual_score(["size", "amihud_20"], sats, w)
        pack = backtest_pack(sc)
        pack["config"] = {"factors": ["size", "amihud_20"], "sats": sats,
                          "w_sat": w, "method": "two_block_rank"}
        res[name] = {k: v for k, v in pack.items()
                     if k not in ("curve", "ic_series")}
        curves[name] = {"curve": pack["curve"], "ic_series": pack["ic_series"]}
        print(f"{name} {json.dumps(pack['full'])} ic={pack['ic']['icir']} "
              f"({time.time()-t0:.0f}s)")

    # PROD_SI 补测（跳过 IS/OOS）
    try:
        sc = build_score(["size", "amihud_20", "sue_i", "overnight_mom_20"],
                         start="2025-05-01")
        from quantlab.optimize.backtest import run_optimized_backtest
        from prodmodel_build import _pack_run, END, FOCUS
        full = run_optimized_backtest(sc.copy(), n_stocks=100, rebalance=20,
                                      method="inverse_vol",
                                      start="2025-05-01", end=END)
        focus = run_optimized_backtest(sc.copy(), n_stocks=100, rebalance=20,
                                       method="inverse_vol",
                                       start=FOCUS, end=END)
        c = full["curve"].copy()
        c["year"] = pd.to_datetime(c["date"]).dt.year
        yearly = {int(y): round(float((1 + g["ret"]).prod() - 1), 4)
                  for y, g in c.groupby("year")}
        phases = []
        for k in range(4):
            st = str((pd.Timestamp(FOCUS) + pd.Timedelta(days=7 * k)).date())
            try:
                r = run_optimized_backtest(sc.copy(), n_stocks=100,
                                           rebalance=20, method="inverse_vol",
                                           start=st, end=END)
                m = r["metrics"]
                phases.append({"start": st, "annual": m["annual_return"],
                               "sharpe": m["sharpe"],
                               "mdd": m["max_drawdown"]})
            except RuntimeError:
                pass
        icr = score_ic(sc)
        res["PROD_SI"] = {
            "full": _pack_run(full), "focus": _pack_run(focus),
            "yearly": yearly, "phases": phases, "is_oos": None,
            "ic": {k: v for k, v in icr.items() if k != "series"},
            "config": {"factors": ["size", "amihud_20", "sue_i",
                                   "overnight_mom_20"],
                       "window": "2025-05-01+",
                       "method": "rank等权"}}
        curves["PROD_SI"] = {"curve": full["curve"][["date", "nav", "nav_bm"]],
                             "ic_series": icr["series"]}
        print(f"PROD_SI {json.dumps(res['PROD_SI']['full'])} "
              f"({time.time()-t0:.0f}s)")
    except Exception as e:
        print("PROD_SI failed again:", e)

    (OUT_DIR / "models_result.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    pd.to_pickle(curves, OUT_DIR / "curves.pkl")
    print(f"done {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
