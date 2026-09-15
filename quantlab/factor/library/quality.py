"""质量因子（盈利质量与杠杆）
=====================================
- roe / op_margin / debt_ratio / ocf_ratio 等全部为 finance_history /
  westock finance_q 多期历史版：每日因子值 = 该日已披露最新报告期的比率，
  notice_eff / InfoPublDate 生效日记账，无前视（PIT）。
- 2026-09-06 二批迁移：roe/debt_ratio/ocf_ratio/current_ratio（finance_history）
- 2026-09-12 迁移：op_margin（finance_q income 阶梯，OperatingProfit/OperatingRevenue；
  原 finance_snapshot 单点截面废弃——快照当前值回填历史属 as-of 未来数据，
  audit 已升级为硬闸门，禁止快照表进入因子 SQL）

口径校准（迭代 26）：通达信 zhuyinglirun（主营利润）语义不可靠
（茅台 94.7亿、万科 6460亿，无法对应任何真实报表科目），
原 gross_margin（main_profit/revenue，全市场中位数 75%）已废弃，
改用经 5 家知名公司实证校验的营业利润率 op_margin（operating_profit/revenue，
茅台 67.7%/五粮液 42.2%/宁德 20.1%/海康 22.1% 均与公开数据吻合）。
"""
from __future__ import annotations

from ..base import SqlFactor, universe_sql
from ..registry import register
from .fundamental import _INCOME_LADDER


class _FinanceFactor(SqlFactor):
    """（已废弃）finance_snapshot 单点截面基类

    快照表只有「最新报告期」一行/股，任何引用都是当前值回填历史（as-of
    未来数据）。2026-09-12 起 audit 对 as-of 引用升级为硬闸门 FAIL，
    最后一个使用者 op_margin 已迁移 finance_q 多期阶梯，本基类仅留档。
    """
    category = "quality"
    _expr: str = "1.0"

    def _sql(self, start=None, end=None, universe=None):
        raise NotImplementedError(
            "finance_snapshot 单点截面因子已废弃（as-of 未来数据，audit 硬闸门）；"
            "请使用 finance_history / finance_q 多期版")


# ── 多期历史版质量因子（finance_history + ASOF 前向填充，2026-09-06 升级）──
# 旧版为单点截面（通达信最新报告期快照），升级为全历史时序：
# 每日因子值 = 该日已披露最新报告期的比率（notice_eff 生效日，无前视）。
# QUALIFY 去重：同股同日披露多期时取最新报告期（ASOF tie-break 确定）。
# 注意：roa/asset_turnover 用累计值未年化（Q2 即半年累计），横向可比性不受影响。
_HIST_BASE = """
WITH fh AS (
    SELECT code, report_period, notice_eff, net_profit, revenue, total_assets,
           total_liab, total_equity, operate_cf, current_ratio, inventory
    FROM finance_history
    WHERE notice_eff IS NOT NULL
),
g AS (
    SELECT code, notice_date, value FROM (
        SELECT code, notice_eff AS notice_date,
               {expr} AS value,
               ROW_NUMBER() OVER (
                 PARTITION BY code, notice_eff
                 ORDER BY report_period DESC) AS rk
        FROM fh
    ) WHERE rk = 1
)
SELECT k.date, g.code, g.value
FROM kline_daily k
ASOF JOIN g
  ON g.code = k.code AND g.notice_date <= k.date
WHERE g.value IS NOT NULL AND isfinite(g.value) {usql}
"""


class _HistoryFinanceFactor(SqlFactor):
    """finance_history 多期比率基类：ASOF 前向填充，notice_eff 生效日"""
    category = "quality"
    _expr: str = "NULL"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = _HIST_BASE.format(expr=self._expr, usql=usql)
        return sql, uparams


@register
class ROA(_HistoryFinanceFactor):
    name = "roa"
    description = ("总资产收益率 net_profit/total_assets（单期累计未年化；"
                   "finance_history 多期版，披露日记账无前视）")
    _expr = "net_profit / NULLIF(total_assets, 0)"


@register
class NetMarginHist(_HistoryFinanceFactor):
    name = "net_margin"
    description = ("净利率 net_profit/revenue（finance_history 多期版，"
                   "披露日记账无前视）")
    _expr = "net_profit / NULLIF(revenue, 0)"


@register
class AssetTurnoverHist(_HistoryFinanceFactor):
    name = "asset_turnover"
    description = ("资产周转率 revenue/total_assets（单期累计未年化；"
                   "finance_history 多期版，披露日记账无前视）")
    _expr = "revenue / NULLIF(total_assets, 0)"


