"""基本面因子 · 第一批（2026-09-11 立项，westock finance_q + fsdb valuation_daily）
=================================================================================
数据源（本批新增的两个 lake 数据集，parquet 直读，不进主库）：
- data/lake/clean/fundamental/finance_q/{income,balance,cashflow}/part-*.parquet
  westock（腾讯源）三大表，2015 起 46 期/只，InfoPublDate=真实披露日
- data/lake/clean/fundamental/valuation_daily/part-*.parquet
  fsdb 日K 估值：pe_ttm/pb/total_mv/float_mv/float_share，2021 起

PIT 口径：财报值在 InfoPublDate 后可见；同股同日多期披露取最新报告期
（QUALIFY，与 earnings_surprise.py 同模式）。

首批因子（详见 research/fundamental-factors/研究设计.md §3）：
- 估值：ep(1/pe_ttm) bp(1/pb) sp_ttm cfp_ttm
- 质量：roe_ttm roe_cut roic gpoa accruals2 ocf_to_profit
- 成长：np_q_yoy rev_q_yoy

二期再入：sue_v2/sur（单季 SUE 需窗口差分质量验证）、roe_chg/gpoa_chg
（报告期 LAG4 窗口）、ep_pct5（滚动分位）、GARP 合成。
"""
from __future__ import annotations

from ..base import SqlFactor, universe_sql
from ..registry import register

import os

_DATA_ROOT = os.environ.get(
    "QUANTLAB_LAKE",
    "C:/Users/53497/WorkBuddy/2026-09-02-23-42-28/quantlab/data/lake/clean")
_FQ = f"{_DATA_ROOT}/fundamental/finance_q"
_VD = f"{_DATA_ROOT}/fundamental/valuation_daily/part-*.parquet"

# ── 公共 CTE ─────────────────────────────────────────────────────
# income 阶梯：PIT 三重防线
# ① InfoPublDate IS NOT NULL 且 > EndDate（剔空壳行/非法行）
# ② 压制剔除：若存在更晚报告期且其披露日更早（更正披露的旧期），
#    该旧期行被剔除——否则 ASOF 按 pub 取行时，晚披露的旧期会错误
#    覆盖已披露的新报告期（2026-09-11 质检发现，滞后>250天的
#    更正行约占 5%）
# ③ 同股同披露日多期 → 取最新报告期（QUALIFY）
_INCOME_LADDER = f"""
WITH inc AS (
    SELECT code, EndDate AS rp, InfoPublDate AS pub,
           OperatingRevenue, OperatingRevenueTTM,
           OperatingProfit,
           NPParentCompanyOwners, NPParentCompanyOwnersTTM,
           NPParentCompanyOwners_Q,
           GrossProfitTTM, ROETTM, ROECut, ROIC,
           NPParentCompanyYOY_Q, OperatingRevenueGrowRate_Q
    FROM read_parquet('{_FQ}/income/part-*.parquet', union_by_name=true)
    WHERE InfoPublDate IS NOT NULL AND InfoPublDate > EndDate
), sup AS (
    SELECT *,
           MIN(pub) OVER (PARTITION BY code ORDER BY rp DESC
                          ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                   ) AS later_rp_min_pub
    FROM inc
), kept AS (
    SELECT * FROM sup
    WHERE later_rp_min_pub IS NULL OR later_rp_min_pub > pub
), g AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY code, pub ORDER BY rp DESC) AS rk
    FROM kept QUALIFY rk = 1
)
"""

