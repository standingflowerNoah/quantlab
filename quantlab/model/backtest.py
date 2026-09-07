"""截面选股回测引擎 + 绩效指标（L3 模型层）
=====================================
流程：给定截面评分 score(date/code/score)，每隔 rebalance 个交易日，
选评分前 n_stocks 只等权持有到下一调仓日，扣除交易成本后累计净值。

成本模型（config.py）：佣金万2.5 + 印花税千1(卖) + 滑点10bp，
按换手率（相邻两期持仓重叠度）计入每次调仓。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .. import config
from ..config import get_logger
from ..data.store import Store

log = get_logger(__name__)

TRADING_DAYS = 252

# 单次完整换手的成本（买入佣金+滑点 + 卖出佣金+印花税+滑点）
COST_PER_TURNOVER = (config.COMMISSION + config.SLIPPAGE_BASE
                     + config.COMMISSION + config.STAMP_TAX
                     + config.SLIPPAGE_BASE)


def performance(nav: pd.Series, period_days: int = 1) -> dict:
    """净值序列 → 绩效指标（period_days = 每期对应的交易日数，用于年化）"""
    nav = nav.dropna()
    ret = nav.pct_change().dropna()
    n = len(nav)
    years = n * period_days / TRADING_DAYS
    total = float(nav.iloc[-1] / nav.iloc[0] - 1)
    ann = float((nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1) if years > 0 else 0.0
    vol = float(ret.std() * np.sqrt(TRADING_DAYS / period_days)) if len(ret) > 1 else 0.0
    rf = 0.02
    sharpe = float((ann - rf) / vol) if vol > 0 else np.nan
    dd = (nav / nav.cummax() - 1)
    mdd = float(dd.min())
    calmar = float(ann / abs(mdd)) if mdd < 0 else np.nan
    return {
        "total_return": round(total, 4),
        "annual_return": round(ann, 4),
        "annual_vol": round(vol, 4),
        "sharpe": round(sharpe, 3),
        "max_drawdown": round(mdd, 4),
        "calmar": round(calmar, 3),
        "n_periods": n,
    }


def excess_metrics(curve: pd.DataFrame, period_days: int = 20) -> dict:
    """超额收益指标（组合 vs 基准）：信息比率 / 超额年化 / 超额波动 / 超额回撤"""
    excess = (curve["ret"] - curve["bench"]).dropna()
    n = len(excess)
    years = n * period_days / TRADING_DAYS
    nav_ex = (1 + excess).cumprod()
    ann = float((nav_ex.iloc[-1]) ** (1 / years) - 1) if years > 0 else 0.0
    vol = float(excess.std() * np.sqrt(TRADING_DAYS / period_days)) if n > 1 else 0.0
    ir = float(ann / vol) if vol > 0 else np.nan
    dd = (nav_ex / nav_ex.cummax() - 1)
    return {
        "excess_annual": round(ann, 4),
        "excess_vol": round(vol, 4),
        "information_ratio": round(ir, 3),
        "excess_max_drawdown": round(float(dd.min()), 4),
        "win_rate_vs_bm": round(float((excess > 0).mean()), 3),
    }


def _benchmark_returns(pmat: pd.DataFrame, store, benchmark: str) -> pd.Series:
    """基准日收益序列（对齐到股价矩阵的交易日索引）

    benchmark = "equal"  → 全A等权日收益（旧口径，对小市值策略会虚增超额）
    benchmark = 指数代码 → index_kline 中该指数的日收益（如 000852.SH 中证1000）
    """
    if benchmark == "equal":
        return pmat.pct_change().mean(axis=1)
    idx = store.q("SELECT date, close FROM index_kline WHERE code = ?",
                  [benchmark])
    if idx.empty:
        log.warning(f"基准指数 {benchmark} 无数据，退回全A等权")
        return pmat.pct_change().mean(axis=1)
    idx["date"] = pd.to_datetime(idx["date"])
    s = idx.set_index("date")["close"].pct_change()
    return s.reindex(pmat.index)


def run_backtest(score: pd.DataFrame, n_stocks: int = 50,
                 rebalance: int = 20, start=None, end=None,
                 benchmark: str = "000852.SH",
                 tradable: str | None = None) -> dict:
    """截面选股回测

    参数 benchmark: "000852.SH"(中证1000，默认，小市值策略的诚实基准)
                    / "equal"(全A等权) / 其他 index_kline 指数代码
      tradable: 可成交性约束（默认 None=不约束，保持历史口径可比）
        - "sealed": 剔除建仓日涨停封板股（close==high 且涨幅>7%）
        - "hot":    更严格，剔除建仓日涨幅>7% 的全部股票（涨停追高不可行）
    返回 dict:
        curve    DataFrame: date/portfolio/benchmark/nav/nav_bm
        metrics  dict: 组合绩效指标
        metrics_bm dict: 基准绩效指标
        turnover_avg: 平均单次换手率
    """
    store = Store()
    score["date"] = pd.to_datetime(score["date"])
    if start is not None:
        score = score[score["date"] >= pd.to_datetime(start)]
    if end is not None:
        score = score[score["date"] <= pd.to_datetime(end)]

    # 前复权价宽表（date × code）
    px = store.q(
        "SELECT date, code, close*adj_factor AS c FROM kline_daily")
    px["date"] = pd.to_datetime(px["date"])
    pmat = px.pivot(index="date", columns="code", values="c").sort_index()

    # 可成交性约束：预计算调仓日的封板/大涨股票集合
    blocked: set = set()
    if tradable in ("sealed", "hot"):
        dates = sorted(score["date"].unique())
        rb_all = ",".join("'" + str(pd.Timestamp(d).date()) + "'"
                          for d in dates[::rebalance])
        sx = store.q(f"""
            SELECT date, code, high, close,
                   (close - LAG(close) OVER (PARTITION BY code ORDER BY date))
                   / NULLIF(LAG(close) OVER (PARTITION BY code ORDER BY date), 0) AS pct
            FROM kline_daily WHERE date IN ({rb_all})
        """)
        sx["date"] = pd.to_datetime(sx["date"])
        if tradable == "sealed":
            m = (sx["close"] >= sx["high"] - 1e-9) & (sx["pct"] > 0.07)
        else:  # hot
            m = sx["pct"] > 0.07
        blocked = set(zip(sx.loc[m, "date"], sx.loc[m, "code"]))
        log.info(f"可成交性约束({tradable}): 调仓日不可买样本 {len(blocked)} 个")

    # 基准日收益（默认中证1000）
    bench_ret = _benchmark_returns(pmat, store, benchmark)

    dates = sorted(score["date"].unique())
    rb_dates = dates[::rebalance]
    if len(rb_dates) < 2:
        raise RuntimeError("rebalance 期数不足（检查因子样本区间）")

    prev_top: list[str] = []
    rows = []
    turnover_sum = 0.0
    n_periods = 0
    for i in range(len(rb_dates) - 1):
        d0, d1 = rb_dates[i], rb_dates[i + 1]
        if d0 not in pmat.index or d1 not in pmat.index:
            continue
        s = score[score["date"] == d0].sort_values(
            "score", ascending=False)
        if tradable:
            s = s[[(d0, c) not in blocked for c in s["code"]]]
        top = s.head(n_stocks)["code"].tolist()
        # 换手率：与上期持仓的重叠度
        if prev_top:
            overlap = len(set(top) & set(prev_top))
            turnover = 1 - overlap / n_stocks
        else:
            turnover = 1.0        # 首期满仓
        turnover_sum += turnover
        n_periods += 1

        # —— 逐日记账：d0 收盘建仓，持有期内每日盯市 ——
        seg = pmat.loc[d0:d1, top].ffill()      # 停牌日沿用最后价格
        p0row = seg.iloc[0]
        usable = [c for c in top
                  if pd.notna(p0row.get(c)) and p0row[c] > 0]
        if not usable:
            continue
        # 各股自 d0 起的累计净值（等权买入持有）
        vals = (1 + seg[usable].pct_change().fillna(0)).cumprod()
        port = vals.mean(axis=1)                 # 等权组合日频净值
        day_ret = port.pct_change().iloc[1:]     # d0 当日不计收益
        # 调仓日（d1）扣除交易成本
        day_ret.iloc[-1] = day_ret.iloc[-1] - turnover * COST_PER_TURNOVER

        for t, r in day_ret.items():
            rows.append({"date": t, "ret": float(r),
                         "bench": float(bench_ret.get(t, np.nan))})
        prev_top = top

    if not rows:
        raise RuntimeError("回测无有效期间")
    curve = pd.DataFrame(rows).dropna(subset=["ret"])
    curve["nav"] = (1 + curve["ret"]).cumprod()
    curve["nav_bm"] = (1 + curve["bench"].fillna(0)).cumprod()

    metrics = performance(curve["nav"], period_days=1)
    metrics_bm = performance(curve["nav_bm"], period_days=1)
    metrics_bm["sharpe"] = metrics_bm.get("sharpe", np.nan)
    excess = excess_metrics(curve, period_days=1)

    return {
        "curve": curve,
        "metrics": metrics,
        "metrics_bm": metrics_bm,
        "excess": excess,
        "turnover_avg": round(turnover_sum / max(n_periods, 1), 4),
        "n_stocks": n_stocks,
        "rebalance": rebalance,
        "benchmark": benchmark,
        "cost_per_turnover": round(COST_PER_TURNOVER, 5),
    }


def run_multiphase_backtest(score: pd.DataFrame, n_stocks: int = 100,
                            rebalance: int = 20, phases: int = 4,
                            start=None, end=None,
                            benchmark: str = "000852.SH",
                            tradable: str | None = None) -> dict:
    """多相位平均回测（调仓网格运气平滑）

    背景：调仓日网格由回测起点相位决定，同参数下起点差一个相位，
    年化可差 10pp+（相位决定暴跌月的买点）。本引擎把资金等分给
    K 个相位错开的子组合（相位间隔 = rebalance // phases 个交易日），
    每个子组合独立按 run_backtest 口径跑（topN、扣费、可成交性约束），
    合并规则：公共区间（最后一个相位起跑后）各子组合日收益等权平均。

    注意：合并隐含"相位间每日再平衡"，实际相位间权重漂移为二阶小量，
    忽略其成本（相位内成本已全额计入）。

    返回 dict（同 run_backtest 字段）+ phases + phase_detail（各相位单独绩效）。
    """
    s = score.copy()
    s["date"] = pd.to_datetime(s["date"])
    if start is not None:
        s = s[s["date"] >= pd.to_datetime(start)]
    if end is not None:
        s = s[s["date"] <= pd.to_datetime(end)]
    dates = sorted(s["date"].unique())
    offset = max(rebalance // phases, 1)
    if (phases - 1) * offset >= len(dates) - 2:
        raise RuntimeError(f"相位参数超样本长度（phases={phases}, "
                           f"offset={offset}, {len(dates)} 天）")

    results, start_dates = [], []
    for k in range(phases):
        d0 = pd.Timestamp(dates[k * offset])
        r = run_backtest(score, n_stocks=n_stocks, rebalance=rebalance,
                         start=str(d0.date()), end=end,
                         benchmark=benchmark, tradable=tradable)
        results.append(r)
        start_dates.append(d0)

    # 公共区间 = 最后一个相位起跑之后（各相位曲线自身从 d0+1 起有收益）
    common_start = start_dates[-1]
    rets = []
    for r in results:
        c = r["curve"][["date", "ret"]].copy()
        rets.append(c[c["date"] > common_start].set_index("date")["ret"])
    combined = pd.concat(rets, axis=1).mean(axis=1).dropna()
    bm = (results[-1]["curve"][["date", "bench"]]
          .set_index("date")["bench"].reindex(combined.index))

    curve = pd.DataFrame({"date": combined.index,
                          "ret": combined.values,
                          "bench": bm.values})
    curve["nav"] = (1 + curve["ret"]).cumprod()
    curve["nav_bm"] = (1 + curve["bench"].fillna(0)).cumprod()

    phase_detail = []
    for k, (r, d0) in enumerate(zip(results, start_dates)):
        m = r["metrics"]
        phase_detail.append({
            "phase": k, "start": str(d0.date()),
            "annual_return": m["annual_return"], "sharpe": m["sharpe"],
            "max_drawdown": m["max_drawdown"],
            "excess_annual": r["excess"]["excess_annual"],
        })

    return {
        "curve": curve,
        "metrics": performance(curve["nav"], period_days=1),
        "metrics_bm": performance(curve["nav_bm"], period_days=1),
        "excess": excess_metrics(curve, period_days=1),
        "turnover_avg": round(float(np.mean([r["turnover_avg"]
                                             for r in results])), 4),
        "n_stocks": n_stocks,
        "rebalance": rebalance,
        "phases": phases,
        "benchmark": benchmark,
        "phase_detail": phase_detail,
        "cost_per_turnover": round(COST_PER_TURNOVER, 5),
    }
