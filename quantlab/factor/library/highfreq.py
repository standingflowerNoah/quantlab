"""高频因子库（minute_feat 宽表衍生，分钟数据 → 日频因子）
=====================================================
数据源：minute_feat（kline_1min 聚合的日级微观结构宽表，2025-01 起）。
构造范式（文献/研报共识）：**高频因子低频化**——日频原始特征噪声大，
滚动窗口平滑（20 日均值为主，聪明钱按原研报用 10 日）后方有稳定 IC
（方正"高频因子低频化"系列、开源金工交易行为四因子均采用此范式）。

类别与代表文献：
- 日内波动率矩：RV/BV 跳跃分解（Barndorff-Nielsen & Shephard 2004）；
  已实现偏度/峰度（Amaya et al. 2015；海通《高频量价因子》）
- 量分布：日内量分布 HHI/时段占比（海通；方正"暗流涌动""滴水穿石"）
- 日内动量：尾盘/早盘收益（Gao et al. 2018；上财"月频动量消失之谜"
  ——日内动量与隔夜动量相互抵消）
- 流动性：分钟 Amihud（Amihud 2002 高频版）、单位振幅成交额、
  收盘对 VWAP 偏离
- 量价关系/聪明钱：日内量价相关（方正《量价关系的高频乐章》——
  日内交易平稳者次月更易上涨）；聪明钱 Q（开源金工 2.0 版，
  S_t=|r_t|/vol_t^0.25，量累计占比前 20% 分钟）

窗口约定：ROWS 20 行（聪明钱 10 行），窗口内有效值 >= 15（/8），
不足置 NULL；宽表行 n_min >= 120（半日以上有效 bar）方参与。
"""
from __future__ import annotations

from ..base import SqlFactor, universe_sql
from ..registry import register

# 宽表派生列（CTE）：一次 JOIN kline_daily 补收盘（vwap_bias 用）
_HF_BASE = """
WITH f AS (
    SELECT m.date, m.code,
           LN(1e4 * m.rv)                                   AS rv_ln,
           m.rs_neg / NULLIF(m.rv, 0)                       AS dsem,
           GREATEST(m.rv - m.bv, 0) / NULLIF(m.rv, 0)       AS rjv,
           SQRT(m.n_min) * m.sum_r3
               / NULLIF(POWER(m.rv, 1.5), 0)                AS rsk,
           m.n_min * m.sum_r4
               / NULLIF(m.rv * m.rv, 0)                     AS rku,
           m.v_open30, m.v_close30, m.v_hhi,
           m.v_am / NULLIF(m.v_pm, 0)                       AS vampp,
           m.r_first30, m.r_last30, m.hi_pos,
           LN(1 + m.amihud_min)                             AS amihud_ln,
           LN(1 + m.amt_sum / NULLIF(m.day_range, 0))       AS amtrange_ln,
           k.close / NULLIF(m.vwap, 0) - 1.0                AS vwap_bias,
           m.corr_rv, m.smart_q, m.topv_r, m.topv_upr
    FROM minute_feat m
    JOIN kline_daily k ON k.date = m.date AND k.code = m.code
    WHERE m.n_min >= 120 AND m.rv > 0 {usql}
)
"""


class _HfFactor(SqlFactor):
    """高频因子基类：{col} 的 {w} 日均值（宽表列或派生列）"""

    category = "highfreq"
    _col: str = "rv_ln"
    _w: int = 20
    _min_valid: int | None = None

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        mv = self._min_valid or max(self._w - 5, 3)
        sql = _HF_BASE.format(usql=usql) + f"""
SELECT date, code,
       CASE WHEN COUNT({self._col}) OVER w >= {mv}
            THEN AVG({self._col}) OVER w END AS value
FROM f
WINDOW w AS (PARTITION BY code ORDER BY date
             ROWS BETWEEN {self._w - 1} PRECEDING AND CURRENT ROW)
QUALIFY value IS NOT NULL AND ISFINITE(value)
"""
        return sql, uparams


# ── 一、日内波动率族 ────────────────────────────────────────────────

@register
class HfRv(_HfFactor):
    name = "hf_rv_20"
    description = ("20日已实现波动率均值（分钟收益平方和；低波动异象，"
                   "预期负向）")
    _col = "rv_ln"


@register
class HfRvVol(_HfFactor):
    name = "hf_rvvol_20"
    description = ("20日波动率的波动率（RV 变异系数；模糊性厌恶，"
                   "预期负向——方正『云开雾散』）")
    _col = None  # 需复合表达式，覆盖 _expr 由下方实现

    def _sql(self, start=None, end=None, universe=None):
        usql, uparams = universe_sql(universe)
        mv = self._min_valid or 15
        sql = _HF_BASE.format(usql=usql) + f"""
SELECT date, code,
       CASE WHEN COUNT(rv_ln) OVER w >= {mv}
            THEN STDDEV_SAMP(rv_ln) OVER w
                 / NULLIF(AVG(rv_ln) OVER w, 0) END AS value
FROM f
WINDOW w AS (PARTITION BY code ORDER BY date
             ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
QUALIFY value IS NOT NULL AND ISFINITE(value)
"""
        return sql, uparams