# income+balance+cashflow 合并阶梯（质量复合因子原料；PIT 防线同上）
_FIN_LADDER = f"""
WITH i AS (
    SELECT code, EndDate AS rp, InfoPublDate AS pub,
           NPParentCompanyOwnersTTM AS np_ttm, GrossProfitTTM,
           ROW_NUMBER() OVER (PARTITION BY code, EndDate ORDER BY pub DESC) AS rk
    FROM read_parquet('{_FQ}/income/part-*.parquet', union_by_name=true)
    WHERE InfoPublDate IS NOT NULL AND InfoPublDate > EndDate
), b AS (
    SELECT code, EndDate AS rp,
           TotalCurrentAssets + TotalNonCurrentAssets AS total_assets,
           SEWithoutMI,
           ROW_NUMBER() OVER (PARTITION BY code, EndDate ORDER BY InfoPublDate DESC) AS rk
    FROM read_parquet('{_FQ}/balance/part-*.parquet', union_by_name=true)
    WHERE InfoPublDate IS NOT NULL AND InfoPublDate > EndDate
), c AS (
    SELECT code, EndDate AS rp, NetOperateCashFlowTTM AS ocf_ttm,
           ROW_NUMBER() OVER (PARTITION BY code, EndDate ORDER BY InfoPublDate DESC) AS rk
    FROM read_parquet('{_FQ}/cashflow/part-*.parquet', union_by_name=true)
    WHERE InfoPublDate IS NOT NULL AND InfoPublDate > EndDate
), fin AS (
    SELECT i.code AS code, i.pub AS pub, i.rp AS rp,
           i.np_ttm, i.GrossProfitTTM, b.total_assets, b.SEWithoutMI, c.ocf_ttm,
           ROW_NUMBER() OVER (PARTITION BY i.code, i.pub ORDER BY i.rp DESC) AS rk2
    FROM i JOIN b ON i.code=b.code AND i.rp=b.rp AND b.rk=1
           JOIN c ON i.code=c.code AND i.rp=c.rp AND c.rk=1
    WHERE i.rk = 1
), sup AS (
    SELECT *,
           MIN(pub) OVER (PARTITION BY code ORDER BY rp DESC
                          ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                   ) AS later_rp_min_pub
    FROM fin
), g AS (
    SELECT * FROM sup
    WHERE (later_rp_min_pub IS NULL OR later_rp_min_pub > pub) AND rk2 = 1
)
"""


class _LadderFactor(SqlFactor):
    """财报阶梯因子基类：子类给 _expr（基于 ladder 列的 SQL 表达式）"""
    category = "fundamental"
    freq = "daily"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        expr = self._expr
        sql = f"""
{_INCOME_LADDER}
SELECT k.date, g.code, ({expr}) AS value
FROM kline_daily k
ASOF JOIN g ON g.code = k.code AND g.pub <= k.date
WHERE ({expr}) IS NOT NULL AND isfinite({expr}) {usql}
"""
        return sql, uparams


@register
class RoeTtm(_LadderFactor):
    name = "roe_ttm"
    description = "ROE TTM（westock 财报，PIT=InfoPublDate）"
    _expr = "g.ROETTM"


@register
class RoeCut(_LadderFactor):
    name = "roe_cut"
    description = "扣非 ROE（剔除经常性损益，PIT）"
    _expr = "g.ROECut"


@register
class Roic(_LadderFactor):
    name = "roic"
    description = "ROIC 投入资本回报率（PIT）"
    _expr = "g.ROIC"


@register
class NpQYoy(_LadderFactor):
    name = "np_q_yoy"
    description = "单季归母净利同比（PIT）"
    _expr = "g.NPParentCompanyYOY_Q"


@register
class RevQYoy(_LadderFactor):
    name = "rev_q_yoy"
    description = "单季营收同比（PIT）"
    _expr = "g.OperatingRevenueGrowRate_Q"


# ── 质量复合（income+balance+cashflow 三表合并阶梯）──────────────
class _FinLadderFactor(SqlFactor):
    category = "fundamental"
    freq = "daily"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        expr = self._expr
        sql = f"""
{_FIN_LADDER}
SELECT k.date, g.code, ({expr}) AS value
FROM kline_daily k
ASOF JOIN g ON g.code = k.code AND g.pub <= k.date
WHERE ({expr}) IS NOT NULL AND isfinite({expr}) {usql}
"""
        return sql, uparams


@register
class Gpoa(_FinLadderFactor):
    name = "gpoa"
    description = "毛利/总资产（GrossProfitTTM/TA，定价权，PIT）"
    _expr = "g.GrossProfitTTM / NULLIF(g.total_assets, 0)"


@register
class Accruals2(_FinLadderFactor):
    name = "accruals2"
    description = "应计质量 (NI_TTM−OCF_TTM)/TA（Sloan，负向，PIT）"
    _expr = ("(COALESCE(g.np_ttm,0) - COALESCE(g.ocf_ttm,0)) "
             "/ NULLIF(g.total_assets, 0)")


