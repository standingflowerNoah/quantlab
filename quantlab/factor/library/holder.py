"""股东户数因子族（第四批 · 因子思路库 B1 · 2026-09-07）
=====================================
数据宝/私募排排网统计思路：股东户数下降 = 筹码集中（主力吸筹），
连续多期下降信号更强。研报基准（中小市值域 RankIC 负值 |·|≥0.062），
方向预期为负（户数增=筹码分散=散户化→未来收益差），以实测 IC 为准。

数据：holder_num（东财 RPT_HOLDERNUM_DET，披露频率不规则 → 稀疏事件表）。
- PIT 记账：notice_date（披露日）生效，ASOF notice_date <= t（与财务因子口径一致）
- 同一生效时点取最新 end_date（重述修正）
- 陈旧度过滤：最近披露距今 > 180 交易日数据失效置 NULL（户数信息有半衰期）
- 窗口：无滚动窗，事件驱动的最新披露值

因子：
- holder_num_chg    最新披露期户数环比（holder_num/prev − 1）
- holder_num_chg_2  两期累计变化（holder_num/holder_num_{-2} − 1，连续下降信号更强）
"""
from __future__ import annotations

from ..base import SqlFactor, universe_sql
from ..registry import register

# 披露生效基座：环比计算 + 生效时点去重 + 陈旧度过滤
_HN_BASE = """
WITH hn AS (
    SELECT code, end_date, notice_date, holder_num,
           LAG(holder_num) OVER (PARTITION BY code
                                 ORDER BY end_date, notice_date) AS prev_num,
           LAG(holder_num, 2) OVER (PARTITION BY code
                                    ORDER BY end_date, notice_date) AS prev2_num
    FROM holder_num
    WHERE notice_date IS NOT NULL AND holder_num > 0
), eff AS (
    SELECT code, notice_date, holder_num, prev_num, prev2_num FROM (
        SELECT *, ROW_NUMBER() OVER (
            PARTITION BY code, notice_date
            ORDER BY end_date DESC) AS rk
        FROM hn WHERE prev_num > 0
    ) WHERE rk = 1
)
{expr}
"""


class _HolderNumFactor(SqlFactor):
    """股东户数变化基类"""
    category = "holder"
    _lag: int = 1  # 1=最新一期环比, 2=两期累计

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        expr = self._expr.format(usql=usql)      # 先填 expr 内部占位
        sql = _HN_BASE.format(usql=usql, expr=expr)  # 再填基座
        return sql, uparams

    @property
    def _expr(self) -> str:
        denom = ("prev_num" if self._lag == 1 else "prev2_num")
        return f"""
SELECT * FROM (
    SELECT k.date, eff.code,
           CASE WHEN CAST(k.date AS DATE) - eff.notice_date <= 180 THEN
                eff.holder_num / NULLIF(eff.{denom}::DOUBLE, 0) - 1.0
           END AS value
    FROM kline_daily k
    ASOF JOIN eff ON eff.code = k.code AND eff.notice_date <= k.date
    WHERE eff.notice_date IS NOT NULL {{usql}}
)
WHERE value IS NOT NULL AND isfinite(value)
"""


@register
class HolderNumChg(_HolderNumFactor):
    name = "holder_num_chg"
    description = ("最新披露期股东户数环比（下降=筹码集中；PIT 按披露日，"
                   "180 日陈旧度过滤）")
    freq = "daily"
    _lag = 1


@register
class HolderNumChg2(_HolderNumFactor):
    name = "holder_num_chg_2"
    description = ("两期累计股东户数变化（连续下降信号更强；PIT 按披露日，"
                   "180 日陈旧度过滤）")
    freq = "daily"
    _lag = 2
