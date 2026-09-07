#!/usr/bin/env python3
"""Alpha191 融合方式实验：成本归因 / 加权融合 / 池内精选
=====================================
E1 零成本归因：alpha191 top20 无交易成本回测（验证成本损耗假设）
E2 加权融合：final = w*rank(size+amihud) + (1-w)*rank(alpha191)
E3 池内精选：size+amihud 选 400 只候选池 → alpha191 在池内精选 top100
输出: reports/alpha191_fusion.json
"""
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import json

import pandas as pd

from quantlab.config import REPORTS_DIR, get_logger
from quantlab.model import build_composite, run_backtest
from quantlab.model import backtest as btmod

log = get_logger("a191_fuse")

START = "2022-07-01"


def main():
    reps = pd.read_csv(REPORTS_DIR / "alpha191_representatives.csv")
    reps["aic"] = reps["icir20"].abs()
    top20 = reps.nlargest(20, "aic")["factor"].tolist()

    score_sa = build_composite(["size", "amihud_20"], universe="ashare_ex")
    score_a = build_composite(top20, universe="ashare_ex")
    sa2 = score_sa.rename(columns={"score": "score_sa"})
    a2 = score_a.rename(columns={"score": "score_a"})

    def mix(w_sa: float) -> pd.DataFrame:
        m = sa2.merge(a2, on=["date", "code"], how="inner")
        m["r_sa"] = m.groupby("date")["score_sa"].rank(pct=True)
        m["r_a"] = m.groupby("date")["score_a"].rank(pct=True)
        m["score"] = w_sa * m["r_sa"] + (1 - w_sa) * m["r_a"]
        return m[["date", "code", "score"]]

    def pool_refine(pool_n: int = 400, n: int = 100) -> pd.DataFrame:
        rows = []
        for d, g in score_sa.groupby("date"):
            pool = g.nlargest(pool_n, "score")["code"]
            ga = (score_a[score_a["date"] == d]
                  .set_index("code")["score"])
            ranks = ga.reindex(pool).dropna().rank(pct=True)
            full = pd.Series(0.0, index=g["code"].values)
            full.loc[ranks.index] = ranks.values
            rows.extend((d, c, s) for c, s in full.items())
        return pd.DataFrame(rows, columns=["date", "code", "score"])

    out = {}
    # E1 零成本归因
    cost0 = btmod.COST_PER_TURNOVER
    btmod.COST_PER_TURNOVER = 0.0
    bt = run_backtest(score_a, n_stocks=100, rebalance=20, start=START)
    out["a191_top20_nocost"] = {"metrics": bt["metrics"],
                                "excess": bt["excess"],
                                "turnover": bt["turnover_avg"]}
    btmod.COST_PER_TURNOVER = cost0
    log.info(f"E1 零成本 a191top20: ann={bt['metrics']['annual_return']:+.1%}")

    # E2 加权融合
    for w in (0.6, 0.7, 0.8):
        bt = run_backtest(mix(w), n_stocks=100, rebalance=20, start=START)
        out[f"mix_w{w}"] = {"metrics": bt["metrics"], "excess": bt["excess"],
                            "turnover": bt["turnover_avg"]}
        log.info(f"E2 w={w}: ann={bt['metrics']['annual_return']:+.1%} "
                 f"sharpe={bt['metrics']['sharpe']} to={bt['turnover_avg']:.1%}")

    # E3 池内精选
    bt = run_backtest(pool_refine(400, 100), n_stocks=100,
                      rebalance=20, start=START)
    out["pool_refine_400"] = {"metrics": bt["metrics"], "excess": bt["excess"],
                              "turnover": bt["turnover_avg"]}
    log.info(f"E3 池内精选: ann={bt['metrics']['annual_return']:+.1%} "
             f"sharpe={bt['metrics']['sharpe']} to={bt['turnover_avg']:.1%}")

    # 对照
    bt = run_backtest(score_sa, n_stocks=100, rebalance=20, start=START)
    out["size_amihud_ref"] = {"metrics": bt["metrics"], "excess": bt["excess"],
                              "turnover": bt["turnover_avg"]}

    json.dump(out, open(REPORTS_DIR / "alpha191_fusion.json", "w",
                        encoding="utf-8"), ensure_ascii=False, default=str)
    print()
    for k, v in out.items():
        m, e = v["metrics"], v["excess"]
        print(f"{k:22s} 年化={m['annual_return']:+.1%} 夏普={m['sharpe']} "
              f"回撤={m['max_drawdown']:.1%} IR={e['information_ratio']} "
              f"换手={v.get('turnover', 0):.1%}")


if __name__ == "__main__":
    main()
