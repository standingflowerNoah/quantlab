"""严重异动事件策略：组合层可执行回测引擎。

执行假设（对齐 A 股现实约束）
------------------------------
- 入场：事件日 T 次日（T+1）开盘价买入；
  T+1 一字涨停开盘（open=high=low 且跳空≥板块限价×0.9）→ 放弃（买不进）。
- 出场：持有 N 日后收盘卖出（基线变体）；
  止损变体：T+2 起每日收盘检查，自入场累计收益 ≤ -SL 则当日收盘卖出
  （T+1 买入当日不可卖= T+1 制度；近似忽略跌停无法卖出，报告中声明）。
- 成本：买=佣金2.5bp+滑点S；卖=佣金2.5bp+印花5bp+滑点S。S 默认 20bp/边。
- 仓位：等权；变体1=日历组合（无仓位上限，所有开放事件等权）；
  变体2=并发上限 MAX_POS，超出按信号强度（score）择优。

组合构造
--------
日历时间组合：每日组合收益 = 当日所有持仓事件当日收益的等权均值。
单事件持仓期收益序列：[close_{T+1}/open_{T+1}-1] + [ret_{T+2..T+N}]。
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "reports" / "_tmp"


@dataclass
class BTConfig:
    hold_days: int = 10            # 持有期（交易日）
    stop_loss: float | None = None  # 止损（如 -0.08）；None=不止损
    slip_bp: float = 20.0          # 单边滑点 bp
    fee_buy: float = 0.00025       # 佣金
    fee_sell: float = 0.00025 + 0.0005  # 佣金+印花
    max_pos: int | None = None     # 并发仓位上限；None=日历组合
    cost_on: bool = True


def _daily_ret_matrix(dp: pd.DataFrame) -> pd.DataFrame:
    """(date × code) 当日收益矩阵：T 行 = close_T/open_T-1?? 不——直接用 ret 列透视。"""
    return dp.pivot_table(index="date", columns="code", values="ret", aggfunc="first")


def build_trades(ev: pd.DataFrame, dp: pd.DataFrame, cfg: BTConfig,
                 score_col: str = "score") -> pd.DataFrame:
    """构造逐事件持仓期日收益（T+1 起每日收益），含入场可执行性过滤。"""
    # 事件必要列
    need = ["date", "code", "direction", "entry_blocked"]
    df = ev.copy()
    df = df[~df["entry_blocked"].fillna(False)]
    # 北交所剔除（主口径）：92/43/83/87 开头
    df = df[~df["code"].str.startswith(("92", "43", "83", "87"))]
    # score 缺失置 0（不排序时无影响）
    if score_col not in df.columns:
        df[score_col] = 0.0
    df = df.dropna(subset=["date"]).sort_values(["date", score_col]).reset_index(drop=True)

    # 日线快查结构: (code -> DataFrame indexed by date)
    dpg = {c: g.set_index("date") for c, g in dp.groupby("code")}

    trades = []
    for _, e in df.iterrows():
        g = dpg.get(e["code"])
        if g is None:
            continue
        idx = g.index.get_indexer([e["date"]], method="nearest")
        i = idx[0]
        if i < 0 or i + 1 >= len(g):
            continue
        t1 = g.index[i + 1]
        row_t1 = g.iloc[i + 1]
        # 入场收益（T+1 开盘 → T+1 收盘）
        if not (row_t1["open"] > 0):
            continue
        r0 = row_t1["close"] / row_t1["open"] - 1
        # 后续日收益
        rets = []
        for j in range(i + 2, min(i + 1 + cfg.hold_days, len(g))):
            rets.append((g.index[j], g.iloc[j]["ret"]))
        # T+N 收盘卖出：ret 序列长度 = hold_days-1（T+2..T+N）
        days = [(t1, r0)] + rets
        if len(days) < cfg.hold_days:
            # 数据不足（样本尾部）→ 截断（右删失：仍计入，仓位提前平仓）
            pass
        trades.append({
            "code": e["code"], "ev_date": e["date"], "entry_date": t1,
            "direction": e["direction"], "score": e[score_col],
            "days": days,
        })
    return pd.DataFrame(trades)


def run_portfolio(trades: pd.DataFrame, cfg: BTConfig) -> dict:
    """日历组合模拟（可含并发上限与止损）。"""
    if trades.empty:
        return {"error": "no trades"}
    # 展开为 (trade_id, date, day_ret)
    rows = []
    for tid, tr in trades.iterrows():
        cum = 1.0
        for k, (d, r) in enumerate(tr["days"]):
            cum *= (1 + (r if pd.notna(r) else 0))
            rows.append({"tid": tid, "date": d, "k": k, "ret": r,
                         "cum": cum, "code": tr["code"],
                         "entry_date": tr["entry_date"], "score": tr["score"]})
    pos = pd.DataFrame(rows)

    # 止损：k>=1（T+2 起）且 cum-1 <= stop_loss → 该日收盘退出，其后剔除
    if cfg.stop_loss is not None:
        stop_hit = (pos["k"] >= 1) & (pos["cum"] - 1 <= cfg.stop_loss)
        stop_tids = pos.loc[stop_hit, ["tid", "date"]].groupby("tid")["date"].min()
        keep = []
        for tid, tr in trades.iterrows():
            if tid in stop_tids.index:
                sd = stop_tids[tid]
                keep.append([x for x in tr["days"] if x[0] <= sd])
            else:
                keep.append(tr["days"])
        trades = trades.assign(days=keep)
        rows = []
        for tid, tr in trades.iterrows():
            cum = 1.0
            for k, (d, r) in enumerate(tr["days"]):
                cum *= (1 + (r if pd.notna(r) else 0))
                rows.append({"tid": tid, "date": d, "k": k, "ret": r, "cum": cum})
        pos = pd.DataFrame(rows)

    # 并发上限：逐日贪心，按 score 择优保留（预计算每笔结束日，避免 O(n²)）
    if cfg.max_pos is not None:
        end_of = pos.groupby("tid")["date"].max().to_dict()
        by_entry = trades.groupby("entry_date")
        all_dates = pd.DatetimeIndex(sorted(pos["date"].unique()))
        open_set: set = set()
        chosen: set = set()
        for d in all_dates:
            open_set = {tid for tid in open_set if end_of.get(tid) >= d}
            if d in by_entry.groups:
                cands = by_entry.get_group(d)
                cands = cands.sort_values("score", ascending=False)
                for tid in cands.index:
                    if len(open_set) >= cfg.max_pos:
                        break
                    open_set.add(tid)
                    chosen.add(tid)
        pos = pos[pos["tid"].isin(chosen)]
        trades = trades[trades.index.isin(chosen)]

    # 日收益：当日开放持仓等权
    daily = pos.groupby("date").agg(ret=("ret", "mean"), n_pos=("tid", "nunique"))

    # 成本：入场日/出场日按持仓事件均摊
    cost_side = (cfg.slip_bp + cfg.fee_buy * 1e4) / 1e4 if cfg.cost_on else 0.0
    cost_sell = (cfg.slip_bp + cfg.fee_sell * 1e4) / 1e4 if cfg.cost_on else 0.0
    entry_costs = {}
    exit_costs = {}
    active_tids = set(pos["tid"].unique())
    for tid, tr in trades.iterrows():
        if tid not in active_tids:
            continue
        ds = [x[0] for x in tr["days"]]
        entry_costs[ds[0]] = entry_costs.get(ds[0], 0) + 1
        exit_costs[ds[-1]] = exit_costs.get(ds[-1], 0) + 1
    n_open = pos.groupby("date")["tid"].nunique()
    adj_cost = pd.Series(0.0, index=daily.index)
    for d, cnt in entry_costs.items():
        if d in adj_cost.index:
            adj_cost[d] += cnt / n_open[d] * cost_side
    for d, cnt in exit_costs.items():
        if d in adj_cost.index:
            adj_cost[d] += cnt / n_open[d] * cost_sell
    daily["net_ret"] = daily["ret"] - adj_cost

    eq = (1 + daily["net_ret"]).cumprod()
    yrs = (daily.index.max() - daily.index.min()).days / 365.25
    ann = eq.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else np.nan
    dd = (eq / eq.cummax() - 1).min()
    sharpe = daily["net_ret"].mean() / daily["net_ret"].std() * np.sqrt(244) \
        if daily["net_ret"].std() > 0 else np.nan
    return {
        "n_trades": pos["tid"].nunique(),
        "days": len(daily),
        "ann_ret": ann, "max_dd": dd, "sharpe": sharpe,
        "total": eq.iloc[-1] - 1,
        "avg_pos": daily["n_pos"].mean(),
        "daily": daily, "eq": eq,
    }


def yearly_table(res: dict) -> pd.DataFrame:
    d = res["daily"].copy()
    d["year"] = d.index.year
    g = d.groupby("year")["net_ret"].apply(lambda x: (1 + x).prod() - 1)
    return g


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mask", default="all", help="事件子集: all/up/down/g1/g2 …")
    args = ap.parse_args()
    ev = pd.read_parquet(TMP / "abnormal_events.parquet")
    dp = pd.read_parquet(TMP / "daily_panel.parquet",
                         columns=["date", "code", "open", "high", "low", "close",
                                  "ret", "adj_factor"])
    print(f"事件 {len(ev)}，日线 {len(dp):,}")


if __name__ == "__main__":
    main()