@register
class OcfToProfit(_FinLadderFactor):
    name = "ocf_to_profit"
    description = "经营现金流/净利 TTM（利润现金支撑度，PIT）"
    _expr = "g.ocf_ttm / NULLIF(g.np_ttm, 0)"


# ── 估值（fsdb 日频估值直读 + 财报 TTM/市值）────────────────────
@register
class Ep(SqlFactor):
    name = "ep"
    description = "EP = 1/pe_ttm（fsdb 日频，2021 起）"
    category = "value"
    freq = "daily"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
SELECT date, code, 1.0/pe_ttm AS value
FROM read_parquet('{_VD}')
WHERE pe_ttm IS NOT NULL AND pe_ttm != 0 AND isfinite(1.0/pe_ttm) {usql}
"""
        return sql, uparams


@register
class Bp(SqlFactor):
    name = "bp"
    description = "BP = 1/pb（fsdb 日频，2021 起）"
    category = "value"
    freq = "daily"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
SELECT date, code, 1.0/pb AS value
FROM read_parquet('{_VD}')
WHERE pb IS NOT NULL AND pb != 0 AND isfinite(1.0/pb) {usql}
"""
        return sql, uparams


@register
class SpTtm(SqlFactor):
    name = "sp_ttm"
    description = "SP = 营收TTM/总市值（fsdb 市值 + westock TTM，PIT）"
    category = "value"
    freq = "daily"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
{_INCOME_LADDER}
SELECT k.date, g.code,
       g.OperatingRevenueTTM / NULLIF(v.total_mv, 0) AS value
FROM kline_daily k
JOIN read_parquet('{_VD}') v ON v.code = k.code AND v.date = k.date
ASOF JOIN g ON g.code = k.code AND g.pub <= k.date
WHERE g.OperatingRevenueTTM IS NOT NULL
  AND v.total_mv > 0 AND isfinite(g.OperatingRevenueTTM / v.total_mv) {usql}
"""
        return sql, uparams


@register
class CfpTtm(SqlFactor):
    name = "cfp_ttm"
    description = "CFP = 经营现金流TTM/总市值（fsdb 市值 + westock TTM，PIT）"
    category = "value"
    freq = "daily"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
WITH c AS (
    SELECT code, EndDate AS rp, InfoPublDate AS pub, NetOperateCashFlowTTM AS ocf_ttm
    FROM read_parquet('{_FQ}/cashflow/part-*.parquet', union_by_name=true)
    WHERE InfoPublDate IS NOT NULL AND InfoPublDate > EndDate
), sup AS (
    SELECT *,
           MIN(pub) OVER (PARTITION BY code ORDER BY rp DESC
                          ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                   ) AS later_rp_min_pub
    FROM c
), g AS (
    SELECT * FROM sup
    WHERE later_rp_min_pub IS NULL OR later_rp_min_pub > pub
    QUALIFY ROW_NUMBER() OVER (PARTITION BY code, pub ORDER BY rp DESC) = 1
)
SELECT k.date, g.code,
       g.ocf_ttm / NULLIF(v.total_mv, 0) AS value
FROM kline_daily k
JOIN read_parquet('{_VD}') v ON v.code = k.code AND v.date = k.date
ASOF JOIN g ON g.code = k.code AND g.pub <= k.date
WHERE g.ocf_ttm IS NOT NULL AND v.total_mv > 0
  AND isfinite(g.ocf_ttm / NULLIF(v.total_mv, 0)) {usql}
"""
        return sql, uparams


