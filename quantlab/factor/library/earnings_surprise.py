"""盈余惊喜与盈利加速（第一批新因子 · 2026-09-06 因子思路库 A1/A2）
=====================================
基于 finance_history（东财多期财报，2021Q1 起 22 期）：

- sue  标准化预期外盈利（SUE, Foster-Olsen-Shevlin 1984；中信/申万口径）
       E[Q_t] = Q_{t-4} + delta_t，delta_t = 过去8个季差分的均值（至少6个）
       SUE_t = (Q_t - E[Q_t]) / sigma_t，sigma_t = 过去8期预测误差的标准差
       （至少6个），clip ±10 防小 sigma 爆炸。
       对应 PEAD（盈余公告后漂移）：公告后沿预期差方向漂移。
- earnings_accel  盈利加速（海通选股因子系列83：盈利加速的定量刻画）
       = 当期净利同比 - 上期净利同比（相邻季度差分；两期均要求基数>0），
       刻画盈利动能拐点，比静态同比更领先。
- sue_pred  预告 SUE（第五批 SUE_I 改进 · 申万真实超预期系列）：业绩预告
       净利区间中值代入 sue 同一误差框架（同一 delta/sigma），比财报早
       1-3 个月给出惊喜信号。PIT：参数取预告披露时点已可见的最新财报期。
- sue_i    SUE_I 融合版（申万口径）：财报 + 预告事件流合并，过去 90 日
       事件信号按时间线性加权（衰减窗口外归零——旧信号不再满仓使用，
       正对 SUE 2026 拥挤衰减的时效改进）。

记账规则（无前视，与 growth.py 同模式）：
- 每期值在 notice_eff（法定披露截止保守化的生效日）后才可见 →
  kline 日期 ASOF JOIN notice_eff <= t 的最近一期，阶梯填充
- 同股同日披露多期（年报+一季报同日）→ QUALIFY 取最新报告期（防
  ASOF tie-break 不确定导致 PIT 误报）
- 季度序号 qs = year*4+quarter，RANGE 窗口按季度距离（缺期不跨期）

数据窗口：2021Q1 起的财报 → 差分 2022Q1 起 → SUE 有效值约 2024 年披露起；
预告源 perf_forecast 2020Q1 起 → sue_pred/sue_i 有效约 2023 年起。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..base import Factor, SqlFactor, universe_sql
from ..registry import register
from ...config import get_logger

log = get_logger(__name__)

# 公共 CTE：季度序号 + 净利 4 期滞后（同比与季差分的原料）
_Q_BASE = """
    SELECT code, report_period, notice_eff,
           year(report_period) * 4 + quarter(report_period) AS qs,
           net_profit,
           LAG(net_profit, 4) OVER (
               PARTITION BY code ORDER BY report_period) AS np_lag4
    FROM finance_history
    WHERE notice_eff IS NOT NULL
"""

# ASOF 前向填充 + 同日多期去重（与 growth.py 相同模式）
_ASOF_TAIL = """
SELECT k.date, g.code, g.value
FROM kline_daily k
ASOF JOIN g
  ON g.code = k.code AND g.notice_date <= k.date
WHERE g.value IS NOT NULL AND isfinite(g.value) {usql}
"""


class _SueBase(SqlFactor):
    """盈余惊喜基类：finance_history 滚动窗口 + ASOF 前向填充"""
    category = "surprise"
    _min_win: int = 6          # delta/sigma 最少观测数（参数敏感性测试可调）

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        # asof_tail 内嵌的 {usql} 不能被 str.format 二次处理，单独 replace
        sql = (self._body()
               .format(min_win=self._min_win, q_base=_Q_BASE,
                       asof_tail=_ASOF_TAIL)
               .replace("{usql}", usql))
        return sql, uparams

    def _body(self) -> str:
        raise NotImplementedError


@register
class SueFactor(_SueBase):
    name = "sue"
    description = ("标准化预期外盈利 SUE：(净利季差分 − 过去8季差分均值) / "
                   "过去8期误差标准差；记账日=MAX(当期, 窗口内上期 notice_eff)（PIT："
                   "保证窗口成分全部已披露），阶梯填充")
    freq = "daily"

    def _body(self) -> str:
        return """
