"""高/低位放量事件簇（第三批 · 因子思路库 A5 · 2026-09-07）
=====================================
国盛《"量价淘金"选股因子系列研究(十八)》的日频近似版。

事件定义（t 日，全因果、只用 ≤t 信息）：
- 高位放量 ev_hi：成交量 ≥ 过去 60 有效交易日 vol 的 80 分位
                  且 close ≥ 过去 60 日 close 的 80 分位
- 低位放量 ev_lo：成交量 ≥ 过去 60 日 vol 的 80 分位
                  且 close ≤ 过去 60 日 close 的 20 分位

因子值 = 过去 20 有效交易日事件计数（ROWS 窗口含当日）。
研报语义"事件发生后 N 日内信号持续" ⇔ t 日值 = Σ 过去 N 日事件，
rolling 过去窗口实现，天然满足"事件首次发生前因子值必须为 0"
的方向性约束（上线闸门在审计中显式复核）。

工程要点：
- 分位用 QUANTILE_CONT 聚合窗口（支持 ROWS frame，完全因果）；
  不用 PERCENT_RANK/CUME_DIST——ranking 族忽略 frame 会扫全历史（前视）
- 60 日窗口有效行 < 45（次新/长期停牌）→ 分位不可靠，事件置 0 且
  20 日窗有效行 < 15 → 因子置 NULL（与库内 min_win 约定一致）
- 事件日方向：高位放量在 A 股常为出货/追高（预期负向），低位放量
  常为吸筹/反弹启动（预期正向）——最终方向一律由 IC 数据定
"""
from __future__ import annotations

from ..base import SqlFactor, universe_sql
from ..registry import register

# 分位基座：全部为因果滚动分位（ROWS frame 过去 59 行 + 当前行）
_EVENT_BASE = """
WITH base AS (
    SELECT date, code, close, vol,
           COUNT(*) OVER w60 AS n60,
           QUANTILE_CONT(vol, 0.8) OVER w60 AS vol_q80,
           QUANTILE_CONT(close, 0.8) OVER w60 AS px_q80,
           QUANTILE_CONT(close, 0.2) OVER w60 AS px_q20
    FROM kline_daily
    WHERE close > 0 AND vol > 0 {usql}
    WINDOW w60 AS (PARTITION BY code ORDER BY date
                   ROWS BETWEEN 59 PRECEDING AND CURRENT ROW)
), ev AS (
    SELECT date, code,
           CASE WHEN n60 >= 45 AND vol >= vol_q80 AND close >= px_q80
                THEN 1 ELSE 0 END AS ev_hi,
           CASE WHEN n60 >= 45 AND vol >= vol_q80 AND close <= px_q20
                THEN 1 ELSE 0 END AS ev_lo
    FROM base
)
{expr}
"""


class _VolumeEventFactor(SqlFactor):
    """放量事件簇基类"""
    category = "volume_event"
    _ev: str = "ev_hi"  # 事件列

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = _EVENT_BASE.format(usql=usql, expr=self._expr)
        return sql, uparams

    @property
    def _expr(self) -> str:
        return f"""
SELECT date, code,
       CASE WHEN COUNT({self._ev}) OVER w20 >= 15
            THEN SUM({self._ev}) OVER w20 END AS value
FROM ev
WINDOW w20 AS (PARTITION BY code ORDER BY date
               ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
QUALIFY value IS NOT NULL
"""


@register
class EvHighVol20(_VolumeEventFactor):
    name = "ev_high_vol_20"
    description = "20日高位放量事件计数（量≥60日80分位且价≥80分位；追高/出货簇）"
    freq = "daily"
    _ev = "ev_hi"


@register
class EvLowVol20(_VolumeEventFactor):
    name = "ev_low_vol_20"
    description = "20日低位放量事件计数（量≥60日80分位且价≤20分位；吸筹/反弹簇）"
    freq = "daily"
    _ev = "ev_lo"
