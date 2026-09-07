"""目标持仓生成（L5 决策层）
=====================================
给定合成评分，生成某日的目标持仓：
评分 → top N → 权重优化（逆波动等）→ 个股权重上限 → 行业权重上限
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import get_logger
from ..data.store import Store
from ..optimize.weights import optimize_weights
from .risk import cap_individual_weights, cap_industry_weights

log = get_logger(__name__)


def _industry_map(store: Store) -> dict:
    """code → industry（一级行业），无行业数据返回空"""
    df = store.q("SELECT code, industry FROM instruments WHERE industry IS NOT NULL")
    if df.empty:
        return {}
    return dict(zip(df["code"], df["industry"]))


def generate_target(score: pd.DataFrame, date=None, n_stocks: int = 100,
                    method: str = "inverse_vol", lookback: int = 60,
                    max_weight: float = 0.05,
                    max_industry_weight: float = 0.30,
                    apply_timing: bool = False) -> pd.DataFrame:
    """生成目标持仓

    返回 DataFrame: code/name/weight/score（weight 已归一，含风控约束）。
    apply_timing=True 时按中证1000 均线择时缩放总仓位（默认关闭——
    样本外验证显示择时易过拟合，无择时策略样本外更稳健）。
    """
    store = Store()
    score["date"] = pd.to_datetime(score["date"])
    if date is None:
        # 取覆盖最广的最近交易日（容忍个别股票水位超前导致最新日稀疏）
        cov = score.groupby("date")["code"].nunique()
        total = score["code"].nunique()
        recent = cov[cov >= total * 0.8].index
        date = recent.max() if len(recent) else score["date"].max()
    else:
        date = pd.to_datetime(date)

    s = score[score["date"] == date].sort_values("score", ascending=False)
    if s.empty:
        raise RuntimeError(f"评分在 {date.date()} 无截面数据")
    top = s.head(n_stocks)
    codes = top["code"].tolist()

    # 权重：用截至 date 的历史收益算协方差优化
    px = store.q(
        "SELECT date, code, close*adj_factor AS c FROM kline_daily WHERE date <= ?",
        [date.date()])
    px["date"] = pd.to_datetime(px["date"])
    pmat = px.pivot(index="date", columns="code", values="c").sort_index()
    rmat = pmat.pct_change()
    hist = rmat.loc[:, codes].iloc[-(lookback + 1):-1]
    keep = [c for c in codes if hist[c].isna().mean() < 0.3]
    hist = hist[keep].fillna(0.0)
    w = optimize_weights(method, hist.values)

    holdings = pd.DataFrame({"code": keep, "weight": w})
    holdings = holdings.merge(
        top[["code", "score"]], on="code", how="left")

    # 风控：个股权重上限
    holdings["weight"] = cap_individual_weights(
        holdings["weight"], max_weight)
    # 风控：行业权重上限
    ind_map = _industry_map(store)
    if ind_map:
        holdings = cap_industry_weights(holdings, ind_map, max_industry_weight)

    # 名称
    names = store.q("SELECT code, name FROM instruments")
    holdings = holdings.merge(names, on="code", how="left")
    holdings = holdings.sort_values("weight", ascending=False).reset_index(drop=True)

    # 大盘择时：按中证1000 均线缩放总仓位
    if apply_timing:
        from .timing import market_timing
        pos = market_timing()
        if not pos.empty:
            total_pos = float(pos.iloc[-1]["position"])
            holdings["weight"] = holdings["weight"] * total_pos
    return holdings[["code", "name", "weight", "score"]]
