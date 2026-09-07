"""A/B 结论稳健性扫描：调仓频率网格 × 相位偏移
================================================
问题：focus 窗口 plus−base=+6.4pp，但 base 绝对收益对调仓网格相位高度敏感
     （2025-05 起跑 +19.7% vs 2025-06 起跑 +9.3%，同引擎同参数）。
检验：{reb 15/25/30} × {base,plus,swap} + {reb20 相位偏移 2 档} × {base,plus,swap}
判据：plus−base 若跨网格一致为正 → 增量结论稳健；若符号乱跳 → 相位噪声主导。
输出：reports/composite_ab_robustness.json
"""
import sys
sys.path.insert(0, '.')
import warnings
warnings.filterwarnings("ignore")

import json
import time

from quantlab.model import build_composite, run_backtest

BASE = ["amplitude_20", "max_return_20", "momentum_120", "momentum_20",
        "momentum_60", "price_position_250", "reversal_10", "reversal_5",
        "rsi_14", "size", "total_mcap", "turnover", "volatility_20",
        "volatility_60"]
PLUS = BASE + ["sue", "overnight_mom_20"]
SWAP = [f for f in BASE if f != "price_position_250"] + \
       ["sue", "overnight_mom_20", "chip_vwap_bias_250"]

END = "2026-09-04"
GRID = [
    ("reb15", 15, "2025-05-01"),
    ("reb20_p0", 20, "2025-05-01"),   # = focus 基准相位
    ("reb20_p1", 20, "2025-05-20"),   # 相位 +10 交易日
    ("reb20_p2", 20, "2025-06-10"),   # 相位 +20 交易日
    ("reb25", 25, "2025-05-01"),
    ("reb30", 30, "2025-05-01"),
]


def main():
    t0 = time.time()
    scores = {}
    for tag, factors in (("base", BASE), ("plus", PLUS), ("swap", SWAP)):
        scores[tag] = build_composite(factors, universe="ashare_ex")
        print(f"[合成 {tag}] 完成")

    out = {"grid": {}, "meta": {
        "end": END, "note": "年化/夏普/超额年化，均 run_backtest top100 扣费"}}
    for gname, reb, start in GRID:
        out["grid"][gname] = {"reb": reb, "start": start}
        for tag in ("base", "plus", "swap"):
            r = run_backtest(scores[tag].copy(), n_stocks=100, rebalance=reb,
                             start=start, end=END)
            m, e = r["metrics"], r["excess"]
            out["grid"][gname][tag] = {
                "annual": m["annual_return"], "sharpe": m["sharpe"],
                "mdd": m["max_drawdown"],
                "excess": e["excess_annual"], "ir": e["information_ratio"],
                "turnover": r["turnover_avg"],
            }
            print(f"  {gname:9s} {tag:4s} 年化{m['annual_return']:+7.1%} "
                  f"夏普{m['sharpe']:5.2f} 超额{e['excess_annual']:+7.1%} "
                  f"IR{e['information_ratio']:+5.2f}")
        d = out["grid"][gname]
        print(f"  {gname:9s} Δ(plus−base)={d['plus']['annual']-d['base']['annual']:+.1%} "
              f"Δ(swap−base)={d['swap']['annual']-d['base']['annual']:+.1%}")

    with open("reports/composite_ab_robustness.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s → reports/composite_ab_robustness.json")


if __name__ == "__main__":
    main()
