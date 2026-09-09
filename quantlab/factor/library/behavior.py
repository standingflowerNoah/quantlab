"""方正金工多因子系列 · 日频近似因子（研报因子挖掘计划 Phase 1，2026-09-10）
=====================================
来源：方正证券《多因子选股系列研究》之三/四/五/六/九（曹春晓等）。
重要口径说明：原报告中除"球队硬币"（之四）外均为**分钟频**构造；
本文件实现的是可由日频 OHLCV 忠实移植的**日频近似版**，已在
research/研报因子挖掘计划.md 中标注口径降级。纯分钟版（潮汐/草木皆兵/
花隐林间/一视同仁/适度冒险等）归 Phase 2 用分钟湖实现。

全部因果（只用 ≤t 信息），窗口内有效行不足时置 NULL（库内 min_win 约定）。
方向约定：研报语义方向写在 description，最终方向一律由 IC 数据定。
"""
from __future__ import annotations

from ..base import SqlFactor, universe_sql
from ..registry import register

# ── 公共基座：前复权开/收盘 + 三维度收益拆分 ──────────────────────────
# r_day = 日间收益（昨收→今收），r_on = 隔夜收益（昨收→今开），r_in = 日内收益（今开→今收）
_PX_RET = """
WITH px AS (
    SELECT date, code,
           open * adj_factor AS open_adj,
           close * adj_factor AS close_adj,
           high, low
    FROM kline_daily
    WHERE close > 0 AND open > 0 {usql}
),
ret AS (
    SELECT date, code, open_adj, close_adj, high, low,
           close_adj / LAG(close_adj) OVER w - 1 AS r_day,
           open_adj / LAG(close_adj) OVER w - 1 AS r_on,
           close_adj / open_adj - 1 AS r_in
    FROM px
    WINDOW w AS (PARTITION BY code ORDER BY date)
)
"""

_W20 = "PARTITION BY code ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW"


# ════════════════════════════════════════════════════════════════════
# 1. 球队硬币（系列之四，日频原版逻辑）
#    可知性：波动率下降 → 硬币型（预期动量）；波动率上升 → 球队型（预期反转）。
#    三维度（日间/日内/隔夜）各取 20 日累计收益，按波动率变化翻转方向后等权合成。
#    研报绩效：RankIC -9.67%，RankICIR -4.73（负向因子）
# ════════════════════════════════════════════════════════════════════
_TEAM_COIN = _PX_RET + """
, vs AS (
    SELECT date, code, r_day, r_on, r_in,
           STDDEV_SAMP(r_day) OVER w20 AS v_now,
           STDDEV_SAMP(r_day) OVER wprev AS v_prev
    FROM ret
    WINDOW
      w20 AS ({w20}),
      wprev AS (PARTITION BY code ORDER BY date
                ROWS BETWEEN 39 PRECEDING AND 20 PRECEDING)
),
agg AS (
    SELECT date, code, v_now, v_prev, n20,
           CASE WHEN v_now IS NOT NULL AND v_prev IS NOT NULL AND v_now < v_prev
                THEN (s_day + s_on + s_in) / 3.0              -- 硬币型：动量方向
                ELSE -(s_day + s_on + s_in) / 3.0 END AS value  -- 球队型/未知：反转方向
    FROM (
        SELECT date, code, v_now, v_prev,
               COUNT(r_day) OVER w20 AS n20,
               SUM(r_day) OVER w20 AS s_day,
               SUM(r_on) OVER w20 AS s_on,
               SUM(r_in) OVER w20 AS s_in
        FROM vs
        WINDOW w20 AS ({w20})
    )
)
SELECT date, code, value
FROM agg
WHERE n20 >= 15 AND value IS NOT NULL
""".format(w20=_W20)


class TeamCoin20(SqlFactor):
    name = "team_coin_20"
    description = ("球队硬币20日（方正之四日频版）：三维度反转按波动率变化翻转，"
                   "波动下降→动量、波动上升→反转；研报RankIC -9.67%")
    category = "price"
    freq = "daily"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        return _TEAM_COIN.format(usql=usql), uparams


register(TeamCoin20)


