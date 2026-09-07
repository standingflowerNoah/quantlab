"""估值时序异常 + 特异换手（开放式挖掘 · 2026-09-07）
=====================================
搜索来源：东吴 EPA 估值异常研报（思路库 C3）+ 中信建投行为金融因子
跟踪月报（SPILLTURN 溢出换手近一年多空 26%）+ 雪球主流因子池盘点。

- epd_60    估值时序偏离：EP 相对自身过去 60 交易日的 z-score
            （静态 ep 只有截面维度；本因子是【时序维度】——自身历史比较，
             与 ep 截面正交的信息）。研报口径：PE 布林带偏离，2010-2025
             多空年化 17.2%/IR 3.37（需自证）。
- epds_60   EPD 持续性过滤：EPD 的 60 日信息比率（均值/|std|）。
            研报逻辑：EPD 方向频繁翻转 = 估值逻辑已变（重组/赛道切换），
            用 IR 剔除这类噪声，只保留"持续便宜/持续贵"的真偏离。
- turn_idio_20  特异换手率（SPILLTURN 对偶）：个股换手率 − 市场中位换手率
            的 20 日均值。剔除市场级换手潮（普涨普跌行情全市场放量）后，
            个股自身的关注度激增/退潮才是特异信息。

依赖：ep / turnover 因子湖数据先行落库。全部因果（rolling 过去窗口）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..base import Factor
from ..registry import register
from ...config import get_logger

log = get_logger(__name__)


class _PandasFactorBase(Factor):
    """pandas 因子公共骨架：读依赖因子 → 逐股时序计算 → 过滤输出"""

    freq = "daily"

    def compute(self, store, start=None, end=None,
                universe: list[str] | None = None) -> pd.DataFrame:
        df = self._compute_series(store)
        if df.empty:
            return pd.DataFrame(columns=["date", "code", "value"])
        out = df.rename(columns={df.columns[-1]: "value"})[
            ["date", "code", "value"]].copy()
        out["date"] = pd.to_datetime(out["date"])
        out["code"] = out["code"].astype(str).str.zfill(6)
        if universe is not None:
            out = out[out["code"].isin(set(map(str, universe)))]
        if start is not None:
            out = out[out["date"] >= pd.to_datetime(start)]
        if end is not None:
            out = out[out["date"] <= pd.to_datetime(end)]
        out = out.replace([np.inf, -np.inf], np.nan).dropna(subset=["value"])
        return out.reset_index(drop=True)

    def _compute_series(self, store) -> pd.DataFrame:
        raise NotImplementedError


def _load_wide(store, name: str) -> pd.DataFrame:
    fv = store.read_factor(name)
    fv["date"] = pd.to_datetime(fv["date"])
    fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
    w = fv.pivot(index="date", columns="code", values="value").sort_index()
    return w


@register
class Epd60(_PandasFactorBase):
    name = "epd_60"
    description = ("估值时序偏离：EP 相对自身过去60日 z-score（时序维度，"
                   "与截面 ep 互补；低分位=相对自身历史便宜）")
    category = "value"
    _win = 60

    def _compute_series(self, store) -> pd.DataFrame:
        ep = _load_wide(store, "ep")
        mu = ep.rolling(self._win, min_periods=45).mean()
        sd = ep.rolling(self._win, min_periods=45).std()
        out = (ep - mu) / sd
        out = out.clip(-10, 10)                     # 防小 std 爆炸
        return out.stack().rename("value").reset_index()


@register
class Epds60(_PandasFactorBase):
    name = "epds_60"
    description = ("EPD 持续性：epd_60 的 60 日信息比率（均值/|std|）；"
                   "剔除估值逻辑已变的频繁翻转股")
    category = "value"
    _win = 60

    def _compute_series(self, store) -> pd.DataFrame:
        epd = Epd60()._compute_series(store)
        w = epd.pivot(index="date", columns="code", values="value").sort_index()
        mu = w.rolling(self._win, min_periods=45).mean()
        sd = w.rolling(self._win, min_periods=45).std()
        ir = mu / sd.where(sd > 1e-12)
        ir = ir.clip(-10, 10)
        return ir.stack().rename("value").reset_index()


@register
class TurnIdio20(_PandasFactorBase):
    name = "turn_idio_20"
    description = ("特异换手率：个股换手率 − 当日市场中位换手率的 20 日均值"
                   "（剔除市场级换手潮后的个体关注度激增/退潮；SPILLTURN 对偶）")
    category = "volume"
    _win = 20

    def _compute_series(self, store) -> pd.DataFrame:
        to = _load_wide(store, "turnover")
        med = to.median(axis=1)                      # 当日市场换手潮水位
        idio = to.sub(med, axis=0)
        out = idio.rolling(self._win, min_periods=15).mean()
        return out.stack().rename("value").reset_index()