# ════════════════════════════════════════════════════════════════
# 二批因子（2026-09-11 二期：SUE 族 / 改善族 / 时序估值 / GARP）
# ════════════════════════════════════════════════════════════════
# SUE 口径（z-score 简化版，研报常用）：
#   SUE_t = (Q_t − mean(Q_{t−1..t−8})) / std(Q_{t−1..t−8})，clip ±10
#   与东财版 Foster-Olsen（delta 框架）口径不同，作长样本对照。
# 序列口径：报告期空间计算（非 pub 阶梯）——每 (code, rp) 取首次披露
#   （MIN pub）行：SUE 刻画"公告日惊喜"，原料用首次披露值；
#   压制剔除沿用（更正披露不回填历史）。缺期防护：LAG 距离≠整季 → NULL。
_SUE_COMMON = f"""
WITH inc AS (
    SELECT code, EndDate AS rp, InfoPublDate AS pub,
           NPParentCompanyOwners_Q AS q_np,
           OperatingRevenue AS rev_cum, ROETTM
    FROM read_parquet('{_FQ}/income/part-*.parquet', union_by_name=true)
    WHERE InfoPublDate IS NOT NULL AND InfoPublDate > EndDate
), sup AS (
    SELECT *,
           MIN(pub) OVER (PARTITION BY code ORDER BY rp DESC
                          ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                   ) AS later_rp_min_pub
    FROM inc
), kept AS (
    SELECT * FROM sup
    WHERE later_rp_min_pub IS NULL OR later_rp_min_pub > pub
), fp AS (            -- 同 rp 多次披露 → 取首次
    SELECT *, ROW_NUMBER() OVER (PARTITION BY code, rp ORDER BY pub) AS rk
    FROM kept
), q_seq AS (
    SELECT code, rp, pub, q_np, rev_cum, ROETTM,
           LAG(rp) OVER (PARTITION BY code ORDER BY rp) AS rp_lag,
           LAG(rev_cum) OVER (PARTITION BY code ORDER BY rp) AS rev_lag,
           LAG(ROETTM, 4) OVER (PARTITION BY code ORDER BY rp) AS roe_lag4,
           LAG(rp, 4) OVER (PARTITION BY code ORDER BY rp) AS rp_lag4
    FROM fp WHERE rk = 1
), sq AS (            -- 单季值（缺期防护）
    SELECT code, rp, pub, q_np AS np_q, ROETTM,
           CASE WHEN quarter(rp) = 1 THEN rev_cum
                WHEN date_diff('month', rp_lag, rp) = 3
                     THEN rev_cum - rev_lag
                ELSE NULL END AS rev_q,
           CASE WHEN date_diff('month', rp_lag4, rp) = 12
                THEN ROETTM - roe_lag4 ELSE NULL END AS roe_chg_v
    FROM q_seq
)
"""


class _SueFactor(SqlFactor):
    """SUE 族基类：子类给 _col（sq 表列名）与 _win（窗口期数）"""
    category = "surprise"
    freq = "daily"
    _col: str = ""
    _win: int = 8
    _min_n: int = 6

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
{_SUE_COMMON}
, w AS (
    SELECT code, rp, pub, {self._col} AS v,
           AVG({self._col}) OVER (
               PARTITION BY code ORDER BY rp
               ROWS BETWEEN {self._win} PRECEDING AND 1 PRECEDING) AS mu,
           STDDEV({self._col}) OVER (
               PARTITION BY code ORDER BY rp
               ROWS BETWEEN {self._win} PRECEDING AND 1 PRECEDING) AS sd,
           COUNT({self._col}) OVER (
               PARTITION BY code ORDER BY rp
               ROWS BETWEEN {self._win} PRECEDING AND 1 PRECEDING) AS n
    FROM sq WHERE {self._col} IS NOT NULL
), sue AS (
    SELECT code, rp, pub,
           LEAST(10, GREATEST(-10, (v - mu) / NULLIF(sd, 0))) AS value
    FROM w WHERE n >= {self._min_n}
), g AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY code, pub ORDER BY rp DESC) AS rk
    FROM sue QUALIFY rk = 1
)
SELECT k.date, g.code, g.value
FROM kline_daily k
ASOF JOIN g ON g.code = k.code AND g.pub <= k.date
WHERE g.value IS NOT NULL AND isfinite(g.value) {usql}
"""
        return sql, uparams


@register
class SueV2(_SueFactor):
    name = "sue_v2"
    description = "单季净利 SUE（westock 长样本 z 版，2015 起，PIT=首次披露日）"
    _col = "np_q"


@register
class Sur(_SueFactor):
    name = "sur"
    description = "单季营收 SUE（SUR，难操纵盈余信号，PIT）"
    _col = "rev_q"



# ── 改善族：roe_chg（sq 表已有差分列，阶梯直读）──────────────────
@register
class RoeChg(_SueFactor):
    """ΔROE：ROETTM − 4 期前 ROETTM（阶梯前向填充，非 z-score）"""
    name = "roe_chg"
    description = "ROE 改善（ROETTM − LAG4，PIT，缺期防护）"
    category = "quality"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
{_SUE_COMMON}
, g AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY code, pub ORDER BY rp DESC) AS rk
    FROM (SELECT * FROM sq WHERE roe_chg_v IS NOT NULL) t
    QUALIFY rk = 1
)
SELECT k.date, g.code, g.roe_chg_v AS value
FROM kline_daily k
ASOF JOIN g ON g.code = k.code AND g.pub <= k.date
WHERE g.roe_chg_v IS NOT NULL AND isfinite(g.roe_chg_v) {usql}
"""
        return sql, uparams


