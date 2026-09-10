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
