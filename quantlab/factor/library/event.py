"""事件类因子（基于回补的特色数据域：龙虎榜 / 大宗交易 / 限售解禁）
=====================================
- block_premium_20   大宗折溢价：20 自然日大宗成交金额加权折溢价率（%），
  无大宗=0。折价成交多为股东/机构出货，负 IC 预期。
- dragon_net_20      龙虎榜强度：20 自然日上榜净买入合计 / 流通市值（%），
  未上榜=0。游资接力 vs 拉高出货，实测强正 IC（ICIR20 +2.3）。
- lockup_pressure_60 解禁压力：未来 60 自然日解禁股数 / 流通股数。
  解禁日期事前已知（无未来函数），解禁抛压负向预期。

技术要点：
1. 事件表是稀疏行（仅事件日有记录），20/60 日窗口必须用
   RANGE（时间跨度）而非 ROWS（行数）；非事件交易日经 LEFT JOIN 补 0。
2. 窗口方向：t 日值 = 过去 N 日事件之和 → 事件向【未来】扩散
   （k.date BETWEEN e_d AND e_d+N-1）。曾因方向写反（e_d-N~e_d）
   导致 t 日值包含未来事件，形成严重前视（IC 虚高至 2.3），已修复。
3. 前视控制：龙虎榜/大宗为盘后信息，因子值统一记账到「下一交易日」
   （SQL 内嵌 trade_calendar LEAD 映射），compute_all 每日重算无污染。
4. lockup 为事前公告的未来事件（t 日已知未来 60 日解禁），方向合法。
"""
from __future__ import annotations

from ..base import SqlFactor, universe_sql
from ..registry import register

# t 日盘后信息 → t+1 交易日记账（消除建仓前视；compute_all 幂等追加安全）
# 用法：作为 WITH 链的最后一个 CTE（前面加逗号拼接）
SHIFT_NEXT_D = """cal AS (
    SELECT trade_date AS d,
           LEAD(trade_date) OVER (ORDER BY trade_date) AS next_d
    FROM trade_calendar
)"""


@register
class BlockPremium20(SqlFactor):
    name = "block_premium_20"
    description = "20日大宗成交金额加权折溢价率%（无大宗=0；t+1记账无前视）"
    category = "event"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
        WITH ev AS (
            SELECT date, code, amount_wan,
                   COALESCE(premium_pct, 0) AS prem
            FROM block_trade
        ),
        span AS (
            SELECT date, code, amount_wan, prem,
                   CAST(date + INTERVAL 19 DAY AS DATE) AS e
            FROM ev
        ),
        hit AS (
            SELECT k.date, s.code,
                   SUM(s.amount_wan) AS amt,
                   SUM(s.amount_wan * s.prem) AS amt_prem
            FROM span s
            JOIN kline_daily k
              ON k.code = s.code AND k.date BETWEEN s.date AND s.e
            GROUP BY k.date, s.code
        ),
        raw AS (
            SELECT k.date, k.code,
                   COALESCE(h.amt_prem / NULLIF(h.amt, 0), 0) AS value
            FROM kline_daily k
            LEFT JOIN hit h ON h.date = k.date AND h.code = k.code
            WHERE k.close > 0 {usql}
        ),
        {SHIFT_NEXT_D}
        SELECT c.next_d AS date, r.code, r.value
        FROM raw r
        JOIN cal c ON c.d = r.date
        WHERE c.next_d IS NOT NULL
        """
        return sql, uparams


@register
class DragonNet20(SqlFactor):
    name = "dragon_net_20"
    description = "20日龙虎榜净买入合计/流通市值%（未上榜=0；t+1记账无前视）"
    category = "event"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
        WITH ev AS (
            -- 同股同日可能上多个榜（单日榜 + 3日累计榜，数据重复/口径不一）：
            -- 优先取单日榜净买入，无单日榜才用最强榜；消除 SUM 重复计账
            SELECT date, code,
                   MAX(CASE WHEN reason NOT LIKE '%连续三个交易日%'
                            THEN net_buy_wan END) AS net_single,
                   MAX(net_buy_wan) AS net_any
            FROM dragon_tiger
            GROUP BY date, code
        ),
        ev2 AS (
            SELECT date, code, COALESCE(net_single, net_any) AS net FROM ev
        ),
        span AS (
            SELECT date, code, net,
                   CAST(date + INTERVAL 19 DAY AS DATE) AS e
            FROM ev2
        ),
        hit AS (
            SELECT k.date, s.code, SUM(s.net) AS net20
            FROM span s
            JOIN kline_daily k
              ON k.code = s.code AND k.date BETWEEN s.date AND s.e
            GROUP BY k.date, s.code
        ),
        raw AS (
            SELECT k.date, k.code,
                   COALESCE(h.net20 / 1e4, 0)
                   / NULLIF(f.float_shares * k.close / 1e8, 0) AS value
            FROM kline_daily k
            ASOF JOIN share_capital_daily f
              ON f.code = k.code AND k.date >= f.date
            LEFT JOIN hit h ON h.date = k.date AND h.code = k.code
            WHERE k.close > 0 AND f.float_shares > 0 {usql}
        ),
        {SHIFT_NEXT_D}
        SELECT c.next_d AS date, r.code, r.value
        FROM raw r
        JOIN cal c ON c.d = r.date
        WHERE c.next_d IS NOT NULL
        """
        return sql, uparams


@register
class LockupPressure60(SqlFactor):
    name = "lockup_pressure_60"
    description = "未来60日解禁股数/流通股数（解禁抛压，前瞻已知无未来函数）"
    category = "event"

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = f"""
        WITH ev AS (
            SELECT code, date AS ldate, shares FROM lockup
        ),
        hit AS (
            SELECT k.date, e.code, SUM(e.shares) AS lk
            FROM ev e
            JOIN kline_daily k
              ON k.code = e.code
             AND k.date >= CAST(e.ldate - INTERVAL 60 DAY AS DATE)
             AND k.date < e.ldate
            GROUP BY k.date, e.code
        )
        SELECT k.date, k.code,
               COALESCE(h.lk, 0) * 1e8 / NULLIF(f.float_shares, 0) AS value
        FROM kline_daily k
        ASOF JOIN share_capital_daily f
          ON f.code = k.code AND k.date >= f.date
        LEFT JOIN hit h ON h.date = k.date AND h.code = k.code
        WHERE k.close > 0 AND f.float_shares > 0 {usql}
        """
        return sql, uparams