# ── 三表 GPOA 报告期序列（sue_gpoa / gpoa_chg 共用）─────────────
_GPOA_COMMON = f"""
WITH i AS (
    SELECT code, EndDate AS rp, InfoPublDate AS pub, GrossProfitTTM
    FROM read_parquet('{_FQ}/income/part-*.parquet', union_by_name=true)
    WHERE InfoPublDate IS NOT NULL AND InfoPublDate > EndDate
), b AS (
    SELECT code, EndDate AS rp, InfoPublDate,
           TotalCurrentAssets + TotalNonCurrentAssets AS total_assets
    FROM read_parquet('{_FQ}/balance/part-*.parquet', union_by_name=true)
    WHERE InfoPublDate IS NOT NULL AND InfoPublDate > EndDate
), ji AS (
    SELECT code, rp, pub, GrossProfitTTM,
           ROW_NUMBER() OVER (PARTITION BY code, rp ORDER BY pub) AS rk
    FROM i
), jb AS (
    SELECT code, rp, total_assets,
           ROW_NUMBER() OVER (PARTITION BY code, rp ORDER BY InfoPublDate) AS rk
    FROM b
), gp AS (
    SELECT ji.code AS code, ji.rp AS rp, ji.pub AS pub,
           ji.GrossProfitTTM / NULLIF(jb.total_assets, 0) AS gpoa,
           LAG(ji.rp, 4) OVER (PARTITION BY ji.code ORDER BY ji.rp) AS rp_lag4,
           LAG(ji.GrossProfitTTM / NULLIF(jb.total_assets, 0), 4)
               OVER (PARTITION BY ji.code ORDER BY ji.rp) AS gpoa_lag4
    FROM ji JOIN jb ON ji.code = jb.code AND ji.rp = jb.rp AND jb.rk = 1
    WHERE ji.rk = 1 AND jb.total_assets > 0
)
"""


@register
class SueGpoa(SqlFactor):
    """GPOA TTM 的 8 期 z-score（质量维度的超预期，开源 2025）"""
    name = "sue_gpoa"
    description = "GPOA 8 期 z-score（质量超预期，三表，PIT=首次披露日）"
    category = "surprise"
    freq = "daily"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
{_GPOA_COMMON}
, w AS (
    SELECT code, rp, pub, gpoa AS v,
           AVG(gpoa) OVER (PARTITION BY code ORDER BY rp
               ROWS BETWEEN 8 PRECEDING AND 1 PRECEDING) AS mu,
           STDDEV(gpoa) OVER (PARTITION BY code ORDER BY rp
               ROWS BETWEEN 8 PRECEDING AND 1 PRECEDING) AS sd,
           COUNT(gpoa) OVER (PARTITION BY code ORDER BY rp
               ROWS BETWEEN 8 PRECEDING AND 1 PRECEDING) AS n
    FROM gp WHERE gpoa IS NOT NULL
), z AS (
    SELECT code, rp, pub,
           LEAST(10, GREATEST(-10, (v - mu) / NULLIF(sd, 0))) AS value
    FROM w WHERE n >= 6
), g AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY code, pub ORDER BY rp DESC) AS rk
    FROM z QUALIFY rk = 1
)
SELECT k.date, g.code, g.value
FROM kline_daily k
ASOF JOIN g ON g.code = k.code AND g.pub <= k.date
WHERE g.value IS NOT NULL AND isfinite(g.value) {usql}
"""
        return sql, uparams


@register
class GpoaChg(SqlFactor):
    """ΔGPOA：GPOA − 4 期前 GPOA（雪球 ΔQuality 子项）"""
    name = "gpoa_chg"
    description = "GPOA 改善（GPOA − LAG4，三表，PIT，缺期防护）"
    category = "quality"
    freq = "daily"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
{_GPOA_COMMON}
, d AS (
    SELECT code, rp, pub,
           CASE WHEN date_diff('month', rp_lag4, rp) = 12
                THEN gpoa - gpoa_lag4 ELSE NULL END AS chg
    FROM gp
), g AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY code, pub ORDER BY rp DESC) AS rk
    FROM d WHERE chg IS NOT NULL QUALIFY rk = 1
)
SELECT k.date, g.code, g.chg AS value
FROM kline_daily k
ASOF JOIN g ON g.code = k.code AND g.pub <= k.date
WHERE g.chg IS NOT NULL AND isfinite(g.chg) {usql}
"""
        return sql, uparams


