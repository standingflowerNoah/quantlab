"""流动性因子（国泰君安经典多因子体系 · Liquidity）
=====================================
基于 kline_daily 的成交额(amount, 元)与成交量(vol, 股)构建，有完整历史深度，
可做 IC 检验与回测。

口径：
- Amihud 非流动性 = |日收益| / 亿元成交额（20 日均值），度量"单位成交额造成的价格冲击"，
  值越大流动性越差。A 股存在「低流动性溢价」，属经典 liquidity 因子。
- 换手率 = vol / float_shares（股/股，与 turnover 因子一致口径；vol 单位为股）。
- 成交额规模 = ln(20 日均成交额)，流动性规模代理（与 size 高相关，建模时注意去冗余）。
"""
from __future__ import annotations

from ..base import SqlFactor, QFQ_CLOSE, universe_sql
from ..registry import register


@register
class Amihud20(SqlFactor):
    name = "amihud_20"
    description = "Amihud 非流动性 |日收益|/亿元成交额 的20日均值（低流动性溢价）"
    category = "liquidity"
    economic_rationale = ("risk：非流动性溢价——持有高冲击成本股票的风险补偿"
                          "（Amihud 2002）；与容量直接对冲（本因子升=组合容量降）")

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
        {QFQ_CLOSE},
        ret AS (
            SELECT date, code,
                   ABS(close_adj / LAG(close_adj) OVER w - 1) AS abs_r,
                   amount
            FROM qfq
            JOIN kline_daily k USING (date, code)
            WHERE amount > 0 {usql}
            WINDOW w AS (PARTITION BY code ORDER BY date)
        )
        SELECT date, code,
               AVG(abs_r / (amount / 1e8)) OVER (
                 PARTITION BY code ORDER BY date
                 ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS value
        FROM ret
        QUALIFY value IS NOT NULL
        """
        return sql, uparams


@register
class TurnoverStd20(SqlFactor):
    name = "turnover_std_20"
    description = "20日换手率标准差（换手率波动，投机性代理）"
    category = "liquidity"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
        WITH tr AS (
            SELECT k.date, k.code, k.vol / f.float_shares AS to_rate
            FROM kline_daily k
            ASOF JOIN share_capital_daily f
              ON f.code = k.code AND k.date >= f.date
            WHERE f.float_shares > 0 {usql}
        )
        SELECT date, code,
               STDDEV_SAMP(to_rate) OVER (
                 PARTITION BY code ORDER BY date
                 ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS value
        FROM tr
        QUALIFY value IS NOT NULL
        """
        return sql, uparams


@register
class AvgAmount20(SqlFactor):
    name = "avg_amount_20"
    description = "ln(20日平均成交额 亿元)（流动性规模）"
    category = "liquidity"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
        SELECT date, code,
               LN(AVG(amount / 1e8) OVER (
                 PARTITION BY code ORDER BY date
                 ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)) AS value
        FROM kline_daily
        WHERE amount > 0 {usql}
        QUALIFY value IS NOT NULL
        """
        return sql, uparams


@register
class AmountStd20(SqlFactor):
    name = "amount_std_20"
    description = "20日成交额标准差/均值（成交额波动，流动性稳定性）"
    category = "liquidity"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
        SELECT date, code,
               STDDEV_SAMP(amount) OVER (
                 PARTITION BY code ORDER BY date
                 ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
               / NULLIF(AVG(amount) OVER (
                 PARTITION BY code ORDER BY date
                 ROWS BETWEEN 19 PRECEDING AND CURRENT ROW), 0) AS value
        FROM kline_daily
        WHERE amount > 0 {usql}
        QUALIFY value IS NOT NULL
        """
        return sql, uparams
