"""隔夜/日内收益拆分族（第二批新因子 · 2026-09-06 因子思路库 A3）
=====================================
日频 OHLC 隐含两个独立信息段（中信建投《市场微观结构系列》思路的日频版）：

- overnight（隔夜）= open_t / close_{t-1} − 1（后复权口径，跨除权可比）
  —— 集中反映隔夜信息（公告/外盘/情绪），研报结论：隔夜+早盘信息正向预示
- intraday（日内）= close_t / open_t − 1（日内无除权，原始价比）
  —— 尾盘信息易过度反应、短期反转（负向预示）

一字板日（high = low）无日内信息且隔夜=涨跌停值失真 → 剔除（置 NULL）。

因子：
- overnight_mom_20      20日隔夜收益均值（隔夜动量）
- intraday_mom_5        5日日内收益均值（短期反转候选，方向由 IC 定）
- overnight_ratio_20    20日 |隔夜| / (|隔夜|+|日内|)：隔夜信息占比
- overnight_vol_ratio_20 20日 std(隔夜) / std(日内)：隔夜/日内波动结构比

窗口：ROWS 按行数（与现有因子一致），要求窗口内有效值 >= 15（20日窗）
/ >= 4（5日窗），不足置 NULL。
"""
from __future__ import annotations

from ..base import SqlFactor, universe_sql
from ..registry import register

# 拆分基座：一字板剔除 + 后复权隔夜收益
_SPLIT_BASE = """
WITH base AS (
    SELECT date, code,
           CASE WHEN high > low THEN
                (open * adj_factor)
                / NULLIF(LAG(close * adj_factor) OVER w, 0) - 1.0
           END AS overnight,
           CASE WHEN high > low THEN
                close / NULLIF(open, 0) - 1.0
           END AS intraday
    FROM kline_daily
    WHERE open > 0 AND close > 0 {usql}
    WINDOW w AS (PARTITION BY code ORDER BY date)
)
{expr}
"""


class _SplitFactor(SqlFactor):
    """隔夜/日内拆分基类"""
    category = "intraday_split"
    _expr: str = "NULL"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = _SPLIT_BASE.format(usql=usql, expr=self._expr)
        return sql, uparams


@register
class OvernightMom20(_SplitFactor):
    name = "overnight_mom_20"
    description = "20日隔夜收益均值（隔夜动量；一字板日剔除，后复权）"
    freq = "daily"
    _expr = """
SELECT date, code,
       CASE WHEN COUNT(overnight) OVER w20 >= 15
            THEN AVG(overnight) OVER w20 END AS value
FROM base
WINDOW w20 AS (PARTITION BY code ORDER BY date
               ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
QUALIFY value IS NOT NULL
"""


# 2026-09-07 冗余清理：intraday_mom_5（ρ=−0.88 镜像 reversal_5）、
# overnight_ratio_20（ρ=0.90）、overnight_vol_ratio_20（弱且互冗余）
# 已按用户裁决删除（定义见 git 历史，compute_factor 可随时重建）
