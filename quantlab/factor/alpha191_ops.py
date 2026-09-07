"""Alpha191 算子库（国泰君安《基于短周期价量特征的多因子选股体系》）
=====================================
所有算子基于「宽表」DataFrame（index=交易日, columns=股票代码）向量化实现：
- 时序算子：逐列 rolling（pandas 原生，C 级速度）
- 截面算子：逐行 rank（axis=1）
- 条件/一元算子：elementwise

数据约定：OHLC 与 VWAP 均为前复权口径（除权日不产生假跳空），volume 为原始成交量（股）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "delay", "delta", "ts_mean", "ts_std", "ts_sum", "ts_min", "ts_max",
    "ts_rank", "ts_corr", "ts_cov", "ts_argmax", "ts_argmin",
    "rank", "scale", "sign", "log_", "abs_", "if_", "max_", "min_", "power",
]


# ── 时序算子（逐列滚动） ────────────────────────────────────────────
def delay(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.shift(d)


def delta(x: pd.DataFrame, d: int = 1) -> pd.DataFrame:
    return x - x.shift(d)


def ts_mean(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d, min_periods=d).mean()


def ts_std(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d, min_periods=d).std()


def ts_sum(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d, min_periods=d).sum()


def ts_min(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d, min_periods=d).min()


def ts_max(x: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d, min_periods=d).max()


def ts_rank(x: pd.DataFrame, d: int) -> pd.DataFrame:
    """滚动窗口内时序百分位（0~1）"""
    return x.rolling(d, min_periods=d).rank(pct=True)


def ts_corr(x: pd.DataFrame, y: pd.DataFrame, d: int) -> pd.DataFrame:
    """滚动窗口相关系数（逐列成对）"""
    return x.rolling(d, min_periods=d).corr(y)


def ts_cov(x: pd.DataFrame, y: pd.DataFrame, d: int) -> pd.DataFrame:
    return x.rolling(d, min_periods=d).cov(y)


def _sliding_arg(x: np.ndarray, d: int, which: str) -> np.ndarray:
    """分块滑动窗口 argmax/argmin（控制临时内存 ~160MB）"""
    T, N = x.shape
    out = np.full((T, N), np.nan)
    if T < d:
        return out
    filled = x.copy()
    nan_mask = np.isnan(filled)
    filled[nan_mask] = -np.inf if which == "max" else np.inf
    chunk = max(1, int(2e7 // max(T * d, 1)))
    red = np.argmax if which == "max" else np.argmin
    for s in range(0, N, chunk):
        e = min(N, s + chunk)
        sw = np.lib.stride_tricks.sliding_window_view(filled[:, s:e], d, axis=0)
        am = red(sw, axis=2).astype(float)
        cnt = np.lib.stride_tricks.sliding_window_view(
            nan_mask[:, s:e], d, axis=0).sum(axis=2)
        out[d - 1:, s:e] = np.where(cnt < d, am, np.nan)
    return out


def ts_argmax(x: pd.DataFrame, d: int) -> pd.DataFrame:
    """窗口内最大值出现位置（0=当前日，d-1=窗口首日）"""
    arr = _sliding_arg(x.values.astype(float), d, "max")
    return pd.DataFrame(arr, index=x.index, columns=x.columns)


def ts_argmin(x: pd.DataFrame, d: int) -> pd.DataFrame:
    arr = _sliding_arg(x.values.astype(float), d, "min")
    return pd.DataFrame(arr, index=x.index, columns=x.columns)


# ── 截面算子（逐行） ────────────────────────────────────────────────
def rank(x: pd.DataFrame) -> pd.DataFrame:
    """每日截面百分位排名（0~1）"""
    return x.rank(axis=1, pct=True)


def scale(x: pd.DataFrame, a: float = 1.0) -> pd.DataFrame:
    """截面缩放：abs 求和归一到 a"""
    denom = x.abs().sum(axis=1)
    return x.mul(a / denom.where(denom != 0), axis=0)


# ── 一元 / 二元算子 ─────────────────────────────────────────────────
def sign(x: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(np.sign(x.values), index=x.index, columns=x.columns)


def log_(x: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        np.log(x.values, where=x.values > 0, out=np.full(x.shape, np.nan)),
        index=x.index, columns=x.columns)


def abs_(x: pd.DataFrame) -> pd.DataFrame:
    return x.abs()


def if_(cond: pd.DataFrame, a, b) -> pd.DataFrame:
    """cond 为真取 a，否则取 b（a/b 可为宽表或标量）"""
    cv = cond.values.astype(bool)
    av = a.values if isinstance(a, pd.DataFrame) else a
    bv = b.values if isinstance(b, pd.DataFrame) else b
    out = np.where(cv, av, bv)
    return pd.DataFrame(out, index=cond.index, columns=cond.columns)


def max_(a, b) -> pd.DataFrame:
    ref = a if isinstance(a, pd.DataFrame) else b
    av = a.values if isinstance(a, pd.DataFrame) else a
    bv = b.values if isinstance(b, pd.DataFrame) else b
    return pd.DataFrame(np.maximum(av, bv), index=ref.index, columns=ref.columns)


def min_(a, b) -> pd.DataFrame:
    ref = a if isinstance(a, pd.DataFrame) else b
    av = a.values if isinstance(a, pd.DataFrame) else a
    bv = b.values if isinstance(b, pd.DataFrame) else b
    return pd.DataFrame(np.minimum(av, bv), index=ref.index, columns=ref.columns)


def power(x: pd.DataFrame, p: float) -> pd.DataFrame:
    """幂运算（负底数 → NaN，避免复数）"""
    v = x.values.astype(float)
    out = np.full(v.shape, np.nan)
    valid = ~(np.isnan(v) | (v < 0))
    out[valid] = np.power(v[valid], p)
    return pd.DataFrame(out, index=x.index, columns=x.columns)