WITH q AS (
{q_base}
),
d AS (                                   -- 季差分 dq = Q(t) − Q(t−4)
    SELECT code, report_period, notice_eff, qs,
           net_profit - np_lag4 AS dq
    FROM q
    WHERE np_lag4 IS NOT NULL
),
d2 AS (                                  -- δ_t = 过去8季差分均值（不含当期）
    SELECT code, report_period, notice_eff, qs, dq,
           AVG(dq) OVER w8 AS delta,
           COUNT(dq) OVER w8 AS delta_n,
           MAX(notice_eff) OVER w8 AS pub_max  -- 泄露修复: δ/σ 窗口成分的最晚披露日
    FROM d
    WINDOW w8 AS (PARTITION BY code ORDER BY qs
                  RANGE BETWEEN 8 PRECEDING AND 1 PRECEDING)
),
e AS (                                   -- 误差 ε_t = ΔQ_t − δ_t
    SELECT code, report_period, notice_eff, qs, pub_max,
           dq - delta AS err
    FROM d2
    WHERE delta IS NOT NULL AND delta_n >= {min_win}
),
e2 AS (                                  -- σ_t = 过去8期误差标准差（不含当期）
    SELECT code, report_period, notice_eff, pub_max,
           err,
           STDDEV_SAMP(err) OVER w8 AS sigma,
           COUNT(err) OVER w8 AS sigma_n
    FROM e
    WINDOW w8 AS (PARTITION BY code ORDER BY qs
                  RANGE BETWEEN 8 PRECEDING AND 1 PRECEDING)
),
sue AS (                                 -- SUE = ε / σ，clip ±10
    SELECT code, report_period, notice_eff, pub_max,
           LEAST(GREATEST(err / sigma, -10), 10) AS value
    FROM e2
    WHERE sigma IS NOT NULL AND sigma > 1e-12 AND sigma_n >= {min_win}
),
g AS (
    SELECT code, notice_date, value FROM (
        SELECT code, GREATEST(notice_eff, pub_max) AS notice_date, value,
               ROW_NUMBER() OVER (
                 PARTITION BY code, GREATEST(notice_eff, pub_max)
                 ORDER BY report_period DESC) AS rk
        FROM sue
    ) WHERE rk = 1
)
{asof_tail}
"""


@register
class EarningsAcceleration(_SueBase):
    name = "earnings_accel"
    description = ("盈利加速：当期净利同比 − 上期净利同比（相邻季度差分，两期基数>0）；"
                   "记账日=MAX(当期, 上期 notice_eff)（PIT：上期同比依赖上期净利，"
                   "须待其披露），阶梯填充")
    freq = "daily"

    def _body(self) -> str:
        return """
WITH q AS (
{q_base}
),
y AS (                                   -- 自算同比，剔除去年同期 ≤0（符号翻转无定义）
    SELECT code, report_period, notice_eff, qs,
           CASE WHEN np_lag4 > 0
                THEN LEAST(GREATEST(net_profit / np_lag4 - 1.0, -10), 10)
           END AS np_yoy                 -- 同比 clip ±10（±1000%）：防微利基数爆炸
    FROM q
    WHERE net_profit IS NOT NULL
),
a AS (
    SELECT code, report_period, notice_eff, np_yoy,
           np_yoy - LAG(np_yoy) OVER (PARTITION BY code ORDER BY qs) AS accel_raw,
           qs - LAG(qs) OVER (PARTITION BY code ORDER BY qs) AS qs_gap,
           LAG(np_yoy) OVER (PARTITION BY code ORDER BY qs) AS yoy_prev,
           LAG(notice_eff) OVER (PARTITION BY code ORDER BY qs) AS notice_prev
    FROM y
),
acc AS (                                 -- 仅相邻季度差分有效（防缺期跨期比较）
    SELECT code, report_period, notice_eff,
           CASE WHEN qs_gap = 1 AND yoy_prev IS NOT NULL
                THEN LEAST(GREATEST(accel_raw, -5), 5)   -- 加速度 clip ±5
           END AS value,
           CASE WHEN qs_gap = 1 AND yoy_prev IS NOT NULL
                THEN GREATEST(notice_eff, notice_prev)
           END AS pub_eff                 -- 泄露修复: 上期同比依赖上期净利披露, 记账取两期较晚
    FROM a
),
g AS (
    SELECT code, notice_date, value FROM (
        SELECT code, pub_eff AS notice_date, value,
               ROW_NUMBER() OVER (
                 PARTITION BY code, pub_eff
                 ORDER BY report_period DESC) AS rk
        FROM acc
    ) WHERE rk = 1 AND value IS NOT NULL
)
{asof_tail}
"""


# ── SUE_I 改进（第五批 · 申万真实超预期系列）──────────────────



@register
class SuePred(_SueBase):
    """预告 SUE（第五批 SUE_I 改进）

    预告净利区间中值 est 视为 Q_t 的早期估计：
    SUE_hat = (est − Q_{t−4} − delta) / sigma
    其中 delta/sigma 取预告披露时点已可见的最新财报期的预测参数
    （ASOF notice_eff <= notice_date，严格 PIT）；Q_{t−4} 同样要求
    去年同期财报在预告前已披露。clip ±10 与 sue 一致。
    """
    name = "sue_pred"
    description = ("预告 SUE：业绩预告净利中值代入 sue 误差框架（同 δ/σ），"
                   "比财报早 1-3 个月；预告披露日记账")
    freq = "daily"

    def _body(self) -> str:
        return """