# ════════════════════════════════════════════════════════════════════
# 2. 勇攀高峰（系列之三，日频近似）
#    Moreira-Muir：异常高波动时段的收益波动比与波动率协方差 =
#    对高波动的风险补偿充足度。日频近似：Parkinson 高低价方差代理，
#    过去 20 日中 pv ≥ mean+std 的"异常高波日"子集上算 cov(r, pv)。
#    研报绩效：RankIC 5.62%，RankICIR 4.47（正向）
# ════════════════════════════════════════════════════════════════════
_CLIMB_PEAK = _PX_RET + """
, pvcalc AS (
    SELECT date, code, r_day AS r,
           0.5 * POWER(LN(high / low), 2) AS pv
    FROM ret
    WHERE high > 0 AND low > 0 AND r_day IS NOT NULL
),
stats AS (
    SELECT date, code, r, pv,
           AVG(pv) OVER w20 AS m_pv,
           STDDEV_SAMP(pv) OVER w20 AS s_pv,
           COUNT(pv) OVER w20 AS n20
    FROM pvcalc
    WINDOW w20 AS ({w20})
),
flag AS (
    SELECT date, code, r, pv, n20,
           CASE WHEN s_pv > 0 AND pv >= m_pv + s_pv THEN 1 ELSE 0 END AS hi
    FROM stats
),
cov AS (
    SELECT date, code, n20,
           CASE WHEN COUNT(CASE WHEN hi = 1 THEN 1 END) OVER w20 >= 3
                THEN AVG(CASE WHEN hi = 1 THEN r * pv END) OVER w20
                   - AVG(CASE WHEN hi = 1 THEN r END) OVER w20
                   * AVG(CASE WHEN hi = 1 THEN pv END) OVER w20
           END AS value
    FROM flag
    WINDOW w20 AS ({w20})
)
SELECT date, code, value
FROM cov
WHERE n20 >= 15 AND value IS NOT NULL
""".format(w20=_W20)


class ClimbPeak20(SqlFactor):
    name = "climb_peak_20"
    description = ("勇攀高峰20日（方正之三日频近似）：异常高波日子集的收益波动比-波动率"
                   "协方差（高波动风险补偿充足度）；研报RankIC 5.62%")
    category = "price"
    freq = "daily"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        return _CLIMB_PEAK.format(usql=usql), uparams


register(ClimbPeak20)


# ════════════════════════════════════════════════════════════════════
# 3. 云开雾散 / 波动率的波动率（系列之五，日频近似）
#    模糊性厌恶：vol-of-vol 高 → 投资者急于减仓 → 未来收益低。
#    factor = STD(pv,20)/AVG(pv,20)（Parkinson 方差的变异系数）。
#    研报为负向因子。
# ════════════════════════════════════════════════════════════════════
_VOLVOL = _PX_RET + """
, pvcalc AS (
    SELECT date, code,
           0.5 * POWER(LN(high / low), 2) AS pv
    FROM ret
    WHERE high > 0 AND low > 0
)
SELECT date, code,
       CASE WHEN COUNT(pv) OVER w20 >= 15 AND AVG(pv) OVER w20 > 0
            THEN STDDEV_SAMP(pv) OVER w20 / AVG(pv) OVER w20 END AS value
FROM pvcalc
WINDOW w20 AS ({w20})
QUALIFY value IS NOT NULL
""".format(w20=_W20)


class VolVol20(SqlFactor):
    name = "volvol_20"
    description = ("云开雾散20日（方正之五日频近似）：Parkinson 高低价方差的20日变异系数"
                   "（波动率的波动率，模糊厌恶）；研报负向")
    category = "price"
    freq = "daily"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        return _VOLVOL.format(usql=usql), uparams


register(VolVol20)


# ── 飞蛾扑火公共基座（系列之六，日频近似）─────────────────────────────
# 跳跃 = 隔夜跳空绝对值 > 过去60日（不含当日）跳空标准差 2 倍；
# 振幅 = (high-low)/close（同日，复权因子相消）。
_JUMP_BASE = _PX_RET + """
, gap AS (
    SELECT date, code, open_adj, close_adj, high, low,
           open_adj / LAG(close_adj) OVER w - 1 AS g,
           (high - low) / close_adj AS amp
    FROM ret
    WINDOW w AS (PARTITION BY code ORDER BY date)
),
flag AS (
    SELECT date, code, amp, g,
           CASE WHEN ABS(g) > 2 * STDDEV_SAMP(g) OVER (
                     PARTITION BY code ORDER BY date
                     ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING)
                THEN 1 ELSE 0 END AS jmp
    FROM gap
)
"""


