"""资金流量因子 · 第一批（2026-09-14 立项，tushare moneyflow 湖表）
=================================================================================
数据源：data/lake/clean/moneyflow/part-{year}.parquet（tushare 标准 moneyflow
接口，全市场分单资金流，2022-01 起日频，单位万元）：
- super_net 超大单净流入（机构/大资金代理）
- large_net  大单净流入
- mid_net    中单净流入
- small_net  小单净流入（散户代理）
- main_net   主力净流入 = 超大单 + 大单
- net_mf_amount 全单净流入额（净主动买入）

单位换算：资金流字段为万元，kline_daily.amount 为元 → 占比 = 净额×1e4/成交额。

因果性：全部为窗口统计（≤t 日数据），t 收盘后可得（资金流为盘后口径，
同 kline_daily 的 LE 截断规则）。窗口内有效行 <15/20 置 NULL。

方向假设（事前登记，最终方向一律由 IC 实测定）：
- 主力持续净流入（机构建仓）→ 预期正 IC；A 股亦有"拉高出货"反例
- 小单持续净流入（散户接盘）→ 预期负 IC
- 智钱-散户分歧（超大+大-小单）→ 预期正 IC
"""
from __future__ import annotations

from ... import config
from ..base import SqlFactor, universe_sql
from ..registry import register

_MF = str(config.CLEAN_DIR / "moneyflow" / "part-*.parquet").replace("\\", "/")

# 公共基座：资金流 join 日K成交额（万元→元对齐后求占比）
# u = 主力净流入额(元)占比分子原料；s = 小单；n = 全单；d = 智钱-散户分歧
_MF_BASE = f"""
WITH mf AS (
    SELECT date, code, main_net, small_net, net_mf_amount,
           (super_net + large_net - small_net) AS sd_net
    FROM read_parquet('{_MF}', union_by_name=true)
),
k AS (
    SELECT date, code, amount
    FROM kline_daily
    WHERE amount > 0 {{usql}}
),
j AS (
    SELECT m.date, m.code, m.main_net, m.small_net, m.net_mf_amount,
           m.sd_net, k.amount
    FROM mf m JOIN k k ON m.date = k.date AND m.code = k.code
)
"""

_W20 = ("PARTITION BY code ORDER BY date "
        "ROWS BETWEEN 19 PRECEDING AND CURRENT ROW")
_W5 = ("PARTITION BY code ORDER BY date "
       "ROWS BETWEEN 4 PRECEDING AND CURRENT ROW")


class _MfRatioFactor(SqlFactor):
    """窗口净流入占比基类：col 字段 20 日净流入合计 / 20 日成交额合计"""
    category = "flow"
    freq = "daily"
    col: str = ""

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = (_MF_BASE + f"""
        SELECT date, code,
               CASE WHEN COUNT(amount) OVER w20 >= 15
                    THEN SUM({self.col}) OVER w20 * 10000.0
                         / SUM(amount) OVER w20 END AS value
        FROM j
        WINDOW w20 AS ({_W20})
        QUALIFY value IS NOT NULL
        """).format(usql=usql)
        return sql, uparams


@register
class MfMainPct20(_MfRatioFactor):
    name = "mf_main_pct_20"
    col = "main_net"
    description = ("20日主力净流入占成交额比：超大单+大单持续净流入方向，"
                   "机构资金代理（假设正向，IC 实测定向）")
    economic_rationale = "behavioral、data"


@register
class MfSmallPct20(_MfRatioFactor):
    name = "mf_small_pct_20"
    col = "small_net"
    description = ("20日小单净流入占成交额比：散户资金代理，"
                   "散户持续净买入=接盘信号（假设负向）")
    economic_rationale = "behavioral"


@register
class MfNetPct20(_MfRatioFactor):
    name = "mf_net_pct_20"
    col = "net_mf_amount"
    description = ("20日全单净流入占成交额比：净主动买入强度（假设正向）")
    economic_rationale = "behavioral"


@register
class MfSmartDumb20(_MfRatioFactor):
    name = "mf_smart_dumb_20"
    col = "sd_net"
    description = ("20日智钱-散户分歧度：(超大单+大单-小单)净流入占成交额比，"
                   "机构与散户方向相反时信号最强（假设正向）")
    economic_rationale = "behavioral、data"


@register
class MfMainChg20(SqlFactor):
    name = "mf_main_chg_20"
    description = ("主力净流入占比边际变化：5日占比 - 20日占比，"
                   "主力流入加速/减速（假设正向）")
    category = "flow"
    freq = "daily"
    economic_rationale = "behavioral"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = (_MF_BASE + f"""
        SELECT date, code,
               CASE WHEN COUNT(amount) OVER w20 >= 15
                    THEN SUM(main_net) OVER w5 * 10000.0
                         / NULLIF(SUM(amount) OVER w5, 0)
                       - SUM(main_net) OVER w20 * 10000.0
                         / NULLIF(SUM(amount) OVER w20, 0) END AS value
        FROM j
        WINDOW w5 AS ({_W5}), w20 AS ({_W20})
        QUALIFY value IS NOT NULL
        """).format(usql=usql)
        return sql, uparams
