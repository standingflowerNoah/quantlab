"""D6 组合层回测：DOWN & 20日天量 & 主力净流入（对比 S1s）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from abnormal_backtest import BTConfig, build_trades, run_portfolio, yearly_table  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "reports" / "_tmp"


def main() -> None:
    ev = pd.read_parquet(TMP / "abnormal_events_official_enriched.parquet")
    ev = ev[~ev["is_st"].fillna(False).astype(bool)]
    dp = pd.read_parquet(TMP / "daily_panel.parquet",
                         columns=["date", "code", "open", "high", "low", "close",
                                  "ret", "adj_factor"])

    d6 = ev["is_20d_high_vol"] == True  # noqa: E712
    down = ev["direction"] == "DOWN"
    variants = {
        "S1s(基准对照)": down & (ev["main_net_pct"] >= 0.05) & (ev["small_net_pct"] <= -0.05),
        "D6_天量+主力净买": down & d6 & (ev["main_net_pct"] > 0),
        "D6s_天量+强承接": down & d6 & (ev["main_net_pct"] >= 0.05) & (ev["small_net_pct"] <= -0.05),
        "D6_天量无条件": down & d6,
    }
    for name, mask in variants.items():
        sub = ev[mask].copy()
        sub["score"] = sub["main_net_pct"].fillna(0)
        cfg = BTConfig(hold_days=10, stop_loss=None, slip_bp=20.0,
                       max_pos=None, cost_on=True)
        res = run_portfolio(build_trades(sub, dp, cfg), cfg)
        if "error" in res:
            print(f"{name}: {res['error']}")
            continue
        yt = yearly_table(res)
        print(f"{name:>16}: n={res['n_trades']:>5} 年化={res['ann_ret']:+.1%} "
              f"回撤={res['max_dd']:.1%} 夏普={res['sharpe']:.2f} 年均仓={res['avg_pos']:.1f}")
        print("    " + "  ".join(f"{y}:{v:+.1%}" for y, v in yt.items()))


if __name__ == "__main__":
    main()
