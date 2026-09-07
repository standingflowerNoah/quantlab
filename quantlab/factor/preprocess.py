"""因子预处理（国泰君安多因子标准流程）
=====================================
国泰君安经典多因子模型的因子标准化三步：
1. 去极值（MAD 法）：median ± n·1.4826·MAD，超出部分截断（winsorize），
   消除极端值对 z-score 的污染；
2. 中性化：行业哑变量 + log 市值 OLS 回归取残差（复用 neutralize_section）；
3. 标准化：截面 z-score = (x - μ)/σ。

与既有 rank 标准化（composite.py）互补：rank 稳健但丢失分布信息，
z-score 保留分布但需先做去极值 + 中性化。国泰君安流程用后者。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import get_logger

log = get_logger(__name__)

# MAD → std 一致估计量缩放因子（正态分布假设）
_MAD_SCALE = 1.4826


def winsorize_mad(df: pd.DataFrame, n: float = 3.0,
                  value_col: str = "value") -> pd.DataFrame:
    """MAD 去极值（逐日截面截断）

    参数 n: 偏离中位数的 MAD 倍数（3 = 约 4.45σ，国泰君安常用 3~5）
    返回: 拷贝后的 DataFrame，value_col 已被 winsorize
    """
    if df.empty:
        return df.copy()

    def _w(x: pd.Series) -> pd.Series:
        x = x.astype(float)
        med = x.median()
        mad = (x - med).abs().median()
        if not np.isfinite(mad) or mad == 0:
            return x
        hi = med + n * _MAD_SCALE * mad
        lo = med - n * _MAD_SCALE * mad
        return x.clip(lo, hi)

    out = df.copy()
    out[value_col] = out.groupby("date")[value_col].transform(_w)
    return out


def zscore_section(df: pd.DataFrame, value_col: str = "value") -> pd.DataFrame:
    """截面 z-score 标准化（逐日 (x-μ)/σ）"""
    if df.empty:
        return df.copy()
    out = df.copy()

    def _z(x: pd.Series) -> pd.Series:
        x = x.astype(float)
        sd = x.std()
        if not np.isfinite(sd) or sd == 0:
            return x - x.mean()
        return (x - x.mean()) / sd

    out[value_col] = out.groupby("date")[value_col].transform(_z)
    return out


def preprocess(df: pd.DataFrame, industry_map: dict | None = None,
               size_df: pd.DataFrame | None = None,
               mode: str = "both", mad_n: float = 3.0) -> pd.DataFrame:
    """国泰君安标准预处理：去极值 → 中性化 → z-score

    参数:
        df: date/code/value 长格式
        industry_map: code → industry（None=跳过行业中性化）
        size_df: date/code/value 市值序列（None=跳过市值中性化）
        mode: both/industry/size（中性化口径）
        mad_n: MAD 截断倍数
    返回: date/code/value，已完成标准化（z-score）
    """
    df = winsorize_mad(df, n=mad_n)
    if industry_map is not None and size_df is not None and mode:
        from .neutralize import neutralize_section
        df = neutralize_section(df, industry_map, size_df, mode=mode)
    return zscore_section(df)
