"""权重优化回测（L4 优化层）
=====================================
在 top N 选股基础上，每期用历史收益矩阵优化权重（等权/逆波动/
最小方差/风险平价），替代等权持有，观察夏普与回撤的改善。

复用 L3 model.backtest 的 performance 与成本模型。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import get_logger
from ..data.store import Store
from ..model.backtest import performance, COST_PER_TURNOVER, _benchmark_returns
from .weights import optimize_weights

log = get_logger(__name__)


def run_optimized_backtest(score: pd.DataFrame, n_stocks: int = 100,
                           rebalance: int = 40, method: str = "inverse_vol",
                           lookback: int = 60, start=None, end=None,
                           benchmark: str = "000852.SH",
                           pmat: pd.DataFrame | None = None) -> dict:
    """权重优化选股回测

    参数:
        method: equal / inverse_vol / min_var / risk_parity
        lookback: 协方差估计用的历史日数
        benchmark: "000852.SH"(中证1000，默认) / "equal"(全A等权) / 指数代码
        pmat: 预计算的后复权价格宽表（多模型批量回测时共享，省去重复读表）
    （默认 40 日调仓 + inverse_vol，经 IC 衰减与调仓网格实测最优）
    """
    store = Store()
    score["date"] = pd.to_datetime(score["date"])
    if start is not None:
        score = score[score["date"] >= pd.to_datetime(start)]
    if end is not None:
        score = score[score["date"] <= pd.to_datetime(end)]

    if pmat is None:
        px = store.q("SELECT date, code, close*adj_factor AS c FROM kline_daily")
        px["date"] = pd.to_datetime(px["date"])
        pmat = px.pivot(index="date", columns="code", values="c").sort_index()
    rmat = pmat.pct_change()

    bench_ret = _benchmark_returns(pmat, store, benchmark)

    dates = sorted(score["date"].unique())
    rb_dates = dates[::rebalance]
    if len(rb_dates) < 2:
        raise RuntimeError("rebalance 期数不足")

    prev_w = None
    prev_codes: list[str] = []
    turnover_sum = 0.0
    n_periods = 0
    rows = []
    for i in range(len(rb_dates) - 1):
        d0, d1 = rb_dates[i], rb_dates[i + 1]
        if d0 not in pmat.index or d1 not in pmat.index:
            continue
        s = score[score["date"] == d0].sort_values("score", ascending=False)
        top = s.head(n_stocks)["code"].tolist()

        # 历史收益矩阵（截至 d0 前一交易日，过去 lookback 日）
        hist = rmat.loc[:d0, top].iloc[-(lookback + 1):-1]
        if len(hist) < 20:
            continue          # 早期 rebalance 日历史不足，跳过
        nan_ratio = hist.isna().mean()
        valid = [c for c in top if nan_ratio.get(c, 1.0) < 0.3]
        if len(valid) < 5:
            valid = top
        hist = hist[valid].fillna(0.0)

        w = optimize_weights(method, hist.values)
        w = np.asarray(w, dtype=float)

        # —— 逐日记账：d0 收盘建仓，持有期内每日盯市 ——
        seg = pmat.loc[d0:d1, valid].ffill()    # 停牌日沿用最后价格
        p0row = seg.iloc[0]
        usable_idx = [j for j, c in enumerate(valid)
                      if pd.notna(p0row.get(c)) and p0row[c] > 0]
        if not usable_idx:
            continue
        usable = [valid[j] for j in usable_idx]
        wu = w[usable_idx]
        wu = wu / wu.sum()                       # 剔除不可交易股后归一化

        # 各股自 d0 起的累计净值（买入持有，权重随价格漂移）
        vals = (1 + seg[usable].pct_change().fillna(0)).cumprod()
        port = (vals * wu).sum(axis=1)           # 组合日频净值
        day_ret = port.pct_change().iloc[1:]     # d0 当日不计收益

        # 换手 = 权重变化 L1/2（对期初目标权重）
        if prev_w is not None:
            w_old = np.zeros(len(usable))
            for j, c in enumerate(usable):
                if c in prev_codes:
                    w_old[j] = prev_w[prev_codes.index(c)]
            turnover = float(np.abs(wu - w_old).sum() / 2)
        else:
            turnover = 1.0
        turnover_sum += turnover
        n_periods += 1

        # 调仓日（d1）扣除交易成本
        day_ret.iloc[-1] = day_ret.iloc[-1] - turnover * COST_PER_TURNOVER

        for t, r in day_ret.items():
            rows.append({"date": t, "ret": float(r),
                         "bench": float(bench_ret.get(t, np.nan))})
        prev_w, prev_codes = wu, usable

    if not rows:
        raise RuntimeError("回测无有效期间")
    curve = pd.DataFrame(rows).dropna(subset=["ret"])
    curve["nav"] = (1 + curve["ret"]).cumprod()
    curve["nav_bm"] = (1 + curve["bench"].fillna(0)).cumprod()

    from ..model.backtest import excess_metrics
    return {
        "curve": curve,
        "metrics": performance(curve["nav"], period_days=1),
        "metrics_bm": performance(curve["nav_bm"], period_days=1),
        "excess": excess_metrics(curve, period_days=1),
        "turnover_avg": round(turnover_sum / max(n_periods, 1), 4),
        "method": method, "n_stocks": n_stocks, "lookback": lookback,
        "benchmark": benchmark,
    }
