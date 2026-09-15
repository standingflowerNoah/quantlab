"""估值/规模/流动性因子
=====================================
- size / total_mcap / turnover：基于 kline_daily(收盘/成交量) × finance_snapshot(股本)
  计算历史序列（有完整时间深度，可做 IC 评估）
- 2026-09-07 移除 value_pe/value_pb（daily_snapshot 当前截面）：与库内 bp/ep
  同源冗余（value_pb≡1/bp、value_pe≈1/ep），历史估值敞口由 bp/ep/sp 多期版承担
- 2026-09-07 新增 ep_size_dom（C1 因子分域，第三批实验落地）
"""
from __future__ import annotations

import pandas as pd

from ..base import Factor, SqlFactor, universe_sql
from ..registry import register


class _KlineSharesFactor(SqlFactor):
    """kline ASOF JOIN share_capital_daily 的历史因子（PIT 逐日股本口径）

    2026-09-11 起改用 share_capital_daily（逐日真值股本），取代
    finance_snapshot（当前股本回填历史，as-of 缺口）。
    """
    category = "size"
    _expr: str = "1.0"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
        SELECT k.date, k.code, {self._expr} AS value
        FROM kline_daily k
        ASOF JOIN share_capital_daily f
          ON f.code = k.code AND k.date >= f.date
        WHERE f.float_shares > 0 {usql}
        """
        return sql, uparams


@register
class Size(_KlineSharesFactor):
    name = "size"
    description = "对数流通市值 ln(close×float_shares/1e8 亿元)"
    category = "size"
    economic_rationale = ("risk+behavioral：小市值溢价——流动性差/退市/信息不对称的"
                           "风险补偿；兼有机构持仓排除（中证1000外）与彩票偏好成分；"
                           "FDR 单因子 q=0.12 不过显著闸门，组合层 A/B 为其存活证据")
    _expr = "LN(k.close * f.float_shares / 1e8)"


@register
class TotalMcap(_KlineSharesFactor):
    name = "total_mcap"
    description = "对数总市值 ln(close×total_shares/1e8 亿元)"
    category = "size"
    _expr = "LN(k.close * f.total_shares / 1e8)"


@register
class Turnover(_KlineSharesFactor):
    name = "turnover"
    description = "日换手率 vol(手)×100/float_shares（流动性代理）"
    category = "volume"
    _expr = "k.vol * 100.0 / f.float_shares"


class _HistMcapValueFactor(SqlFactor):
    """国泰君安估值因子（多期历史版，2026-09-06 升级；2026-09-11 换 PIT 股本）

    分子 = finance_history 该日已披露最新报告期的财务科目（ASOF 前向填充，
    披露日记账无前视）；分母 = 当日收盘 × 当日总股本
    （share_capital_daily 逐日真值，ASOF 取 <= t 最近一条 = PIT 正确）。
    净利/营收/现金流为累计口径（Q2 即半年累计，未年化）。
    """
    category = "value"
    _fin_col: str = "net_profit"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
        WITH fh AS (
            SELECT code, report_period, notice_eff, {self._fin_col} AS fin
            FROM finance_history
            WHERE notice_eff IS NOT NULL AND {self._fin_col} IS NOT NULL
        ),
        g AS (
            SELECT code, notice_date, fin FROM (
                SELECT code, notice_eff AS notice_date, fin,
                       ROW_NUMBER() OVER (
                         PARTITION BY code, notice_eff
                         ORDER BY report_period DESC) AS rk
                FROM fh
            ) WHERE rk = 1
        )
        SELECT k.date, g.code,
               g.fin / NULLIF(k.close * fs.total_shares, 0) AS value
        FROM kline_daily k
        ASOF JOIN g
          ON g.code = k.code AND g.notice_date <= k.date
        ASOF JOIN share_capital_daily fs
          ON fs.code = k.code AND k.date >= fs.date
        WHERE g.fin IS NOT NULL AND k.close > 0 AND fs.total_shares > 0 {usql}
        """
        return sql, uparams


@register
class EP(_HistMcapValueFactor):
    name = "ep"
    description = ("盈利收益率 EP=净利润/总市值（累计口径未年化；"
                   "finance_history 多期版，披露日记账无前视）")
    _fin_col = "net_profit"


@register
class BP(_HistMcapValueFactor):
    name = "bp"
    description = "账面市值比 BP=净资产/总市值（≈1/PB，价值因子；多期版无前视）"
    _fin_col = "total_equity"


@register
class SP(_HistMcapValueFactor):
    name = "sp"
    description = "销售市值比 SP=营业收入/总市值（累计口径；多期版无前视）"
    _fin_col = "revenue"


@register
class OCFP(_HistMcapValueFactor):
    name = "ocfp"
    description = "经营现金流市值比 OCFP=经营现金流/总市值（累计口径；多期版无前视）"
    _fin_col = "operate_cf"


@register
class EpSizeDom(Factor):
    """EP 的市值分域版（C1 因子分域，光大量化选股系列思路，第三批实验落地）

    逻辑：估值因子（EP）在小市值域更稳健（研报口径：大市值域失效）。
    实现：每截面按 size 中位数二分域（小/大市值），域内 pct-rank EP。
    实验结果（scripts/batch3_domainsplit.py）：IC 0.020→0.030，
    ICIR 0.12→0.20，分年度 delta 五年全正（2022~2026）。
    """
    name = "ep_size_dom"
    description = ("EP 市值分域版：size 二分域内 pct-rank EP（分域 IC +53%，"
                   "五年增益全正；依赖 ep/size 因子先行落库）")
    category = "value"
    freq = "daily"

    def compute(self, store, start=None, end=None,
                universe: list[str] | None = None) -> pd.DataFrame:
        ep = store.read_factor("ep")
        size = store.read_factor("size")
        if ep.empty or size.empty:
            return pd.DataFrame(columns=["date", "code", "value"])
        m = ep.merge(size, on=["date", "code"], suffixes=("_ep", ""))
        m = m.dropna(subset=["value_ep", "value"])
        med = m.groupby("date")["value"].transform("median")
        dom = (m["value"] > med).astype(int)          # 0=小市值域 1=大市值域
        m["value"] = (m.groupby(["date", dom], observed=True)["value_ep"]
                      .rank(pct=True)).values
        out = m[["date", "code", "value"]].copy()
        out["date"] = pd.to_datetime(out["date"])
        out["code"] = out["code"].astype(str).str.zfill(6)
        if universe is not None:
            out = out[out["code"].isin(set(map(str, universe)))]
        if start is not None:
            out = out[out["date"] >= pd.to_datetime(start)]
        if end is not None:
            out = out[out["date"] <= pd.to_datetime(end)]
        return out.dropna(subset=["value"]).reset_index(drop=True)