# ── 2026-09-06 二批迁移：roe/debt_ratio/ocf_ratio/current_ratio 从
#    finance_snapshot 单点截面升级为 finance_history 多期版 ──
# 修复两个 audit WARN：① as-of 缺口（快照回填历史）② 截面覆盖仅 29 只
# （旧版 date=report_date 各股披露日分散，每披露日仅约 29 只同日披露）。

@register
class ROEHist(_HistoryFinanceFactor):
    name = "roe"
    description = ("净资产收益率 net_profit/total_equity（单期累计未年化；"
                   "finance_history 多期版，披露日记账无前视）")
    _expr = "net_profit / NULLIF(total_equity, 0)"


@register
class DebtRatioHist(_HistoryFinanceFactor):
    name = "debt_ratio"
    description = ("资产负债率 total_liab/total_assets（finance_history 多期版；"
                   "口径由流动+长期负债统一为总负债，与通行定义一致）")
    _expr = "total_liab / NULLIF(total_assets, 0)"


@register
class OcfRatioHist(_HistoryFinanceFactor):
    name = "ocf_ratio"
    description = ("经营现金流/净利润 operate_cf/net_profit（盈利含金量；"
                   "finance_history 多期版，披露日记账无前视）")
    _expr = "operate_cf / NULLIF(net_profit, 0)"


# ── 数据源受限科目（历史口径备忘）──
# finance_history 无 operating_profit / inventory 字段（业绩报表接口缺科目）；
# current_assets/current_liab 上游未灌数——quick_ratio/current_ratio 已于
# 2026-09-07 用东财 CURRENT_RATIO/INVENTORY 字段回溯修复；op_margin 已于
# 2026-09-12 迁 westock finance_q（income 表有 OperatingProfit）。
# 至此本模块因子 SQL 零引用 finance_snapshot（audit as-of 硬闸门口径）。

# ── 2026-09-07 三批迁移：current_ratio/quick_ratio 回溯修复 ──
# 根因：finance_history 旧拉取代码读 TOTAL_CURRENT_ASSETS/TOTAL_CURRENT_LIABILITIES
# ——东财 RPT_DMSK_FN_BALANCE 实际无此字段（57 字段实测），导致列全空。
# 真实字段：CURRENT_RATIO（流动比率，百分数口径）+ INVENTORY（存货绝对值）。
# 速动比率推导：quick = CA/CL − INV/CL = current_ratio − inventory/total_liab。
# 金融股（银行）无流动资产概念，CURRENT_RATIO=NULL → 因子自然排除，属预期。

@register
class CurrentRatioHist(_HistoryFinanceFactor):
    name = "current_ratio"
    description = ("流动比率（东财 CURRENT_RATIO 口径，已 ÷100 归一为比率；"
                   "finance_history 多期版，披露日记账无前视；金融股无此概念自然缺值）")
    category = "leverage"
    _expr = "current_ratio"


# ── 2026-09-12 迁移：op_margin 从 finance_snapshot 单点截面升级为 ──
# westock finance_q income 阶梯多期版（PIT 硬闸门口径）。
# 背景：finance_history 无营业利润科目（业绩报表接口缺），但 finance_q
# income 表有 OperatingProfit/OperatingRevenue（东财 F10 口径，茅台中报
# 67.71% 与公开一致，5558 只覆盖）。记账于 InfoPublDate 真实披露日，
# 更正披露压制/同日多期取最新报告期，复用 fundamental._INCOME_LADDER。

@register
class OpMarginHist(SqlFactor):
    name = "op_margin"
    description = ("营业利润率 OperatingProfit/OperatingRevenue（盈利能力，"
                   "finance_q 多期版，InfoPublDate 披露日记账无前视；"
                   "2026-09-12 迁移，原 finance_snapshot 单点截面废弃）")
    category = "quality"
    freq = "daily"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        expr = "OperatingProfit / NULLIF(OperatingRevenue, 0)"
        sql = f"""
{_INCOME_LADDER}
SELECT k.date, g.code, ({expr}) AS value
FROM kline_daily k
ASOF JOIN g ON g.code = k.code AND g.pub <= k.date
WHERE ({expr}) IS NOT NULL AND isfinite({expr}) {usql}
"""
        return sql, uparams


@register
class QuickRatioHist(_HistoryFinanceFactor):
    name = "quick_ratio"
    description = ("速动比率 = 流动比率 − 存货/总负债（=（流动资产−存货）/流动负债 恒等变形；"
                   "finance_history 多期版，披露日记账无前视）")
    category = "leverage"
    _expr = ("current_ratio - inventory / NULLIF(total_liab, 0)")
