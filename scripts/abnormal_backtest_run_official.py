"""官方口径严重异动策略：组合层可执行回测。

变体（全部 T+1 开盘入场、T+1 一字板剔除、北交所已剔除、非 ST）：
- BASE_UP / BASE_DOWN：官方口径事件基线
- S1：DOWN & main_net_pct>0 & small_net_pct<0（思路1）
- S1s：DOWN & main_net_pct>=5% & small_net_pct<=-5%（思路1 强化档）
- S1m：DOWN & main_net_pct>=10%（主力强承接单调档）
- 持有期 10 日；止损 -8% 变体；成本滑点 20bp/边
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from abnormal_backtest import BTConfig, build_trades, run_portfolio, yearly_table  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "reports" / "_tmp"


def main() -> None:
    ev = pd.read_parquet(TMP / "abnormal_events_official.parquet")
    ev = ev[~ev["is_st"].fillna(False).astype(bool)].copy()
    dp = pd.read_parquet(TMP / "daily_panel.parquet",
                         columns=["date", "code", "open", "high", "low", "close",
                                  "ret", "adj_factor"])

    variants = {
        "BASE_UP": ev[ev["direction"] == "UP"],
        "BASE_DOWN": ev[ev["direction"] == "DOWN"],
        "S1_主力进散户出": ev[(ev["direction"] == "DOWN")
                          & (ev["main_net_pct"] > 0) & (ev["small_net_pct"] < 0)],
        "S1s_强化5pct": ev[(ev["direction"] == "DOWN")
                       & (ev["main_net_pct"] >= 0.05) & (ev["small_net_pct"] <= -0.05)],
        "S1m_主力强承接10pct": ev[(ev["direction"] == "DOWN") & (ev["main_net_pct"] >= 0.10)],
    }
    results = {}
    for name, sub in variants.items():
        sub = sub.copy()
        sub["score"] = sub["main_net_pct"].fillna(0)
        for sl in [None, -0.08]:
            cfg = BTConfig(hold_days=10, stop_loss=sl, slip_bp=20.0,
                           max_pos=None, cost_on=True)
            res = run_portfolio(build_trades(sub, dp, cfg), cfg)
            if "error" in res:
                print(f"{name} sl={sl}: {res['error']}")
                continue
            yt = yearly_table(res)
            key = f"{name}|{'sl8' if sl else 'nosl'}"
            results[key] = {
                "n_trades": res["n_trades"], "ann": res["ann_ret"],
                "dd": res["max_dd"], "sharpe": res["sharpe"],
                "avg_pos": res["avg_pos"],
                "yearly": {int(y): float(v) for y, v in yt.items()},
            }
            print(f"{key:>26}: n={res['n_trades']:>5} 年化={res['ann_ret']:+.1%} "
                  f"回撤={res['max_dd']:.1%} 夏普={res['sharpe']:.2f} "
                  f"年均仓={res['avg_pos']:.1f}")
            print("    " + "  ".join(f"{y}:{v:+.1%}" for y, v in yt.items()))

    # 成本敏感性（S1s）
    print("\n=== 成本敏感性（S1s, hold10, 无止损）===")
    sub = variants["S1s_强化5pct"].copy()
    sub["score"] = sub["main_net_pct"].fillna(0)
    for slip in [0.0, 10.0, 20.0, 40.0]:
        cfg = BTConfig(hold_days=10, stop_loss=None, slip_bp=slip,
                       max_pos=None, cost_on=True)
        res = run_portfolio(build_trades(sub, dp, cfg), cfg)
        if "error" not in res:
            print(f"  滑点{slip:.0f}bp/边: 年化={res['ann_ret']:+.2%} "
                  f"夏普={res['sharpe']:.2f} 回撤={res['max_dd']:.1%}")

    # 持有期敏感性（S1s）
    print("\n=== 持有期敏感性（S1s, 无止损, 滑点20bp）===")
    for hold in [5, 10, 20]:
        cfg = BTConfig(hold_days=hold, stop_loss=None, slip_bp=20.0,
                       max_pos=None, cost_on=True)
        res = run_portfolio(build_trades(sub, dp, cfg), cfg)
        if "error" not in res:
            print(f"  hold{hold}: 年化={res['ann_ret']:+.2%} 夏普={res['sharpe']:.2f} "
                  f"回撤={res['max_dd']:.1%}")

    with open(TMP / "backtest_results_official.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n结果 → {TMP / 'backtest_results_official.json'}")


if __name__ == "__main__":
    main()