class _JumpBase(SqlFactor):
    category = "price"
    freq = "daily"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        return self._BASE.format(usql=usql, w20=_W20), uparams


class AmpNonJump20(_JumpBase):
    name = "amp_nonjump_20"
    description = ("飞蛾扑火-非跳跃振幅20日（方正之六日频近似）：剔除隔夜跳空跳跃日后的"
                   "20日平均振幅；研报逻辑=剔除跳跃后振幅更纯净")
    _BASE = _JUMP_BASE + """
SELECT date, code,
       CASE WHEN COUNT(CASE WHEN jmp = 0 THEN 1 END) OVER w20 >= 10
            THEN AVG(CASE WHEN jmp = 0 THEN amp END) OVER w20 END AS value
FROM flag
WINDOW w20 AS ({w20})
QUALIFY value IS NOT NULL
"""


class JumpShare20(_JumpBase):
    name = "jump_share_20"
    description = ("飞蛾扑火-跳跃振幅占比20日（方正之六日频近似）：跳跃日振幅合计/"
                   "总振幅（跳跃在振幅中的权重）")
    _BASE = _JUMP_BASE + """
SELECT date, code,
       CASE WHEN COUNT(amp) OVER w20 >= 10
            THEN SUM(CASE WHEN jmp = 1 THEN amp END) OVER w20
                 / NULLIF(SUM(amp) OVER w20, 0) END AS value
FROM flag
WINDOW w20 AS ({w20})
QUALIFY value IS NOT NULL
"""


register(AmpNonJump20)
register(JumpShare20)


# ════════════════════════════════════════════════════════════════════
# 6. 水中行舟（系列之九，日频近似）
#    个股成交额变化对市场成交额变化的 20 日跟随相关性。
#    随波逐流（跟随性高）信息含量低；独立行情（逆水行舟）更可贵。
# ════════════════════════════════════════════════════════════════════
_BOAT = """
WITH mkt AS (
    SELECT date, LN(SUM(amount)) AS lm
    FROM kline_daily
    WHERE close > 0 AND vol > 0 AND amount > 0
    GROUP BY date
),
mdm AS (
    SELECT date, lm - LAG(lm) OVER (ORDER BY date) AS dm
    FROM mkt
),
stk AS (
    SELECT date, code, amount
    FROM kline_daily
    WHERE close > 0 AND vol > 0 AND amount > 0 {usql}
),
joined AS (
    SELECT s.date, s.code,
           LN(s.amount) - LAG(LN(s.amount)) OVER w AS da,
           k.dm
    FROM stk s
    JOIN mdm k USING (date)
    WINDOW w AS (PARTITION BY s.code ORDER BY s.date)
),
pair AS (
    SELECT date, code,
           CASE WHEN COUNT(da) OVER w20 >= 15
                     AND STDDEV_SAMP(da) OVER w20 > 0
                     AND STDDEV_SAMP(dm) OVER w20 > 0
                THEN (AVG(da * dm) OVER w20 - AVG(da) OVER w20 * AVG(dm) OVER w20)
                     / (STDDEV_SAMP(da) OVER w20 * STDDEV_SAMP(dm) OVER w20)
           END AS value
    FROM joined
    WHERE da IS NOT NULL AND dm IS NOT NULL
    WINDOW w20 AS ({w20})
)
SELECT date, code, value
FROM pair
WHERE value IS NOT NULL
"""


class BoatFollow20(SqlFactor):
    name = "boat_follow_20"
    description = ("水中行舟20日（方正之九日频近似）：个股对市场成交额变化的20日"
                   "跟随相关性（随波逐流度）；独立行情个股信息更足")
    category = "volume"
    freq = "daily"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        return _BOAT.format(usql=usql, w20=_W20), uparams


register(BoatFollow20)
