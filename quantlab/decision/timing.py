"""大盘择时（L5 决策层风控）
=====================================
用小盘指数（中证1000）均线判断市场趋势，趋势向下时降低仓位，
用于平滑小市值策略在微盘股崩盘期的回撤。

信号：fast 日均线 > slow 日均线 → 满仓(1)，否则 → 目标仓位(target_pos)。
"""
from __future__ import annotations

import pandas as pd

from ..config import get_logger
from ..data.store import query

log = get_logger(__name__)


def market_timing(index_code: str = "000852.SH", fast: int = 20,
                  slow: int = 60, target_pos: float = 0.5) -> pd.DataFrame:
    """大盘择时信号（date/position）

    返回每个交易日建议仓位 position ∈ {1.0, target_pos}。
    注意：样本外验证显示激进降仓(0.0)会过拟合，默认用温和降仓(0.5)。
    """
    df = query("""
        SELECT date, close FROM index_kline
        WHERE code = ? ORDER BY date
    """, [index_code])
    if df.empty:
        log.warning(f"指数 {index_code} 无 K 线，择时失效")
        return pd.DataFrame(columns=["date", "position"])
    df["date"] = pd.to_datetime(df["date"])
    df["fast_ma"] = df["close"].rolling(fast).mean()
    df["slow_ma"] = df["close"].rolling(slow).mean()
    df["position"] = 1.0
    df.loc[df["fast_ma"] < df["slow_ma"], "position"] = target_pos
    df = df.dropna(subset=["slow_ma"])
    return df[["date", "position"]]


def apply_timing(curve: pd.DataFrame, position: pd.DataFrame) -> pd.DataFrame:
    """把择时仓位叠加到回测净值曲线上

    curve: date/ret/nav...（run_optimized_backtest 返回的 curve）
    position: date/position
    返回新 curve（ret 已乘仓位）
    """
    c = curve.copy()
    c["date"] = pd.to_datetime(c["date"])
    pos = position.copy()
    pos["date"] = pd.to_datetime(pos["date"])
    c = c.merge(pos, on="date", how="left")
    c["position"] = c["position"].ffill().fillna(1.0)
    c["ret"] = c["ret"] * c["position"]
    c["nav"] = (1 + c["ret"]).cumprod()
    c["nav_bm"] = (1 + c["bench"].fillna(0)).cumprod()
    return c