WITH q AS (
{q_base}
),
d AS (
    SELECT code, report_period, notice_eff, qs,
           net_profit - np_lag4 AS dq
    FROM q
    WHERE np_lag4 IS NOT NULL
),
d2 AS (
    SELECT code, report_period, notice_eff, qs, dq,
           AVG(dq) OVER w8 AS delta,
           COUNT(dq) OVER w8 AS delta_n
    FROM d
    WINDOW w8 AS (PARTITION BY code ORDER BY qs
                  RANGE BETWEEN 8 PRECEDING AND 1 PRECEDING)
),
e AS (
    SELECT code, report_period, notice_eff, qs,
           delta,
           dq - delta AS err
    FROM d2
    WHERE delta IS NOT NULL AND delta_n >= {min_win}
),
e2 AS (
    SELECT code, report_period, notice_eff,
           delta,
           err,
           STDDEV_SAMP(err) OVER w8 AS sigma,
           COUNT(err) OVER w8 AS sigma_n
    FROM e
    WINDOW w8 AS (PARTITION BY code ORDER BY qs
                  RANGE BETWEEN 8 PRECEDING AND 1 PRECEDING)
),
param AS (        -- 每次财报披露时点可见的"下一期预测参数"
    SELECT code, eff_date, delta, sigma FROM (
        SELECT code, notice_eff AS eff_date, delta, sigma,
               ROW_NUMBER() OVER (
                 PARTITION BY code, notice_eff
                 ORDER BY report_period DESC) AS rk
        FROM e2
        WHERE delta IS NOT NULL AND sigma IS NOT NULL AND sigma_n >= {min_win}
    ) WHERE rk = 1
),
pred AS (
    SELECT code, report_date, notice_date,
           CASE WHEN amt_lower IS NOT NULL AND amt_upper IS NOT NULL
                THEN (amt_lower + amt_upper) / 2.0
                WHEN amt_lower IS NOT NULL THEN amt_lower
                WHEN amt_upper IS NOT NULL THEN amt_upper
           END AS est
    FROM perf_forecast
    WHERE notice_date IS NOT NULL
),
base AS (
    SELECT p.code, p.notice_date, p.est,
           year(p.report_date) * 4 + quarter(p.report_date) AS qs,
           fh.net_profit AS np_lag4
    FROM pred p
    LEFT JOIN finance_history fh
      ON fh.code = p.code
     AND fh.report_period = p.report_date - INTERVAL 1 YEAR
     AND fh.notice_eff <= p.notice_date      -- 基数须在预告前已披露
),
ph AS (
    SELECT b.code, b.notice_date, b.qs,
           LEAST(GREATEST((b.est - b.np_lag4 - prm.delta) / prm.sigma,
                          -10), 10) AS value
    FROM base b
    ASOF JOIN param prm
      ON prm.code = b.code AND prm.eff_date <= b.notice_date
    WHERE b.np_lag4 IS NOT NULL AND prm.delta IS NOT NULL
      AND prm.sigma > 1e-12 AND isfinite(b.est)
),
g AS (
    SELECT code, notice_date, value FROM (
        SELECT code, notice_date, value,
               ROW_NUMBER() OVER (
                 PARTITION BY code, notice_date
                 ORDER BY qs DESC) AS rk
        FROM ph
    ) WHERE rk = 1 AND value IS NOT NULL
)
{asof_tail}
"""


@register
class SueI(Factor):
    """SUE_I 融合版（申万：多源事件流 + 时间线性加权）

    事件流 = 财报 SUE 事件 ∪ 预告 SUE 事件（同 δ/σ 框架，完全同纲）。
    t 日值 = 过去 90 日事件的线性加权平均：w = 90 − (t − event_date)，
    窗口外无信号 → NULL（旧信号衰减归零，正对 sue 的阶梯固化问题）。
    事件数少（每股年均 ~5 件），逐股 searchsorted 向量化，秒级。
    """
    name = "sue_i"
    description = ("SUE_I：财报+预告事件流 90 日时间线性加权（申万口径），"
                   "旧信号衰减；依赖 sue/sue_pred 事件层与 perf_forecast")
    category = "surprise"
    freq = "daily"
    economic_rationale = ("behavioral：盈余公告后漂移 PEAD——投资者对财报/预告"
                          "信息反应不足（Ball-Brown 1968； Bernard-Thomas 1989）")

    _window = 90  # 线性衰减窗口（交易日近似日历日，衰减因子单调即可）

    @staticmethod
    def _events_from_body(factor: _SueBase) -> pd.DataFrame:
        """从 SueFactor/SuePred 的 _body 派生事件层（asof_tail 换成事件输出，
        单一 SQL 来源，避免手写第二遍）"""
        sql = (factor._body()
               .format(min_win=factor._min_win, q_base=_Q_BASE, asof_tail="")
               .replace("{usql}", "")) + """