@register
class HfRjv(_HfFactor):
    name = "hf_rjv_20"
    description = ("20日相对跳跃贡献均值 max(RV-BV,0)/RV（跳跃强度；"
                   "预期负向——方正『飞蛾扑火』）")
    _col = "rjv"


@register
class HfRsk(_HfFactor):
    name = "hf_rsk_20"
    description = ("20日已实现偏度均值（右偏=日内暴涨特征，彩票偏好"
                   "折价，预期负向——Amaya et al. 2015）")
    _col = "rsk"


@register
class HfRku(_HfFactor):
    name = "hf_rku_20"
    description = ("20日已实现峰度均值（日内极端收益频率；预期负向）"
                   "——海通《高频量价因子》")
    _col = "rku"


@register
class HfDsem(_HfFactor):
    name = "hf_dsem_20"
    description = "20日下行半方差占比均值（下行风险；方向待实证）"
    _col = "dsem"


# ── 二、成交量分布族 ────────────────────────────────────────────────

@register
class HfVopen(_HfFactor):
    name = "hf_vopen_20"
    description = ("20日开盘半小时量占比均值（开盘冲动交易占比；"
                   "散户主导度代理）")
    _col = "v_open30"


@register
class HfVclose(_HfFactor):
    name = "hf_vclose_20"
    description = ("20日尾盘半小时量占比均值（尾盘机构行为占比；"
                   "预期正向）")
    _col = "v_close30"


@register
class HfVhhi(_HfFactor):
    name = "hf_vhhi_20"
    description = ("20日分钟量集中度 HHI 均值（放量脉冲集中=少数知情"
                   "交易主导）")
    _col = "v_hhi"


@register
class HfVampp(_HfFactor):
    name = "hf_vampp_20"
    description = ("20日上午/下午量比均值（APM 简化版；日内时段行为"
                   "差异——开源金工 APM）")
    _col = "vampp"


# ── 三、日内动量族 ──────────────────────────────────────────────────

@register
class HfRlast30(_HfFactor):
    name = "hf_rlast30_20"
    description = ("20日尾盘半小时收益均值（尾盘动量；预期正向——"
                   "尾盘信息延续，Gao et al. 2018）")
    _col = "r_last30"


@register
class HfRfirst30(_HfFactor):
    name = "hf_rfirst30_20"
    description = ("20日开盘半小时收益均值（早盘冲动过度反应；"
                   "预期负向反转）")
    _col = "r_first30"


@register
class HfHipos(_HfFactor):
    name = "hf_hipos_20"
    description = ("20日日内高点时间位置均值（高点越临近收盘越强势；"
                   "预期正向）")
    _col = "hi_pos"


# ── 四、流动性族 ────────────────────────────────────────────────────

@register
class HfAmihud(_HfFactor):
    name = "hf_amihud_20"
    description = ("20日分钟 Amihud 均值（每亿元成交额价格冲击；"
                   "非流动性溢价，方向待实证）")
    _col = "amihud_ln"


@register
class HfAmtRange(_HfFactor):
    name = "hf_amtrange_20"
    description = ("20日单位振幅成交额均值（流动性厚度；方向待实证）")
    _col = "amtrange_ln"


@register
class HfVwapBias(_HfFactor):
    name = "hf_vwapbias_20"
    description = ("20日收盘对日内 VWAP 偏离均值（持续收于均价上方="
                   "过度反应，预期负向）")
    _col = "vwap_bias"


# ── 五、量价关系与聪明钱族 ──────────────────────────────────────────

@register
class HfCorrRv(_HfFactor):
    name = "hf_corr_rv_20"
    description = ("20日日内量价相关均值（量价纠缠度；日内交易平稳者"
                   "更易上涨，预期负向——方正《量价关系的高频乐章》）")
    _col = "corr_rv"


@register
class HfSmartQ(_HfFactor):
    name = "hf_smartq_10"
    description = ("10日聪明钱 Q 均值（聪明钱相对价位；Q 大=高位活跃"
                   "疑似出货，预期负向——开源金工）")
    _col = "smart_q"
    _w = 10
    _min_valid = 8


@register
class HfSmartQ20(_HfFactor):
    name = "hf_smartq_20"
    description = ("20日聪明钱 Q 均值（月度平滑版，对齐开源原研报口径；"
                   "Q 大=聪明钱在高位出货，预期负向）")
    _col = "smart_q"
    _w = 20
    _min_valid = 15


# ── 六、放量时刻族（待著而救）─────────────────────────────────────

@register
class HfTopvR(_HfFactor):
    name = "hf_topvr_20"
    description = ("20日放量分钟收益和均值（top-20% 量分钟的价格贡献；"
                   "放量上涨=主力资金流入，预期正向——开源金工『待著"
                   "而救』）")
    _col = "topv_r"


@register
class HfTopvUpr(_HfFactor):
    name = "hf_topvupr_20"
    description = ("20日放量分钟上涨量占比均值（放量分钟量价配合的方向"
                   "纯度，0.5 中性；预期正向）")
    _col = "topv_upr"
