"""严重异动策略回测驱动：信号定义 → 组合回测 → 分年报表。

运行前提：reports/_tmp/abnormal_events.parquet 已含资金流列（main_net_pct 等）。

策略变体（全部 T+1 开盘入场、T+1 一字板剔除、北交所剔除）
----------------------------------------------------------
- BASE_UP / BASE_DOWN：无资金流条件的事件基线（对照组）
- S1_思路1：DOWN & main_net_pct>0 & small_net_pct<0
- S1s_思路1强化：DOWN & main_net_pct>=5% & small_net_pct<=-5%
- S2_思路2：(DOWN|SWING) & turnover>=25% & main_net_pct>0
- S3_组合：S2 ∩ 排除 streak>=3（连续异动风控）
- 持有期变体：5/10/20 日；止损变体：-8%
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from abnormal_backtest import BTConfig, build_trades, run_portfolio, yearly_table  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "reports" / "_tmp"


def streak_n(ev: pd.DataFrame) -> pd.Series:
    """UP 方向连续异动次数（gap>3 自然日断开）；全方向通用计算。"""
    ev = ev.sort_values(["code", "date"])
    g = ev.groupby("code")["date"]
    prev = g.shift(1)
    gap = (ev["date"] - prev).dt.days
    brk = gap.isna() | (gap > 3)
    seg = brk.cumsum()
    return ev.groupby(seg).cumcount() + 1


def build_variants(ev: pd.DataFrame) -> dict[str, pd.DataFrame]:
    out = {}
    out["BASE_UP"] = ev[ev["direction"] == "UP"]
    out["BASE_DOWN"] = ev[ev["direction"] == "DOWN"]
    down = ev[ev["direction"] == "DOWN"]
    out["S1_主力进散户出"] = down[(down["main_net_pct"] > 0) & (down["small_net_pct"] < 0)]
    out["S1s_强化5pct"] = down[(down["main_net_pct"] >= 0.05) & (down["small_net_pct"] <= -0.05)]
    out["S2_高换手大承接"] = ev[
        ev["direction"].isin(["DOWN", "SWING"])
        & (ev["turnover"] >= 0.25)
        & (ev["main_net_pct"] > 0)
    ]
    # streak（UP 连续异动）作为风控
    st = streak_n(ev).reindex(ev.index)
    ev2 = ev.assign(streak=st)
    out["S3_S2除连板"] = ev2[
        ev2["direction"].isin(["DOWN", "SWING"])
        & (ev2["turnover"] >= 0.25)
        & (ev2["main_net_pct"] > 0)
        & (ev2["streak"] < 3)
    ]
    # 拉萨席位风控变体：需要席位数据 join（可选）
    return out, ev2


def main() -> None:
    ev = pd.read_parquet(TMP / "abnormal_events.parquet")
    if "main_net_pct" not in ev.columns or ev["main_net_pct"].notna().sum() == 0:
        print("事件表缺资金流列，先跑 abnormal_event_study.py")
        return
    dp = pd.read_parquet(TMP / "daily_panel.parquet",
                         columns=["date", "code", "open", "high", "low", "close",
                                  "ret", "adj_factor", "vol_ratio"])
    variants, ev2 = build_variants(ev)

    results = {}
    for name, sub in variants.items():
        sub = sub.copy()
        sub["score"] = sub["main_net_pct"].fillna(0)
        for hold in [10]:
            for sl in [None, -0.08]:
                cfg = BTConfig(hold_days=hold, stop_loss=sl,
                               slip_bp=20.0, max_pos=None, cost_on=True)
                trades = build_trades(sub, dp, cfg)
                res = run_portfolio(trades, cfg)
                if "error" in res:
                    print(f"{name} hold={hold} sl={sl}: {res['error']}")
                    continue
                yt = yearly_table(res)
                key = f"{name}|hold{hold}|{'sl8' if sl else 'nosl'}"
                results[key] = {
                    "n_trades": res["n_trades"], "ann": res["ann_ret"],
                    "dd": res["max_dd"], "sharpe": res["sharpe"],
                    "avg_pos": res["avg_pos"],
                    "yearly": {int(y): float(v) for y, v in yt.items()},
                }
                print(f"{key:>34}: n={res['n_trades']:>5} 年化={res['ann_ret']:+.1%} "
                      f"回撤={res['max_dd']:.1%} 夏普={res['sharpe']:.2f} "
                      f"年均仓={res['avg_pos']:.1f}")
                # 分年
                print("    " + "  ".join(f"{y}:{v:+.1%}" for y, v in yt.items()))

    # 成本敏感性（S2 10日 无止损）：0 / 25 / 50 bp 单边
    print("\n=== 成本敏感性（S2, hold10, 无止损）===")
    sub = variants["S2_高换手大承接"].copy()
    sub["score"] = sub["main_net_pct"].fillna(0)
    for slip in [0.0, 10.0, 20.0, 30.0]:
        cfg = BTConfig(hold_days=10, stop_loss=None, slip_bp=slip,
                       max_pos=None, cost_on=True)
        res = run_portfolio(build_trades(sub, dp, cfg), cfg)
        if "error" not in res:
            print(f"  滑点{slip:.0f}bp/边: 年化={res['ann_ret']:+.2%} "
                  f"夏普={res['sharpe']:.2f} 回撤={res['max_dd']:.1%}")

    # 容量估算：事件股成交额分布
    for name in ["S2_高换手大承接", "S1_主力进散户出"]:
        sub = variants[name]
        med_amt = sub["amount"].median() / 1e8
        print(f"  {name}: 事件日成交额中位 {med_amt:.1f} 亿，"
              f"5% 参与率单仓 ~{med_amt*0.05*1e4:.0f} 万")

    # ── D1 探索性测试：DOWN 事件 + T+1 缩量确认（T+2 开盘入场）──
    # 信号预验证：T+1 量比 1.2-1.7 组 exec_10 = +2.87%（分位先验，非调优）
    # 实现口径：把事件日平移到 T+1（确认日），引擎自动以 T+2 开盘入场
    print("\n=== D1 探索：DOWN + T+1 缩量确认（T+2 开盘入场，样本内）===")
    dp_sorted = dp.sort_values(["code", "date"])
    gg = dp_sorted.groupby("code", sort=False)
    dp_sorted["t1_volratio"] = gg["vol_ratio"].shift(-1)
    dp_sorted["t1_date"] = gg["date"].shift(-1)
    dn = ev[ev["direction"] == "DOWN"][["date", "code"]].copy()
    dn = dn.merge(
        dp_sorted[["date", "code", "t1_volratio", "t1_date"]],
        on=["date", "code"], how="left")
    dn = dn.dropna(subset=["t1_volratio", "t1_date"])
    for label, lo, hi in [("缩量确认(量比1.2-1.7)", 1.2, 1.7),
                          ("温和缩量(量比<1.2)", 0.0, 1.2)]:
        sub = dn[(dn["t1_volratio"] >= lo) & (dn["t1_volratio"] < hi)].copy()
        sub = sub.rename(columns={"date": "ev_date_orig", "t1_date": "date"})
        sub["main_net_pct"] = 0.0  # 引擎 score 占位
        sub["turnover"] = 0.0
        sub["direction"] = "DOWN"
        sub["entry_blocked"] = False
        sub["amount"] = 1e8
        for hold in [10]:
            cfg = BTConfig(hold_days=hold, stop_loss=None, slip_bp=20.0,
                           max_pos=None, cost_on=True)
            res = run_portfolio(build_trades(sub, dp, cfg), cfg)
            if "error" not in res:
                yt = yearly_table(res)
                print(f"  {label}|hold{hold}: n={res['n_trades']:>5} "
                      f"年化={res['ann_ret']:+.1%} 回撤={res['max_dd']:.1%} "
                      f"夏普={res['sharpe']:.2f}")
                print("    " + "  ".join(f"{y}:{v:+.1%}" for y, v in yt.items()))

    with open(TMP / "backtest_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n结果 → {TMP / 'backtest_results.json'}")


if __name__ == "__main__":
    main()
