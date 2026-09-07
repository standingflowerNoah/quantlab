"""风控约束（L5 决策层）
=====================================
对目标组合施加约束：
- 个股权重上限（超出部分按比例分配给未超限股票）
- 行业权重上限（需要 instruments.industry 数据）
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def cap_individual_weights(weights: pd.Series, max_weight: float) -> pd.Series:
    """个股权重上限：迭代把超限部分按比例分配给未超限股票"""
    w = weights.astype(float).copy()
    if max_weight <= 0 or w.empty:
        return w
    for _ in range(20):
        over = w[w > max_weight]
        if over.empty:
            break
        excess = float((over - max_weight).sum())
        under = w[w <= max_weight]
        if under.empty or under.sum() <= 1e-12:
            w[w > max_weight] = max_weight
            break
        room = float(under.sum())
        w[w > max_weight] = max_weight
        w[w <= max_weight] += under / room * excess
    s = w.sum()
    return w / s if s > 1e-12 else w


def cap_industry_weights(holdings: pd.DataFrame, industry_map: dict,
                         max_industry_weight: float) -> pd.DataFrame:
    """行业权重上限：迭代压缩超限行业的股票权重

    holdings: code/weight；industry_map: code → industry（缺失行业跳过约束）
    """
    df = holdings.copy()
    if not industry_map or max_industry_weight <= 0:
        return df
    df["industry"] = df["code"].map(industry_map)
    for _ in range(20):
        grp = df.groupby("industry")["weight"].sum()
        over = grp[grp > max_industry_weight]
        if over.empty:
            break
        for ind in over.index:
            mask = df["industry"] == ind
            scale = max_industry_weight / grp[ind]
            df.loc[mask, "weight"] *= scale
        s = df["weight"].sum()
        df["weight"] /= s
    df = df.drop(columns=["industry"])
    return df
