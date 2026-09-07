"""机构席位因子（开放式挖掘 · 事件类同家族 · 2026-09-07）
=====================================
数据：dragon_seats（龙虎榜席位明细，OPERATEDEPT_NAME 含"机构专用"）。
现有 dragon_net_20 用净买入总额（负 IC=上榜后均值回归）；本因子切换到
【谁在买】维度——机构专用席位净买入强度。研报口径：机构席位建仓正向
预示（与游资接力的负向不矛盾，是同一事件的子集信息）。

- inst_buy_20  过去 20 日机构席位净买入合计 / 同窗个股成交额合计（‱量级）
               事件表 → RANGE INTERVAL 窗口（skill 教训：不得用 ROWS）
               无上榜 → 无值（覆盖=上榜股，事件因子特性）
方向预期：正（机构建仓）；最终由 IC 定。方向性闸门：rolling 过去窗口
天然因果（同 A5 事件簇模式）。
"""
from __future__ import annotations

from ..base import SqlFactor, universe_sql
from ..registry import register

_SEAT_BASE = """
WITH seats AS (
    SELECT code, CAST(date AS DATE) AS date,
           SUM(CASE WHEN {seat_cond} THEN net ELSE 0 END) AS grp_net
    FROM dragon_seats
    GROUP BY code, CAST(date AS DATE)
), roll AS (
    SELECT code, date,
           SUM(grp_net) OVER w AS grp_net_20
    FROM seats
    WINDOW w AS (PARTITION BY code ORDER BY date
                 RANGE BETWEEN INTERVAL 20 DAY PRECEDING AND CURRENT ROW)
), amt AS (
    SELECT code, CAST(date AS DATE) AS date,
           SUM(amount) OVER w20 AS amt_20
    FROM kline_daily
    WHERE amount > 0
    WINDOW w20 AS (PARTITION BY code ORDER BY date
                   ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
)
{expr}
"""

_INST_COND = "is_inst"
_LHASA_COND = "dept_name LIKE '%拉萨%'"


class _SeatFactor(SqlFactor):
    category = "dragon"
    _seat_cond: str = _INST_COND
    _expr_tpl = """
SELECT k.date, r.code,
       r.grp_net_20 / NULLIF(a.amt_20, 0) * 10000 AS value
FROM roll r
JOIN amt a ON a.code = r.code AND a.date = r.date
JOIN kline_daily k ON k.code = r.code AND CAST(k.date AS DATE) = r.date
WHERE a.amt_20 > 0 {usql}
"""

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = (_SEAT_BASE.replace("{expr}", self._expr_tpl)
               .replace("{usql}", usql)
               .replace("{seat_cond}", self._seat_cond))
        return sql, uparams


@register
class InstBuy20(_SeatFactor):
    name = "inst_buy_20"
    description = ("20日机构席位净买入/成交额（‱；机构建仓正向预示候选，"
                   "与 dragon_net_20 的总额口径信息维度不同）")
    freq = "daily"
    _seat_cond = _INST_COND


@register
class RetailBuy20(_SeatFactor):
    name = "retail_buy_20"
    description = ("20日散户通道（东财拉萨系营业部）净买入/成交额（‱；"
                   "散户接力强度，反向指标候选——与机构维度对偶）")
    freq = "daily"
    _seat_cond = _LHASA_COND
