"""因子截面中性化（L2 因子层增强）
=====================================
消除因子与「行业」和「市值」的暴露，提纯 alpha：
对每个交易日截面做 OLS 回归 y = 行业哑变量 + log市值，取残差作为
中性化后的因子值。中性化后的因子与市值/行业正交，多因子合成时
可避免风格因子重复计入。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import get_logger

log = get_logger(__name__)


def neutralize_section(factor_df: pd.DataFrame, industry_map: dict,
                       size_df: pd.DataFrame,
                       min_n: int = 30,
                       mode: str = "both") -> pd.DataFrame:
    """截面中性化（OLS 残差）

    参数:
        factor_df: date/code/value（待中性化因子）
        industry_map: code → industry（静态映射，缺失行业跳过）
        size_df: date/code/value（市值序列，用 size 因子即可）
        mode: both=行业+市值 / industry=仅行业 / size=仅市值
    返回: date/code/value（残差，已中性化）
    """
    df = factor_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    if mode in ("industry", "both"):
        df["industry"] = df["code"].map(industry_map)
    else:
        df["industry"] = "ALL"
    size_df = size_df.rename(columns={"value": "size"})
    df = df.merge(size_df[["date", "code", "size"]],
                  on=["date", "code"], how="left")
    df = df.dropna(subset=["value"])
    if mode == "industry":
        df = df.dropna(subset=["industry"])
    elif mode in ("size", "both"):
        df = df.dropna(subset=["industry", "size"])
        df = df[df["size"] > 0]

    out = []
    for date, g in df.groupby("date"):
        if len(g) < min_n:
            continue
        cols = []
        if mode in ("industry", "both"):
            dummies = pd.get_dummies(g["industry"], prefix="i")
            cols.append(dummies.values.astype(float))
        if mode in ("size", "both"):
            cols.append(np.log(g["size"].values))
        X = np.column_stack(cols) if cols else np.ones((len(g), 1))
        y = g["value"].values.astype(float)
        try:
            beta = np.linalg.lstsq(X, y, rcond=None)[0]
            resid = y - X @ beta
        except np.linalg.LinAlgError:
            resid = y - y.mean()
        g = g.copy()
        g["value"] = resid
        out.append(g[["date", "code", "value"]])

    if not out:
        return pd.DataFrame(columns=["date", "code", "value"])
    res = pd.concat(out, ignore_index=True)
    return res.sort_values(["date", "code"]).reset_index(drop=True)
