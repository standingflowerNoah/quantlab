"""星期效应稳健性网格（2026-09-07 第十轮）
n=50/200 × 周一~周五 × {等权, ICW}，短窗 2025-02+，EQ3_HFA 因子集。
检查"周四最优"是否依赖 top100 参数。
产物: reports/prodmodel/weekday_grid.json
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")

import pandas as pd

from scripts.prodmodel_weekday import FOCUS, MODELS, WEEKDAY_CN, _m, \
    run_weekly_backtest
from quantlab.model import build_composite

OUT = Path("reports/prodmodel")


def main():
    fac = MODELS["EQ3_HFA"]
    scores = {}
    for meth in ("equal", "ic_weighted"):
        scores[meth] = build_composite(fac, universe="ashare_ex", method=meth)

    res = {}
    for meth, sc in scores.items():
        for n in (50, 200):
            key = f"{'EQ' if meth=='equal' else 'ICW'}_n{n}"
            row = {}
            for wd in range(5):
                r = run_weekly_backtest(sc.copy(), wd, n_stocks=n,
                                        start=FOCUS[0], end=FOCUS[1])
                row[WEEKDAY_CN[wd]] = {"annual": _m(r)["annual"],
                                       "sharpe": _m(r)["sharpe"],
                                       "mdd": _m(r)["mdd"]}
                print(key, WEEKDAY_CN[wd], row[WEEKDAY_CN[wd]], flush=True)
            res[key] = row

    (OUT / "weekday_grid.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1))
    print("saved ->", OUT / "weekday_grid.json")


if __name__ == "__main__":
    main()
