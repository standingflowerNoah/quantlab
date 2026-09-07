"""价格类时序因子：动量 / 反转 / 波动率 / RSI
=====================================
全部基于 kline_daily 前复权收盘价，用 DuckDB 窗口函数一次算全市场，
避免 Python 逐行循环。因子值单位为收益比例（如 0.05 = +5%）。
"""
from __future__ import annotations

from ..base import SqlFactor, QFQ_CLOSE, universe_sql
from ..registry import register


class _MomBase(SqlFactor):
    """N 日动量 / 反转 基类"""
    category = "price"
    n: int = 20
    _sign: int = 1          # +1 动量，-1 反转

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
        {QFQ_CLOSE}
        SELECT date, code,
               {self._sign} * (close_adj / LAG(close_adj, {self.n})
                 OVER (PARTITION BY code ORDER BY date) - 1) AS value
        FROM qfq
        WHERE 1=1 {usql}
        QUALIFY value IS NOT NULL
        """
        return sql, uparams


@register
class Momentum20(_MomBase):
    name = "momentum_20"
    description = "20日动量（前复权累计收益）"
    n = 20


@register
class Momentum60(_MomBase):
    name = "momentum_60"
    description = "60日动量（前复权累计收益）"
    n = 60


@register
class Momentum120(_MomBase):
    name = "momentum_120"
    description = "120日动量（前复权累计收益）"
    n = 120


@register
class Reversal5(_MomBase):
    name = "reversal_5"
    description = "5日反转（-5日收益，短期反转信号）"
    n = 5
    _sign = -1


@register
class Reversal10(_MomBase):
    name = "reversal_10"
    description = "10日反转（-10日收益）"
    n = 10
    _sign = -1


class _VolBase(SqlFactor):
    """N 日日收益标准差（年化前，日频）"""
    category = "price"
    n: int = 20

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
        {QFQ_CLOSE},
        ret AS (
            SELECT date, code,
                   close_adj / LAG(close_adj) OVER w - 1 AS r
            FROM qfq
            WHERE 1=1 {usql}
            WINDOW w AS (PARTITION BY code ORDER BY date)
        )
        SELECT date, code,
               STDDEV_SAMP(r) OVER (
                 PARTITION BY code ORDER BY date
                 ROWS BETWEEN {self.n - 1} PRECEDING AND CURRENT ROW) AS value
        FROM ret
        QUALIFY value IS NOT NULL
        """
        return sql, uparams


@register
class Volatility20(_VolBase):
    name = "volatility_20"
    description = "20日日收益波动率（标准差）"
    n = 20


@register
class Volatility60(_VolBase):
    name = "volatility_60"
    description = "60日日收益波动率（标准差）"
    n = 60


@register
class Rsi14(SqlFactor):
    name = "rsi_14"
    description = "14日相对强弱指标 RSI（0~100）"
    category = "tech"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
        {QFQ_CLOSE},
        diff AS (
            SELECT date, code,
                   close_adj - LAG(close_adj) OVER w AS d
            FROM qfq
            WHERE 1=1 {usql}
            WINDOW w AS (PARTITION BY code ORDER BY date)
        ),
        ag AS (
            SELECT date, code,
                   AVG(CASE WHEN d > 0 THEN d ELSE 0 END) OVER (
                     PARTITION BY code ORDER BY date
                     ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS gain,
                   AVG(CASE WHEN d < 0 THEN -d ELSE 0 END) OVER (
                     PARTITION BY code ORDER BY date
                     ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS loss
            FROM diff
        )
        SELECT date, code,
               CASE WHEN loss = 0 THEN 100.0
                    ELSE 100 - 100 / (1 + gain / loss) END AS value
        FROM ag
        WHERE gain IS NOT NULL AND loss IS NOT NULL
        """
        return sql, uparams


@register
class Amplitude20(SqlFactor):
    name = "amplitude_20"
    description = "20日平均振幅 (high-low)/close"
    category = "price"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
        SELECT date, code,
               AVG((high - low) / close) OVER (
                 PARTITION BY code ORDER BY date
                 ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS value
        FROM kline_daily
        WHERE close > 0 {usql}
        QUALIFY value IS NOT NULL
        """
        return sql, uparams


@register
class MaxReturn20(SqlFactor):
    name = "max_return_20"
    description = "20日最大单日收益（追涨风险代理）"
    category = "price"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
        {QFQ_CLOSE},
        ret AS (
            SELECT date, code,
                   close_adj / LAG(close_adj) OVER w - 1 AS r
            FROM qfq
            WHERE 1=1 {usql}
            WINDOW w AS (PARTITION BY code ORDER BY date)
        )
        SELECT date, code,
               MAX(r) OVER (
                 PARTITION BY code ORDER BY date
                 ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS value
        FROM ret
        QUALIFY value IS NOT NULL
        """
        return sql, uparams


@register
class PricePosition250(SqlFactor):
    name = "price_position_250"
    description = "250日价格位置 close/250日最高（0~1，接近1=创新高）"
    category = "price"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
        {QFQ_CLOSE}
        SELECT date, code,
               close_adj / MAX(close_adj) OVER (
                 PARTITION BY code ORDER BY date
                 ROWS BETWEEN 249 PRECEDING AND CURRENT ROW) AS value
        FROM qfq
        WHERE 1=1 {usql}
        QUALIFY value IS NOT NULL
        """
        return sql, uparams


@register
class DownsideVolatility20(SqlFactor):
    name = "downside_volatility_20"
    description = "20日下行波动率 sqrt(mean(min(r,0)^2))（低波动异象变体）"
    category = "price"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
        {QFQ_CLOSE},
        ret AS (
            SELECT date, code,
                   close_adj / LAG(close_adj) OVER w - 1 AS r
            FROM qfq
            WHERE 1=1 {usql}
            WINDOW w AS (PARTITION BY code ORDER BY date)
        )
        SELECT date, code,
               SQRT(AVG(POW(CASE WHEN r < 0 THEN r ELSE 0 END, 2)) OVER (
                 PARTITION BY code ORDER BY date
                 ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)) AS value
        FROM ret
        QUALIFY value IS NOT NULL
        """
        return sql, uparams


@register
class Skewness20(SqlFactor):
    name = "skewness_20"
    description = "20日日收益偏度 E[((r-μ)/σ)^3]（正偏=彩票股）"
    category = "price"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
        {QFQ_CLOSE},
        ret AS (
            SELECT date, code,
                   close_adj / LAG(close_adj) OVER w - 1 AS r
            FROM qfq
            WHERE 1=1 {usql}
            WINDOW w AS (PARTITION BY code ORDER BY date)
        ),
        st AS (
            SELECT date, code, r,
                   AVG(r) OVER w AS m,
                   STDDEV_SAMP(r) OVER w AS s
            FROM ret
            WINDOW w AS (PARTITION BY code ORDER BY date
                         ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
        )
        SELECT date, code,
               AVG(POW((r - m) / NULLIF(s, 0), 3)) OVER (
                 PARTITION BY code ORDER BY date
                 ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS value
        FROM st
        WHERE s > 0
        QUALIFY value IS NOT NULL
        """
        return sql, uparams
