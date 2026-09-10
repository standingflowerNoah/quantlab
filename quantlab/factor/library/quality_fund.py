"""基本面质量因子 · 第三批补充（研报因子挖掘计划 Phase 3，2026-09-10）
=====================================
- accruals 应计质量：(净利润 − 经营现金流)/总资产。应计越高，利润含金量
  越低（Sloan 1996 应计异象；中银财报因子框架）。负向因子预期。
  注意：库内 ocf_ratio=operate_cf/net_profit 与本因子同源不同比，
  dedup 时观察（分母不同，预期 |ρ|<0.9）。
- goodwill_ratio 商誉占净资产比例（广发 PB-ROE 三重过滤之一）：
  东财商誉专题 RPT_GOODWILL_STOCKDETAILS，实测含多期历史（2019 起每期
  ~2600 家有商誉公司，无商誉公司不在表内 → ratio 缺失≈无商誉），
  PIT 记账用 NOTICE_DATE，2022 起有真实历史，可正常回测。
  初始拉取见 scripts/finance_goodwill_backfill.py（goodwill_snapshot 表）。

计划内另三项的裁决（不新增因子）：
- equity_multiplier ≡ 1/(1−debt_ratio)，与库内 debt_ratio 数学等价 → 不重复
- ocf_quality ≡ 库内 ocf_ratio → 不重复
- rev_growth_pos → 组合过滤层直接用库内 revenue_growth_yoy>0，不单独成因子
"""
from __future__ import annotations

from ..base import SqlFactor, universe_sql
from ..registry import register

# ── accruals：与 growth.py 同款 ASOF PIT 模式 ─────────────────────────
_ACCRUALS = """
WITH fh AS (
    SELECT code, report_period, notice_eff, net_profit, operate_cf, total_assets
    FROM finance_history
    WHERE notice_eff IS NOT NULL
),
g AS (
    SELECT code, notice_date, value FROM (
        SELECT code, notice_eff AS notice_date,
               CASE WHEN total_assets > 0
                    THEN (COALESCE(net_profit, 0) - COALESCE(operate_cf, 0))
                         / total_assets END AS value,
               ROW_NUMBER() OVER (
                 PARTITION BY code, notice_eff
                 ORDER BY report_period DESC) AS rk
        FROM fh
        WHERE report_period >= DATE '2022-01-01'
    ) WHERE rk = 1 AND value IS NOT NULL
)
SELECT k.date, g.code, g.value
FROM kline_daily k
ASOF JOIN g
  ON g.code = k.code AND g.notice_date <= k.date
WHERE g.value IS NOT NULL AND isfinite(g.value) {usql}
"""


class Accruals(SqlFactor):
    name = "accruals"
    description = ("应计质量（净利润−经营现金流)/总资产， Sloan 应计异象；"
                   "应计越高利润含金量越低，研报负向")
    category = "quality"
    freq = "daily"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        return _ACCRUALS.format(usql=usql), uparams


register(Accruals)


# ── goodwill_ratio：快照表 ASOF（无历史，前向跟踪）──────────────────────
_GOODWILL = """
WITH g AS (
    SELECT code, notice_date, ratio AS value FROM goodwill_snapshot
    WHERE notice_date IS NOT NULL AND ratio IS NOT NULL
)
SELECT k.date, g.code, g.value
FROM kline_daily k
ASOF JOIN g
  ON g.code = k.code AND g.notice_date <= k.date
WHERE g.value IS NOT NULL AND isfinite(g.value) {usql}
"""


class GoodwillRatio(SqlFactor):
    name = "goodwill_ratio"
    description = ("商誉/净资产（广发 PB-ROE 过滤之一；东财商誉专题多期，"
                   "无商誉公司不在表→缺失；PIT 披露日记账）")
    category = "quality"
    freq = "daily"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        return _GOODWILL.format(usql=usql), uparams


register(GoodwillRatio)