# ── 时序估值：ep_z5（EP 5 年滚动 z-score）────────────────────────
@register
class EpZ5(SqlFactor):
    """EP 相对自身过去 5 年（1250 交易日）的 z-score。
    时序分位的单调近似（截面 rank 下 z 与分位高度共线）。
    捕捉"自身历史便宜"而非"横向便宜"——与截面 ep 正交。"""
    name = "ep_z5"
    description = "EP 5 年滚动 z-score（时序估值分位近似，2021 起）"
    category = "value"
    freq = "daily"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
WITH v AS (
    SELECT date, code, 1.0 / pe_ttm AS ep
    FROM read_parquet('{_VD}')
    WHERE pe_ttm IS NOT NULL AND pe_ttm != 0 AND isfinite(1.0/pe_ttm)
), z AS (
    SELECT date, code, ep,
           AVG(ep) OVER (PARTITION BY code ORDER BY date
               ROWS BETWEEN 1250 PRECEDING AND 1 PRECEDING) AS mu,
           STDDEV(ep) OVER (PARTITION BY code ORDER BY date
               ROWS BETWEEN 1250 PRECEDING AND 1 PRECEDING) AS sd,
           COUNT(ep) OVER (PARTITION BY code ORDER BY date
               ROWS BETWEEN 1250 PRECEDING AND 1 PRECEDING) AS n
    FROM v
)
SELECT date, code, (ep - mu) / NULLIF(sd, 0) AS value
FROM z WHERE n >= 500 AND sd > 0 {usql}
"""
        return sql, uparams


# ── GARP：ep × np_q_yoy 日截面 rank 乘积（PB-ROE 象限法简化版）───
@register
class Garp(SqlFactor):
    """GARP（Growth at Reasonable Price）：双 rank 高=便宜且成长。
    rank 乘积组合——双高筛选，非线性（一高一低被惩罚）。"""
    name = "garp"
    description = "GARP = ep_rank × np_q_yoy_rank（日截面，双高筛选）"
    category = "value"
    freq = "daily"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
{_INCOME_LADDER}
, e AS (
    SELECT date, code, 1.0 / pe_ttm AS ep
    FROM read_parquet('{_VD}')
    WHERE pe_ttm IS NOT NULL AND pe_ttm != 0 AND isfinite(1.0/pe_ttm)
), j AS (
    SELECT e.date AS date, e.code AS code, e.ep AS ep,
           g.NPParentCompanyYOY_Q AS npy
    FROM e
    ASOF JOIN g ON g.code = e.code AND g.pub <= e.date
    WHERE g.NPParentCompanyYOY_Q IS NOT NULL
), rk AS (
    SELECT date, code,
           RANK() OVER (PARTITION BY date ORDER BY ep) AS r_ep,
           RANK() OVER (PARTITION BY date ORDER BY npy) AS r_npy
    FROM j
)
SELECT date, code, r_ep * r_npy AS value
FROM rk {usql}
"""
        return sql, uparams
