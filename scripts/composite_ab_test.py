"""新因子组合回测 A/B 实验（收口第一批 sue + 第二批 overnight_mom_20 / chip_vwap_bias_250）
=====================================================================================
组合定义（等权 rank 合成，universe=ashare_ex，方向按 FACTOR_DIRECTION）：
  BASE = 现有 14 因子量价组合（model_comparison 惯例，含 price_position_250）
  PLUS = BASE + sue + overnight_mom_20                     —— 增量检验
  SWAP = BASE − price_position_250 + sue + overnight_mom_20 + chip_vwap_bias_250 —— 替换检验

回测（run_backtest）：top100、20日调仓、基准中证1000、扣费（佣金万2.5+印花税千1卖+滑点10bp×2）
窗口：
  full  = 2022-07-01 起（alpha191_fusion_test 惯例）× {plain, sealed 封板剔除}
  focus = 2025-05-01 起（sue 生效后，三组同因子数干净对比）× plain
  walkforward = split 2025-06-01（sue 生效后切分，method=equal 保持口径）
输出：reports/composite_ab_20260906.json
"""
import sys
sys.path.insert(0, '.')

import json
import time
import warnings
warnings.filterwarnings("ignore")

import pandas as pd

from quantlab.model import build_composite, run_backtest
from quantlab.model.walkforward import split_backtest

BASE = ["amplitude_20", "max_return_20", "momentum_120", "momentum_20",
        "momentum_60", "price_position_250", "reversal_10", "reversal_5",
        "rsi_14", "size", "total_mcap", "turnover", "volatility_20",
        "volatility_60"]
PLUS = BASE + ["sue", "overnight_mom_20"]
SWAP = [f for f in BASE if f != "price_position_250"] + \
       ["sue", "overnight_mom_20", "chip_vwap_bias_250"]

N, REB = 100, 20
FULL_START, FOCUS_START, SPLIT = "2022-07-01", "2025-05-01", "2025-06-01"


def yearly(curve: pd.DataFrame) -> dict:
    c = curve.copy()
    c["year"] = pd.to_datetime(c["date"]).dt.year
    return {int(y): round(float((1 + g["ret"]).prod() - 1), 4)
            for y, g in c.groupby("year")}


def pack(res: dict, with_curve=False) -> dict:
    m, e = res["metrics"], res["excess"]
    out = {
        "annual_return": m["annual_return"], "sharpe": m["sharpe"],
        "max_drawdown": m["max_drawdown"], "calmar": m["calmar"],
        "annual_vol": m["annual_vol"], "total_return": m["total_return"],
        "excess_annual": e["excess_annual"],
        "information_ratio": e["information_ratio"],
        "excess_max_drawdown": e["excess_max_drawdown"],
        "win_rate_vs_bm": e["win_rate_vs_bm"],
        "turnover_avg": res["turnover_avg"],
        "yearly": yearly(res["curve"]),
    }
    if with_curve:
        out["dates"] = [str(pd.Timestamp(d).date()) for d in res["curve"]["date"]]
        out["nav"] = [round(float(v), 4) for v in res["curve"]["nav"]]
        out["nav_bm"] = [round(float(v), 4) for v in res["curve"]["nav_bm"]]
    return out


def brief(tag, res):
    m, e = res["metrics"], res["excess"]
    print(f"  {tag:24s} 年化{m['annual_return']:+7.1%} 夏普{m['sharpe']:6.2f} "
          f"回撤{m['max_drawdown']:7.1%} 超额{e['excess_annual']:+7.1%} "
          f"IR{e['information_ratio']:+6.2f} 换手{res['turnover_avg']:.0%}")


def main():
    t0 = time.time()
    out = {"config": {
        "base": BASE, "plus": PLUS, "swap": SWAP,
        "n_stocks": N, "rebalance": REB, "benchmark": "000852.SH",
        "full_start": FULL_START, "focus_start": FOCUS_START, "split": SPLIT,
    }}

    scores = {}
    for tag, factors in (("base", BASE), ("plus", PLUS), ("swap", SWAP)):
        t = time.time()
        scores[tag] = build_composite(factors, universe="ashare_ex")
        print(f"[合成 {tag}] {len(scores[tag])} 行, "
              f"{scores[tag]['date'].nunique()} 天, 耗时 {time.time()-t:.0f}s")

    # ── 1. 全窗口 plain + sealed ──
    out["full"] = {}
    for tag in ("base", "plus", "swap"):
        for sealed in (False, True):
            key = tag + ("_sealed" if sealed else "")
            res = run_backtest(scores[tag].copy(), n_stocks=N, rebalance=REB,
                               start=FULL_START,
                               tradable="sealed" if sealed else None)
            out["full"][key] = pack(res, with_curve=(not sealed))
            brief(f"full/{key}", res)

    # ── 2. focus 窗口（sue 生效后）──
    out["focus"] = {}
    for tag in ("base", "plus", "swap"):
        res = run_backtest(scores[tag].copy(), n_stocks=N, rebalance=REB,
                           start=FOCUS_START)
        out["focus"][tag] = pack(res, with_curve=True)
        brief(f"focus/{tag}", res)

    # ── 3. 样本内外切分（method=equal 与主口径一致）──
    out["walkforward"] = {}
    for tag in ("base", "plus", "swap"):
        in_r, out_r = split_backtest(scores[tag].copy(), split_date=SPLIT,
                                     n_stocks=N, rebalance=REB, method="equal")
        out["walkforward"][tag] = {"in": pack(in_r), "out": pack(out_r)}
        print(f"  wf/{tag:4s} 样本内 年化{in_r['metrics']['annual_return']:+.1%} / "
              f"样本外 年化{out_r['metrics']['annual_return']:+.1%} "
              f"(超额 {out_r['excess']['excess_annual']:+.1%}, "
              f"IR {out_r['excess']['information_ratio']})")

    with open("reports/composite_ab_20260906.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成，总耗时 {time.time()-t0:.0f}s → reports/composite_ab_20260906.json")


if __name__ == "__main__":
    main()
