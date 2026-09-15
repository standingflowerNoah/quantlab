"""筹码因子族（第二批新因子 · 2026-09-06 因子思路库 A4）
=====================================
华泰《人工智能105：基于筹码分层结构的端到端AI因子》（2026-06）的日频低配版。

数学核心（换手衰减的 telescoping 恒等式）：
  过去 N 日买入且至今未卖出的筹码占比
    = Σ_{s=t-N+1..t} to_s · Π_{k=s+1..t} (1−to_k)
    = 1 − Π_{k=t-N+1..t} (1−to_k)
    = 1 − R_t / R_{t−N}，其中 R_t = Π_{k≤t}(1−to_k)（累积存活率）
  R_t = exp(Σ ln(1−to)) —— 窗口累积和即可，值域 (0,1] 数值稳定，纯 SQL。

因子：
- chip_age_short_10  短龄筹码占比（过去10日买入且未卖出的比例；华泰消融
                     实验中筹码龄分层贡献最大——新近交易资金 vs 历史沉淀）
- chip_age_mid_60    中龄筹码占比（过去 60 日）
- chip_vwap_bias_250 现价相对换手衰减加权平均成本的乖离（pandas 递推：
                     cost_t = cost_{t−1}(1−to_t) + vwap_t·to_t，后复权；
                     递推天然数值稳定；PIT 由顺序构造保证）

换手率口径：vol / share_capital_daily.float_shares（逐日真值股本，ASOF 取
<= t 最近一条 = PIT 正确）。2026-09-11 前用 finance_snapshot 当前股本回填
历史，属 as-of 缺口，已随 PIT 股本表落地消除。
clip [0, 0.99] 防全换手/异常股本。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..base import Factor, SqlFactor, universe_sql
from ..registry import register
from ...config import get_logger

log = get_logger(__name__)

# 换手率 + 累积存活率 CTE
_SURV_BASE = """
WITH tr AS (
    SELECT k.date, k.code,
           LEAST(GREATEST(k.vol / f.float_shares, 0.0), 0.99) AS to_rate
    FROM kline_daily k
    ASOF JOIN share_capital_daily f
      ON f.code = k.code AND k.date >= f.date
    WHERE f.float_shares > 0 AND k.vol > 0 {usql}
),
r AS (
    SELECT date, code,
           EXP(SUM(LN(1.0 - to_rate)) OVER w) AS surv
    FROM tr
    WINDOW w AS (PARTITION BY code ORDER BY date)
)
"""


class _ChipAgeFactor(SqlFactor):
    """筹码龄占比基类：1 − R_t / R_{t−N}"""
    category = "chip"
    _lag_n: int = 10

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        sql = (_SURV_BASE.format(usql=usql)
               .replace("{lag_n}", str(self._lag_n)))
        return sql, uparams


# 2026-09-07 冗余清理：chip_age_short_10（ρ=0.91 vs turnover）、
# chip_age_mid_60（ρ=0.88，更弱）已按用户裁决删除
# （定义见 git 历史，compute_factor 可随时重建；_SURV_BASE 为
#  chip_vwap_bias_250 的共用水印，保留）


@register
class ChipVwapBias250(Factor):
    """现价相对换手衰减加权平均成本的乖离（pandas 宽表递推，数值稳定）

    cost_t = cost_{t-1}·(1−to_t) + vwap_t·to_t（后复权口径，跨除权可比）；
    (1−2%)^250 ≈ 0.6%，无限递推 ≈ 250 日自然截断，无前视（顺序构造）。
    """
    name = "chip_vwap_bias_250"
    description = ("筹码成本乖离：现价/换手衰减加权平均成本 − 1（250日有效记忆，"
                   "后复权递推；一字板日正常计入）")
    category = "chip"
    freq = "daily"

    def compute(self, store, start=None, end=None,
                universe: list[str] | None = None) -> pd.DataFrame:
        usql, params = universe_sql(universe)
        df = store.q(f"""
            SELECT k.date, k.code,
                   k.close * k.adj_factor AS close_adj,
                   (k.amount / k.vol) * k.adj_factor AS vwap_adj,
                   LEAST(GREATEST(k.vol / f.float_shares, 0.0), 0.99) AS to_rate
            FROM kline_daily k
            ASOF JOIN share_capital_daily f
              ON f.code = k.code AND k.date >= f.date
            WHERE f.float_shares > 0 AND k.vol > 0 AND k.amount > 0 {usql}
        """, params)
        if df.empty:
            return pd.DataFrame(columns=["date", "code", "value"])

        vwap = df.pivot(index="date", columns="code", values="vwap_adj").sort_index()
        close = df.pivot(index="date", columns="code", values="close_adj").sort_index()
        to = (df.pivot(index="date", columns="code", values="to_rate")
                .sort_index().fillna(0.0).clip(0.0, 0.99))

        arr_v, arr_t = vwap.values, to.values
        cost = np.full(vwap.shape, np.nan)
        c = np.full(vwap.shape[1], np.nan)
        for i in range(vwap.shape[0]):
            v, t = arr_v[i], arr_t[i]
            valid = np.isfinite(v)
            init = valid & ~np.isfinite(c)          # 首个有效日：成本=当日vwap
            c[init] = v[init]
            upd = valid & np.isfinite(c)             # 递推（停牌日成本保持）
            c[upd] = c[upd] * (1.0 - t[upd]) + v[upd] * t[upd]
            cost[i] = c

        bias = close.values / cost - 1.0
        out = (pd.DataFrame(bias, index=vwap.index, columns=vwap.columns)
               .stack().rename("value").reset_index())
        out.columns = ["date", "code", "value"]
        out["date"] = pd.to_datetime(out["date"])
        out["code"] = out["code"].astype(str).str.zfill(6)
        out["value"] = pd.to_numeric(out["value"], errors="coerce")
        out = out.replace([np.inf, -np.inf], np.nan).dropna(subset=["value"])
        out = out[np.isfinite(out["value"])]
        if start is not None:
            out = out[out["date"] >= pd.to_datetime(start)]
        if end is not None:
            out = out[out["date"] <= pd.to_datetime(end)]
        log.info(f"{self.name}: 递推完成 {len(out)} 行 "
                 f"({out['date'].min().date()} ~ {out['date'].max().date()})")
        return out.reset_index(drop=True)