SELECT code, CAST(notice_date AS DATE) AS event_date, value FROM g
"""
        from ...data.store import Store
        return Store().q(sql)

    def compute(self, store, start=None, end=None,
                universe: list[str] | None = None) -> pd.DataFrame:
        ev_fin = self._events_from_body(SueFactor())
        ev_pred = self._events_from_body(SuePred())
        ev = pd.concat([ev_fin, ev_pred], ignore_index=True)
        ev["event_date"] = pd.to_datetime(ev["event_date"])
        ev = ev.dropna(subset=["value"]).groupby(
            ["code", "event_date"], as_index=False)["value"].mean()

        cal = store.q("SELECT DISTINCT CAST(date AS DATE) AS date "
                      "FROM kline_daily ORDER BY date")
        cal["date"] = pd.to_datetime(cal["date"])
        cal_days = cal["date"].to_numpy(dtype="datetime64[ns]")

        if universe is not None:
            ev = ev[ev["code"].isin(set(map(str, universe)))]

        w = int(self._window)
        frames = []
        for code, g in ev.groupby("code", sort=False):
            ed = g["event_date"].to_numpy(dtype="datetime64[ns]")
            s = g["value"].to_numpy(dtype=float)
            lo_idx = np.searchsorted(ed,
                                     cal_days - np.timedelta64(w - 1, "D"),
                                     side="left")
            hi_idx = np.searchsorted(ed, cal_days, side="right")
            valid = hi_idx > lo_idx
            if not valid.any():
                continue
            dates = cal_days[valid]
            li, ri = lo_idx[valid], hi_idx[valid]
            # 事件窗口不等长 → 逐日行内小数组求和（每股事件数少，快）
            vals = np.empty(len(dates))
            for i in range(len(dates)):
                l, r = int(li[i]), int(ri[i])
                ag = (dates[i] - ed[l:r]).astype("timedelta64[D]").astype(float)
                wv = w - ag
                vals[i] = float((wv * s[l:r]).sum() / wv.sum())
            frames.append(pd.DataFrame({
                "date": dates, "code": code, "value": vals}))

        if not frames:
            return pd.DataFrame(columns=["date", "code", "value"])
        out = pd.concat(frames, ignore_index=True)
        if universe is not None:
            out = out[out["code"].isin(set(map(str, universe)))]
        if start is not None:
            out = out[out["date"] >= pd.to_datetime(start)]
        if end is not None:
            out = out[out["date"] <= pd.to_datetime(end)]
        log.info(f"{self.name}: 事件 {len(ev)} 条 → 日频 {len(out)} 行")
        return out.reset_index(drop=True)
