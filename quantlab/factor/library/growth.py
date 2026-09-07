"""成长因子（国泰君安经典多因子体系 · Growth）
=====================================
基于 finance_history（东财多期财报，2021Q1 起 22 期）的同比增速：

- revenue_growth_yoy  营收同比：优先东财自带同比（YSTZ%），缺失时两期自算
- profit_growth_yoy   净利同比：自算并剔除去年同期 ≤0 的样本（符号翻转时
  同比无定义）
- asset_growth_yoy    总资产同比：两期自算

记账规则（无前视）：
- 每期同比在 NOTICE_DATE（披露日）后才可见 → kline 日期 ASOF JOIN
  notice_date ≤ t 的最近一期，因子值从披露日起持续到下一次披露（阶梯填充）
- 历史上退市股也在 finance_history 中（东财按期含全市场），无幸存者偏差
"""
from __future__ import annotations

from ..base import SqlFactor, universe_sql
from ..registry import register

# 同比 CTE：本期 vs 去年同期（finance_history 自连接），记账日 = 生效披露日
# notice_eff：东财 NOTICE_DATE 为"最近修订公告日"（老报告期普遍滞后 1 年+），
# 超出法定披露截止的回退到截止日（截止日全市场必已披露，无前视）。
# QUALIFY 去重：同股同日披露多期（年报+一季报同日）时取最新报告期，
# 保证 ASOF JOIN tie-break 确定（否则截断环境与全量可能选不同期 → 误报穿越）
_GROWTH_BASE = """
WITH fh AS (
    SELECT code, report_period, notice_eff,
           revenue, revenue_yoy, net_profit, net_profit_yoy, total_assets
    FROM finance_history
    WHERE notice_eff IS NOT NULL
),
prev AS (
    SELECT f.code, f.report_period,
           p.revenue AS prev_revenue, p.net_profit AS prev_np,
           p.total_assets AS prev_assets
    FROM fh f
    JOIN fh p
      ON p.code = f.code
     AND p.report_period = f.report_period - INTERVAL 1 YEAR
),
g AS (
    SELECT code, notice_date, value FROM (
        SELECT f.code, f.notice_eff AS notice_date,
               {expr} AS value,
               ROW_NUMBER() OVER (
                 PARTITION BY f.code, f.notice_eff
                 ORDER BY f.report_period DESC) AS rk
        FROM fh f
        LEFT JOIN prev p
          ON p.code = f.code AND p.report_period = f.report_period
        WHERE f.report_period >= DATE '2022-01-01'
    ) WHERE rk = 1
)
SELECT k.date, g.code, g.value
FROM kline_daily k
ASOF JOIN g
  ON g.code = k.code AND g.notice_date <= k.date
WHERE g.value IS NOT NULL AND isfinite(g.value) {usql}
"""


class _YoYGrowthFactor(SqlFactor):
    """同比成长基类：finance_history 两期对齐 + ASOF 前向填充"""
    category = "growth"
    _expr: str = "NULL"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = _GROWTH_BASE.format(expr=self._expr, usql=usql)
        return sql, uparams


@register
class RevenueGrowthYoY(_YoYGrowthFactor):
    name = "revenue_growth_yoy"
    description = "营收同比增速（东财自带同比优先，缺失两期自算；披露日记账无前视）"
    _expr = """
        CASE
          WHEN f.revenue_yoy IS NOT NULL THEN f.revenue_yoy / 100.0
          WHEN f.revenue > 0 AND p.prev_revenue > 0
            THEN f.revenue / p.prev_revenue - 1.0
        END"""


@register
class ProfitGrowthYoY(_YoYGrowthFactor):
    name = "profit_growth_yoy"
    description = "净利润同比增速（自算，剔除去年同期≤0；披露日记账无前视）"
    _expr = """
        CASE
          WHEN f.net_profit IS NOT NULL AND p.prev_np > 0
            THEN f.net_profit / p.prev_np - 1.0
        END"""


@register
class AssetGrowthYoY(_YoYGrowthFactor):
    name = "asset_growth_yoy"
    description = "总资产同比增速（两期自算；披露日记账无前视）"
    _expr = """
        CASE
          WHEN f.total_assets > 0 AND p.prev_assets > 0
            THEN f.total_assets / p.prev_assets - 1.0
        END"""
