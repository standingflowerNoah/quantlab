# -*- coding: utf-8 -*-
"""PandaAI 复现因子登记（WorldQuant 101 族 + cal 自定义族）
=========================================================
来源：pandaaiquant.com 因子中心 2026-09-13 全量拉取 → 筛选 → 复现入库
（panda_pull/replicate_factors.py，宽表实现逐位一致）。

闸门状态（2026-09-13，详见 research/pandaai_factor_replication_20260913.md）：
  - audit 全 PASS；FDR（BH+HLZ t_adj>3）全 PASS（044 rank 4/318 最强）
  - R4 族内去重：4 因子全独立（族内最大 |rho| 0.572），无吸收
  - R2 成本：040 73.5bp / 044 37.4bp / 5d_min_low 63.8bp 🟢；088 19.7bp 🟡 贴线
  - ⚠️ pa_5d_min_low_ratio 与 reversal_5 rho=0.709 越线 → 观察档（非正选）
  - 全部标注 2026-12 双闸门复核

R4/R2 数字快照：reports/_tmp/pa_r4_corr_matrix.csv、pa_r2_cost_summary.csv
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..alpha191 import _to_long, build_wide
from ..alpha191_ops import rank, ts_corr, ts_min, ts_mean, ts_rank, ts_std
from ..base import Factor
from ..registry import register


def decay_linear(x: pd.DataFrame, n: float) -> pd.DataFrame:
    """线性衰减加权 MA：权重 1..n 归一（最新值权重最大），向量化 shift 实现
    （与 panda_pull/replicate_factors.py 逐位一致）"""
    k = int(n)
    w = np.arange(1, k + 1, dtype=float)
    w = w / w.sum()
    out = None
    for i, wi in enumerate(w):
        s = x.shift(k - 1 - i) * wi
        out = s if out is None else out + s
    return out


def _wide(store, start=None, end=None) -> dict[str, pd.DataFrame]:
    w = build_wide(store, start or "2022-06-01")
    if end is not None:
        e = pd.Timestamp(end)
        w = {k: v[v.index <= e] for k, v in w.items()}
    return w


class _PaBase(Factor):
    """PandaAI 复现因子基类：compute 与复现脚本同实现"""
    category = "external"
    freq = "day"

    def _formula(self, W: dict[str, pd.DataFrame]) -> pd.DataFrame:
        raise NotImplementedError

    def compute(self, store, start=None, end=None,
                universe: list[str] | None = None) -> pd.DataFrame:
        res = self._formula(_wide(store, start, end))
        long = _to_long(res)
        if universe:
            long = long[long["code"].isin(universe)]
        return long


@register
class PaA101_040(_PaBase):
    name = "pa_a101_040"
    description = "WorldQuant 101 Alpha#40：(-rank(std(high,10)))×corr(high,volume,10)"
    economic_rationale = (
        "risk+behavioral：低波动且日内高价与量能同步的股票定价不足——波动拥挤的"
        "反向暴露叠加量价确认（WorldQuant 101 Alpha#40 原式，pandaaiquant 复现）。"
        "实证 IC20 +0.096/ICIR 0.66（2023-01~2026-09 ashare_ex，现场重算），"
        "四年 IC 全正（+0.087/+0.121/+0.081/+0.101）；FDR q≈0（t_adj 4.9，"
        "rank 43/318）；R4 族内独立（最大 |rho| 0.572），与 volatility_20 "
        "rho -0.49 同向非马甲；R2 盈亏平衡 73.5bp 🟢（top100 月换手 0.94 偏高，"
        "成本敏感）。2026-12 双闸门复核。"
    )

    def _formula(self, W):
        return (-1 * rank(ts_std(W["high"], 10))) * ts_corr(W["high"],
                                                            W["volume"], 10)


@register
class PaA101_044(_PaBase):
    name = "pa_a101_044"
    description = "WorldQuant 101 Alpha#44：-ts_corr(high, rank(volume), 5)"
    economic_rationale = (
        "behavioral：5 日新高伴随量能扩张=分歧追涨信号，后续回落"
        "（WorldQuant 101 Alpha#44 原式，pandaaiquant 复现）。实证 IC20 "
        "+0.057/ICIR 0.87（现场）——IC 量级中等但稳定性全场最强，FDR q≈0 "
        "（t_adj 6.5，rank 4/318 批次第一）；R4 独立（与国君 191 同编号版 "
        "alpha044 rho 仅 0.02=同编号不同式实锤）；R2 盈亏平衡 37.4bp 🟢。"
        "2026-12 双闸门复核。"
    )

    def _formula(self, W):
        return -1 * ts_corr(W["high"], rank(W["volume"]), 5)


@register
class PaA101_088(_PaBase):
    name = "pa_a101_088"
    description = ("WorldQuant 101 Alpha#88：min(rank(decay((rank(open)+rank(low))"
                   "-(rank(high)+rank(close)),8)), ts_rank(decay(corr(ts_rank"
                   "(close,8),ts_rank(adv60,21),8),7),3))，窗口取整")
    economic_rationale = (
        "risk+behavioral：日内位置错位（开盘/收盘价排名结构）线性衰减与量能趋势"
        "确认取小——位置反转叠加量能确认的复合结构（WorldQuant 101 Alpha#88 原式，"
        "小数窗口取整，pandaaiquant 复现）。实证 IC20 +0.076/ICIR 0.63（现场），"
        "四年 IC 全正；FDR q≈0（t_adj 5.0，rank 38/318）；R4 独立；"
        "⚠️ R2 盈亏平衡 19.7bp 🟡 贴线（top100 月换手 0.84）——成本敏感、"
        "对费率假设最脆弱的一个，2026-12 双闸门复核。"
    )

    def _formula(self, W):
        adv60 = ts_mean(W["volume"], 60)
        t1 = rank(decay_linear((rank(W["open"]) + rank(W["low"]))
                               - (rank(W["high"]) + rank(W["close"])), 8))
        t2 = ts_rank(decay_linear(ts_corr(ts_rank(W["close"], 8),
                                          ts_rank(adv60, 21), 8), 7), 3)
        return t1.where(t2.isna() | (t1 <= t2), t2)


@register
class Pa5dMinLowRatio(_PaBase):
    name = "pa_5d_min_low_ratio"
    description = "PandaAI cal_5d_min_low_ratio：ts_min(low,5)/close（近5日低点距现价）"
    economic_rationale = (
        "behavioral：近 5 日最低价距现价越远=短期深跌超卖，反转机制的低位变形"
        "（PandaAI cal_5d_min_low_ratio 原式）。实证 IC20 +0.065/ICIR 0.47"
        "（现场），FDR PASS（t_adj 3.7，rank 97/318）；R4 族内独立；"
        "⚠️ 与 reversal_5 rho=0.709 越线（R4 判定=观察档非正选）——"
        "增量存疑，若 2026-12 双闸门前组合层无法证明对 reversal_5 的边际贡献"
        "则移除。"
    )

    def _formula(self, W):
        return ts_min(W["low"], 5) / W["close"]
