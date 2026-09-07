"""因子分层回测（quantile backtest）+ 多空组合
=====================================
把「因子 IC 有效」落地成可检验的组合收益：
1. 每隔 horizon 个交易日 rebalance 一次（非重叠持有期，避免复利虚高）
2. 截面按因子值 qcut 分 N 层，每层等权持有未来 horizon 日收益
3. 多空组合 = 高因子值层(Qn) - 低因子值层(Q1)，累计净值
4. 输出分层年化收益表 + 单调性 + 多空净值曲线

方向约定：多空收益 = Qn - Q1。正 IC 因子多空为正（做多高值跑赢），
负 IC 因子多空为负（做多高值跑输，反向即赚钱）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import get_logger
from ..data.store import Store
from .quality import _load_factor, _fwd_return

log = get_logger(__name__)

TRADING_DAYS = 252


def factor_backtest(name: str, horizon: int = 5, n_quantiles: int = 5,
                    start=None, end=None, universe=None,
                    min_n: int = 30) -> dict:
    """因子分层回测（非重叠持有期）

    返回 dict:
        summary   DataFrame: 每层持有期数/平均持有期收益/累计/年化/胜率 + 多空
        curve     DataFrame: 多空组合逐期收益与累计净值（date/ls_ret/nav）
        q_curve   DataFrame: 各层累计净值（date/Q1..Qn）
    """
    store = Store()
    fv = _load_factor(name, store)
    if fv.empty:
        raise ValueError(f"因子 {name} 无数据")

    if universe is not None:
        if isinstance(universe, str):
            from ..data.universe import get_universe
            universe = get_universe(universe)
        fv = fv[fv["code"].isin(set(universe))]

    fwd = _fwd_return(horizon)
    m = fv.merge(fwd, on=["date", "code"], how="inner")
    if m.empty:
        raise ValueError(f"因子 {name} 与收益无交集")

    if start is not None:
        m = m[m["date"] >= pd.to_datetime(start)]
    if end is not None:
        m = m[m["date"] <= pd.to_datetime(end)]

    # 非重叠 rebalance 日：每隔 horizon 个交易日取一个截面
    dates = np.sort(m["date"].unique())
    rb_dates = dates[::horizon]
    m = m[m["date"].isin(rb_dates)]

    # 每日截面分位（duplicates=drop 容忍重复值）
    def _qcut(x):
        try:
            return pd.qcut(x, n_quantiles, labels=False, duplicates="drop")
        except Exception:
            return pd.Series(np.nan, index=x.index)

    m["q"] = m.groupby("date")["value"].transform(_qcut)
    m = m.dropna(subset=["q"])
    m["q"] = m["q"].astype(int)

    # 每层每个持有期的等权收益（行=rebalance日，列=层）
    layer = m.groupby(["date", "q"])["fwd"].mean().unstack()
    layer = layer.reindex(columns=range(n_quantiles))

    # 持有期年化：每 horizon 日一个持有期
    periods_per_year = TRADING_DAYS / horizon

    def _ann(r):
        return (1 + r.mean()) ** periods_per_year - 1

    rows = []
    for q in range(n_quantiles):
        r = layer[q].dropna()
        rows.append({
            "quantile": f"Q{q+1}",
            "n_periods": len(r),
            "mean_ret": round(float(r.mean()), 5),
            "ann_ret": round(float(_ann(r)), 4),
            "cum_ret": round(float((1 + r).prod() - 1), 4),
            "win_rate": round(float((r > 0).mean()), 3),
        })
    ls = (layer[n_quantiles - 1] - layer[0]).dropna()
    rows.append({
        "quantile": f"L-S(Q{n_quantiles}-Q1)",
        "n_periods": len(ls),
        "mean_ret": round(float(ls.mean()), 5),
        "ann_ret": round(float(_ann(ls)), 4),
        "cum_ret": round(float((1 + ls).prod() - 1), 4),
        "win_rate": round(float((ls > 0).mean()), 3),
    })
    summary = pd.DataFrame(rows)

    # ── 净值曲线（非重叠持有期） ─────────────────────────────────
    curve = pd.DataFrame({"date": ls.index, "ls_ret": ls.values})
    curve["nav"] = (1 + curve["ls_ret"]).cumprod()

    q_curve = (1 + layer.fillna(0)).cumprod()
    q_curve.insert(0, "date", q_curve.index)
    q_curve = q_curve.rename(columns={q: f"Q{q+1}" for q in range(n_quantiles)})

    # 单调性：分层收益与层序号的秩相关（近 1 表示强单调）
    order = [rows[q]["ann_ret"] for q in range(n_quantiles)]
    monotonic = float(pd.Series(order).rank().corr(
        pd.Series(range(n_quantiles))))

    return {"summary": summary, "curve": curve, "q_curve": q_curve,
            "monotonic_ic": round(monotonic, 4),
            "horizon": horizon, "n_quantiles": n_quantiles,
            "n_periods": len(ls)}
