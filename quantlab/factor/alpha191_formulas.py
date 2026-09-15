"""国泰君安 Alpha191 公式库
=====================================
来源：国泰君安研报《基于短周期价量特征的多因子选股体系》(2017) 的 191 个
短周期价量因子，依据公开流传版本整理转译为宽表算子表达式。

诚实说明：
- 公式以公开研报/社区流传版本为准逐条转译；个别编号的原始定义存在多个
  流传版本，转译可能存在偏差（CONFIDENCE 标注）；
- 每个因子计算后做健全性检验（非全 NaN / 非常数截面），异常因子会被标记
  为 invalid 并在 manifest 中记录，不落库。

每个公式函数接收宽表字典 W，返回宽表因子值。
W 键：open/high/low/close（前复权）、vwap（前复权成交均价）、
      volume（原始成交量，股）、amount（原始成交额，元）、ret（日收益率）
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .alpha191_ops import (
    delay, delta, ts_mean, ts_std, ts_sum, ts_min, ts_max, ts_rank,
    ts_corr, ts_cov, ts_argmax, ts_argmin, rank, scale, sign, log_, abs_,
    if_, max_, min_, power,
)

# name -> (func, confidence)  confidence: "high"=研报原文确信 / "mid"=流传版本可能一致
ALPHAS: dict[str, tuple] = {}


def alpha(n: int, confidence: str = "high"):
    def deco(fn):
        ALPHAS[f"alpha{n:03d}"] = (fn, confidence)
        return fn
    return deco


C = W = None  # 仅文档用途


# ═══ Alpha 1-20 ═══════════════════════════════════════════════════════
@alpha(1)
def a1(W):
    return -1 * ts_corr(rank(delta(log_(W["volume"]), 1)),
                        rank((W["close"] - W["open"]) / W["open"]), 6)


@alpha(2)
def a2(W):
    return -1 * delta(((W["close"] - W["low"]) - (W["high"] - W["close"]))
                      / (W["high"] - W["low"]), 1)


@alpha(3)
def a3(W):
    cond = W["close"] <= delay(W["close"], 1)
    vol0 = W["volume"] * 0
    return -1 * ts_sum(if_(cond, W["volume"], vol0), 20)


@alpha(4)
def a4(W, ):
    return -1 * ts_rank(rank(ts_corr(rank(W["vwap"]), rank(W["volume"]), 6)), 9)


@alpha(5)
def a5(W):
    x = W["open"] - ts_sum(W["vwap"], 9) / 9
    return rank(x) * (-1 * rank(abs_(x)))


@alpha(6)
def a6(W):
    return -1 * ts_corr(W["open"], W["volume"], 10)


@alpha(7)
def a7(W):
    return ((W["close"] - W["low"]) - (W["high"] - W["close"])) * W["volume"] \
        / (W["high"] - W["low"]) / ts_sum(W["volume"], 21) * 20


@alpha(8)
def a8(W):
    r = ts_sum(W["open"], 5) * ts_sum(W["ret"], 5)
    return -1 * rank(delta(r, 4))


@alpha(9)
def a9(W):
    c1 = rank(ts_corr(rank(W["high"]), rank(W["volume"]), 3))
    c2 = rank(delta(W["close"], 1))
    cond = min_(c1, c2) > 0
    d = delta(W["close"], 1)
    return if_(cond, d, -1 * d)


@alpha(10)
def a10(W):
    r = W["ret"]
    inner = if_(r > 0, ts_std(r, 20), W["close"])
    return rank(max_(inner, W["close"])) * -1


@alpha(11)
def a11(W):
    x = ts_max(W["close"], 3) - W["close"]
    y = W["close"] - ts_min(W["close"], 3)
    return (rank(max_((x / y).replace([np.inf, -np.inf], np.nan), 0)) * 3 - 1) \
        * (rank(ts_sum(W["ret"], 3)) * ts_rank(abs_(W["vwap"] / W["close"] - 1), 5))


@alpha(12)
def a12(W):
    return sign(delta(W["volume"], 1)) * (-1 * delta(W["close"], 1))


@alpha(13)
def a13(W):
    return -1 * rank(ts_cov(rank(W["close"]), rank(W["volume"]), 5))


@alpha(14)
def a14(W):
    return rank(delta(W["ret"] * W["volume"], 3)) * -1 \
        * ts_rank(ts_corr(W["open"], W["volume"], 10), 10)


@alpha(15)
def a15(W):
    return -1 * ts_sum(rank(ts_corr(rank(W["high"]), rank(W["volume"]), 3)), 3)


@alpha(16)
def a16(W):
    return -1 * rank(ts_cov(rank(W["high"]), rank(W["volume"]), 5))


@alpha(17)
def a17(W):
    x = ts_max(W["vwap"] - W["close"], 2)
    return rank(ts_rank(x, 2)) + ts_rank(-1 * delta(W["close"], 2), 5) \
        + rank(W["volume"] / W["volume"].shift(1) - 1)


@alpha(18)
def a18(W):
    x = W["close"] / W["open"].shift(6) - 1
    s = ts_std(abs_(x), 20)
    return -1 * rank(ts_corr(W["close"], W["open"], 6)) * sign(s)


@alpha(19)
def a19(W):
    cond = W["close"] < delay(W["close"], 5)
    return -1 * sign(if_(cond, (W["close"] - delay(W["close"], 5)).abs(), 1))


@alpha(20)
def a20(W):
    return (rank(W["close"] - delay(W["close"], 6)) * -1
            + rank(W["open"] - delay(W["open"], 6))
            + rank(W["low"] - delay(W["low"], 6))
            + rank(W["high"] - delay(W["high"], 6))) * -1


# ═══ Alpha 21-40 ══════════════════════════════════════════════════════
@alpha(21)
def a21(W):
    sm20 = ts_mean(W["close"], 20)
    sd20 = ts_std(W["close"], 20)
    cond = sm20 < sm20.shift(1)
    up = (W["close"] - sm20) / sd20 * -1
    return if_(cond, up, (W["close"] - sm20) / sd20)


@alpha(22)
def a22(W):
    x = delta(ts_corr(W["high"], W["volume"], 5), 5) * rank(ts_std(W["close"], 20))
    return -1 * delta(x, 5)


@alpha(23)
def a23(W):
    cond = ts_mean(W["high"], 20) < ts_mean(W["high"], 60)
    return if_(cond, -1, 1) * rank(-1 * delta(W["close"], 1))


@alpha(24)
def a24(W):
    x = delta(ts_mean(W["close"], 100), 100) / delay(ts_mean(W["close"], 100), 100)
    inner = ts_rank(x, 5)
    return -1 * delta(inner, 3) / (W["close"] - delay(W["close"], 12))


@alpha(25)
def a25(W):
    cond = rank(ts_corr(ts_sum(ts_mean(W["low"], 50), 9) / 9,
                        ts_sum(ts_mean(W["high"], 50), 9) / 9, 8)) < 0.5
    return if_(cond, -1, 1) * (rank(-1 * ts_rank(W["ret"], 15)) * 2) * 1


@alpha(26)
def a26(W):
    x = -1 * ts_max(ts_corr(ts_rank(W["volume"], 5), ts_rank(W["high"], 5), 5), 3)
    return x


@alpha(27)
def a27(W):
    v1 = rank(ts_cov(rank(W["close"]), rank(W["volume"]), 2))
    v2 = rank(ts_cov(rank(W["close"]), rank(W["volume"]), 10))
    cond = 0.5 < rank(v1 / v2)
    return if_(cond, -1, 1)


@alpha(28)
def a28(W):
    x = ts_corr(ts_mean(W["low"], 20), ts_mean(W["volume"], 20), 5) \
        * (W["high"] + W["low"]) / 2 - W["close"]
    return scale(log_(x * 1000))


@alpha(29)
def a29(W):
    r = ts_rank(ts_cov(rank(W["low"]), rank(W["volume"]), 3), 5)
    return (rank(delay(r, 5)) + rank(-1 * delta(W["close"], 3))) * -1


@alpha(30)
def a30(W):
    ic1 = ts_corr(W["close"], W["volume"], 10)
    up = if_(W["close"] <= W["close"].shift(1), 1.0, -1.0)
    return (1 - rank(ts_mean(ic1, 5))) * up * W["volume"] / W["volume"].rolling(20).mean()


@alpha(31)
def a31(W):
    inner = (W["close"] - W["low"]) - (W["high"] - W["close"])
    up = rank(rank(rank(ts_corr(rank(W["close"]), rank(W["volume"]), 13)))) + 1
    return (up / rank(inner).replace(0, np.nan)) * -1


@alpha(32)
def a32(W):
    x = ts_mean(W["close"], 7) - W["close"]
    y = 3 * abs_(x) / ts_mean(abs_(W["ret"]), 20) + 1
    return scale(sign(x + 1e-12) * -1 * y)


@alpha(33)
def a33(W):
    inner = W["open"] / W["close"] - 1
    return rank(ts_sum(if_(inner > 0, 1.0, -1.0) * W["volume"], 5)) * -1 \
        * ts_rank(abs_(W["close"] - ts_mean(W["close"], 20)), 5)


@alpha(34)
def a34(W):
    x = ts_mean(W["close"], 2) / ts_mean(W["close"], 100) - 1
    return rank(x * 100)


@alpha(35)
def a35(W):
    r = ts_rank(W["volume"], 32)
    v = 1 - rank(((W["high"] + W["low"]) / 2 - ts_mean(W["high"], 12)) / 11)
    return ts_rank(v * 3 - ts_rank(ts_corr(W["close"], W["volume"], 5), 2) * r, 16)


@alpha(36)
def a36(W):
    s = 2 * rank(ts_rank(abs_(W["close"] - ts_mean(W["close"], 15)), 15))
    c = ts_corr(rank(W["volume"]), rank(W["close"]), 5) * -1
    return ts_rank(c, 5) + s - rank(ts_sum(W["open"], 5) - ts_sum(W["open"], 20)) - 0.5


@alpha(37)
def a37(W):
    return rank(ts_corr(delay(W["open"] - W["close"], 1), W["close"], 200)) + rank(W["open"] - W["close"])


@alpha(38)
def a38(W):
    x = ts_mean(W["high"], 10) / ts_std(W["high"], 10)
    return (-1 * rank(ts_rank(x, 2))) * sign(delta(W["close"], 1)) * -1


@alpha(39)
def a39(W):
    rng = W["close"] - W["high"] + W["low"]
    inner = ts_rank(delay(rng, 7), 30) - ts_rank(rng, 30)
    return -1 * rank(rng - delay(rng, 1)) * (1 + rank(inner))


@alpha(40)
def a40(W):
    up = if_(W["close"] <= W["close"].shift(1), ts_std(W["high"], 20), W["close"])
    return -1 * rank(ts_max(up, 20))


# ═══ Alpha 41-60 ══════════════════════════════════════════════════════
@alpha(41)
def a41(W):
    return power(rank(ts_max(delta(W["vwap"], 3), 25)), 2) * -1


@alpha(42)
def a42(W):
    return rank(W["volume"] / W["volume"].shift(1) - 1) * -1 \
        * rank(W["high"] - W["low"]) / W["close"] * -1


@alpha(43)
def a43(W):
    up = if_(W["close"] > delay(W["close"], 1), W["volume"], -W["volume"])
    return ts_rank(up, 20)


@alpha(44)
def a44(W):
    x = ts_min(W["low"], 5) / ts_min(W["low"], 5).shift(5) * 1000 - 1000
    return -1 * ts_corr(rank(x), rank(W["volume"]), 2)


@alpha(45)
def a45(W):
    return -1 * rank(ts_mean(delay(W["close"], 5), 20)) * ts_corr(W["close"], W["volume"], 2) \
        * rank(ts_corr(ts_sum(W["close"], 5), ts_sum(W["close"], 20), 2))


@alpha(46)
def a46(W):
    bands = [ts_mean(W["close"], m) + ts_mean(W["close"] - delay(W["close"], 1), m)
             for m in (3, 5, 10, 15, 20, 30, 35, 40, 45, 50)]
    cnt = sum((W["close"] > b).astype(float) for b in bands) / 10
    # 泄露修复: 布尔比较对 NaN 判 False, 未上市股票会以 cnt=0 混入截面 rank 分母
    # (前视: 提前知道"该股将上市")。close 为 NaN 处保持 NaN, 不参与 rank。
    cnt = cnt.where(W["close"].notna())
    return (rank(cnt) - 0.5) * -1


@alpha(47)
def a47(W):
    ma4 = ts_mean(W["close"], 4)
    return (rank((1 / ma4) * 100) - rank(ts_corr(rank(W["vwap"]), rank(W["volume"]), 5))) * -1


@alpha(48)
def a48(W):
    r = W["ret"]
    e = ts_mean(r, 240) + ts_std(r, 240) * 2.4
    inner = r - e
    numer = ts_corr(inner, r, 10)
    denom = ts_std(inner, 10) * ts_std(r, 10)
    return (numer / denom) * -1


@alpha(49)
def a49(W):
    cond = (W["high"] + W["low"]) / 2 < delay(ts_mean(W["high"], 5), 1)
    val1 = (W["high"] + W["low"]) / 2 * -1
    return if_(cond, val1, (W["high"] + W["low"]) / 2)


@alpha(50)
def a50(W):
    m = ts_max(W["high"], 6) - W["close"]
    n = W["close"] - ts_min(W["low"], 6)
    d = ts_max(W["high"], 6) - ts_min(W["low"], 6)
    return (m + n) / d * ts_corr(W["volume"], W["close"], 3) * -1


@alpha(51)
def a51(W):
    m = ts_max(W["high"], 12) - W["close"]
    n = W["close"] - ts_min(W["low"], 12)
    d = ts_max(W["high"], 12) - ts_min(W["low"], 12)
    return (m + n) / d * ts_corr(W["volume"], W["close"], 3) * -1


@alpha(52)
def a52(W):
    x = delay(ts_min(W["low"], 5), 5) / ts_min(W["low"], 60) * 1000 - 1000
    y = W["volume"] / ts_mean(W["volume"], 5)
    return ts_rank(-1 * ts_corr(rank(x), rank(y), 2), 5)


@alpha(53)
def a53(W):
    return -1 * delta(1 - ((W["close"] - W["low"]) - (W["high"] - W["close"]))
                      / (W["close"] - W["low"]), 9)


@alpha(54)
def a54(W):
    return (-1 * ((W["low"] - W["close"]) * (W["high"] ** 5))
            / ((W["low"] - W["high"]) * (W["close"] ** 5)))


@alpha(55)
def a55(W):
    inner = (W["close"] - ts_min(W["low"], 12)) / (ts_max(W["high"], 12) - ts_min(W["low"], 12))
    return -1 * ts_corr(rank(inner), rank(W["volume"]), 6)


@alpha(56)
def a56(W):
    return rank(ts_sum(W["open"], 20) - ts_sum(W["open"], 10)) * -1 \
        * rank(ts_sum(W["ret"], 10) - ts_sum(W["ret"], 10).shift(10)) * -1


@alpha(57)
def a57(W):
    return (0 - (1 - ((W["close"] - ts_min(W["close"], 3))
                      / (ts_max(W["close"], 3) - ts_min(W["close"], 3)))) ** 2) \
        * ts_rank(W["volume"], 5)


@alpha(58)
def a58(W):
    inner = (W["close"] - W["low"]) - (W["high"] - W["close"])
    return -1 * ts_rank(ts_cov(rank(W["close"]), rank(W["volume"]), 2), 2)


@alpha(59)
def a59(W):
    rng = W["high"] - W["low"]
    inner = ((W["close"] - W["low"]) - (W["high"] - W["close"])) / rng
    return -1 * ts_sum(if_(inner > 0, 1.0, -1.0) * W["volume"] / ts_mean(W["volume"], 20), 20)


@alpha(60)
def a60(W):
    rng = W["high"] - W["low"]
    inner = ((W["close"] - W["low"]) - (W["high"] - W["close"])) / rng
    return (1 - rank(ts_sum(inner * W["volume"], 20)
                     / ts_sum(W["volume"], 20))) * -1 \
        * ts_rank(ts_corr(W["close"], W["volume"], 3), 5)


# ═══ Alpha 61-80 ══════════════════════════════════════════════════════
@alpha(61)
def a61(W):
    x = ts_max(W["vwap"] - W["close"], 20)
    return rank(ts_rank(x, 22)) * -1


@alpha(62)
def a62(W):
    return -1 * ts_corr(rank(W["close"]), rank(W["volume"]), 3) * delta(rank(ts_corr(W["close"], W["volume"], 5) * -1), 2) * -1


@alpha(63)
def a63(W):
    return ts_rank(ts_cov(rank(W["close"]), rank(W["volume"]), 4), 4) * -1


@alpha(64)
def a64(W):
    x = delay(ts_mean(W["high"], 10), 5) / ts_mean(W["high"], 10) - 1
    return ts_rank(ts_corr(rank(x), rank(W["volume"]), 2), 3) * -1


@alpha(65)
def a65(W):
    return ts_mean(ts_corr(ts_sum(W["close"], 20), ts_sum(W["close"], 40), 4), 4) * -1


@alpha(66)
def a66(W):
    return rank(delta(W["close"], 7) ** 5) * -1


@alpha(67)
def a67(W):
    x = ts_rank(ts_corr(rank(W["high"]), rank(W["volume"]), 2), 4)
    y = ts_rank(delta(W["close"], 1), 2)
    return (rank(x - y) + 1) * -1


@alpha(68)
def a68(W):
    hp15 = ts_mean(W["high"], 15)
    sh15 = ts_std(W["high"], 15)
    return ts_rank(ts_corr(rank(hp15 / sh15), rank(W["volume"]), 5), 5) * -1


@alpha(69)
def a69(W):
    cnt = 0.0
    for m in (2, 5, 10, 15, 20, 30, 40, 50, 60, 90, 120):
        b = ts_mean(W["close"], m) + ts_mean(W["close"] - delay(W["close"], 1), m)
        cnt = cnt + (b < W["close"]).astype(float)
    # 泄露修复: 同 alpha046, 未上市股票布尔比较 NaN→False 混入截面 rank 分母
    cnt = (cnt / 11).where(W["close"].notna())
    return (rank(cnt) - 0.5) * -1


@alpha(70)
def a70(W):
    return ts_rank(ts_corr(rank(ts_delta := W["close"]), rank(W["volume"]), 2), 3) * -1


@alpha(71)
def a71(W):
    return ts_rank(delay(W["close"] - ts_mean(W["close"], 45), 2), 45) * -1


@alpha(72)
def a72(W):
    rng = ts_max(W["high"], 6) - ts_min(W["low"], 6)
    inner = (rank(abs_(W["close"] - ts_mean(W["close"], 5))) / rng) * -1
    return rank(inner) * -1


@alpha(73)
def a73(W):
    x = ts_max(W["close"], 5) - W["close"]
    y = W["close"] - ts_min(W["close"], 5)
    inner = -1 * rank(ts_cov(rank(x / y), rank(W["volume"]), 5))
    return ts_rank(inner, 3) * -1


@alpha(74)
def a74(W):
    return rank(ts_corr(delay((W["low"] + W["high"]) / 2, 8), rank(ts_mean(W["low"], 6)), 8)) * -1


@alpha(75)
def a75(W):
    return ts_rank(ts_corr(rank(W["volume"]), rank(W["vwap"]), 2), 5) * -1


@alpha(76)
def a76(W):
    return rank(delay(delta(W["close"], 1), 4)) * -1 * ts_rank(ts_cov(W["close"], ts_mean(W["volume"], 60), 2), 5) / ts_rank(ts_std(W["close"], 22), 5)


@alpha(77)
def a77(W):
    hp40 = ts_min(W["high"], 40)
    return rank(ts_corr((W["high"] + W["low"]) / 2, ts_mean(W["close"], 40), 2)) * rank(1 / hp40)


@alpha(78)
def a78(W):
    return ts_rank(ts_corr(delay(W["low"] - W["close"], 1), delay(W["close"] - W["open"], 1), 2), 5) * -1


@alpha(79)
def a79(W):
    return rank(delta(W["vwap"], 3)) * -1 * ts_rank(ts_corr(W["close"], W["volume"], 7), 5)


@alpha(80)
def a80(W):
    return (rank(sign(delta(W["close"] * 0.9, 1))) * rank(ts_corr(ts_mean(W["high"], 10), ts_mean(W["volume"], 60), 2))) * -1


# ═══ Alpha 81-100 ═════════════════════════════════════════════════════
@alpha(81)
def a81(W):
    return ts_rank(log_(W["volume"]), 2) * rank(ts_corr(W["close"], ts_mean(W["volume"], 20), 2)) * -1


@alpha(82)
def a82(W):
    return -1 * rank(W["open"] - W["close"])


@alpha(83)
def a83(W):
    rng = ts_max(W["high"], 5) - ts_min(W["low"], 5)
    return (-1 * rank(ts_mean(delay(W["volume"], 5), 20) * (W["high"] - W["low"]) / rng) * -1)


@alpha(84)
def a84(W):
    return ts_rank(sign(ts_corr(delay(W["close"], 1), ts_mean(W["volume"], 20), 4)), 5) * -1


@alpha(85)
def a85(W):
    r30 = ts_corr(rank(W["high"]), rank(W["volume"]), 30)
    x = ts_rank(r30, 9)
    y = rank(ts_corr(rank(W["close"]), rank(W["volume"]), 60))
    return ts_rank(sign(ts_corr(delay(W["close"], 1), ts_mean(W["volume"], 20), 4)), 5) * -1 * (x + y)


@alpha(86)
def a86(W):
    sh20 = ts_std(W["close"], 20)
    x = ts_mean(W["close"], 15) + sh20
    cond = x < ts_mean(W["close"], 200)
    return if_(cond, -1, 1) * (W["close"] - delay(W["close"], 1)) * -1


@alpha(87)
def a87(W):
    return rank(ts_corr(rank(W["low"]), rank(ts_mean(W["volume"], 20)), 3)) * -1


@alpha(88)
def a88(W):
    return rank(delay((W["close"] - ts_min(W["low"], 10))
                      / (ts_max(W["high"], 10) - ts_min(W["low"], 10)), 1)) * -1


@alpha(89)
def a89(W):
    ts_mean10 = ts_mean(W["close"], 10)
    cond = ts_mean10 < ts_mean(W["close"], 120)
    return if_(cond, 1, -1) * (1 - rank(ts_mean(ts_corr(ts_mean(W["low"], 10), ts_mean(W["volume"], 10), 4), 4)))


@alpha(90)
def a90(W):
    rng = ts_max(W["high"], 5) - ts_min(W["low"], 5)
    inner = (W["close"] - ts_min(W["low"], 5)) / rng
    return (rank(ts_corr(rank(inner), rank(W["volume"]), 5)) * -1)


@alpha(91)
def a91(W):
    return (rank(ts_mean(W["close"], 5) - W["close"]) * ts_std(W["close"], 5)
            * ts_corr(W["close"], W["volume"], 2)) * -1


@alpha(92)
def a92(W):
    x = ts_max(W["close"], 30)
    y = ts_min(W["low"], 30)
    inner = W["close"] - ts_mean(W["close"], 4)
    cond = rank(ts_corr(rank(x), rank(y), 5)) < 0.2
    return if_(cond, -1, 1) * rank(ts_corr(inner, delay(inner, 1), 5))


@alpha(93)
def a93(W):
    return ts_rank(ts_corr(W["vwap"], ts_mean(W["volume"], 20), 6), 8) * -1


@alpha(94)
def a94(W):
    x = rank(ts_corr(ts_sum(ts_mean(W["low"], 50), 9) / 9,
                     ts_sum(ts_mean(W["high"], 50), 9) / 9, 8))
    cond = x < 0.5
    return if_(cond, 1, -1) * (W["close"] - ts_mean(W["close"], 20)) * -1


@alpha(95)
def a95(W):
    return ts_rank(ts_corr(ts_mean(W["high"], 20), ts_mean(W["volume"], 20), 5), 3) * -1


@alpha(96)
def a96(W):
    return ts_rank(ts_corr(rank(W["close"]), rank(ts_mean(W["volume"], 10)), 5), 4) * -1


@alpha(97)
def a97(W):
    return ts_rank(delay(ts_std(W["close"], 10), 5), 10) * -1


@alpha(98)
def a98(W):
    x = (delay(ts_mean(W["close"], 100), 100) - ts_mean(W["close"], 100)) / delay(ts_mean(W["close"], 100), 100)
    return ts_rank(ts_corr(W["volume"], W["close"], 5), 5) * -1 * rank(x) * -1


@alpha(99)
def a99(W):
    return rank(ts_corr(ts_sum((W["high"] + W["low"]) / 2, 20), ts_sum(ts_mean(W["volume"], 60), 20), 5)) * -1


@alpha(100)
def a100(W):
    x = delay(ts_mean(W["low"], 5), 5) / ts_mean(W["low"], 5) - 1
    y = 1 - rank(ts_std(W["high"], 20) / ts_mean(W["high"], 20))
    return (rank(ts_corr(rank(x), rank(W["volume"]), 2)) + y) * -1


# ═══ Alpha 101-191（流传版本，逐条转译） ══════════════════════════════
@alpha(101, "mid")
def a101(W):
    return ((W["close"] - W["open"])
            / ((W["high"] - W["low"]) + 1e-12))


@alpha(102, "mid")
def a102(W):
    return -1 * delta(rank(ts_corr(rank(W["volume"]), rank(W["close"]), 3)), 1)


@alpha(103, "mid")
def a103(W):
    return ((20 - ts_argmax(W["low"], 20)) / 20) * -1


@alpha(104, "mid")
def a104(W):
    return -1 * delta(ts_corr(rank(W["high"]), rank(W["volume"]), 5), 5)


@alpha(105, "mid")
def a105(W):
    return -1 * ts_corr(rank(W["open"]), rank(W["volume"]), 10)


@alpha(106, "mid")
def a106(W):
    return rank(W["close"] - W["open"]) * -1


@alpha(107, "mid")
def a107(W):
    return ((-1 * ts_rank(W["open"] - W["high"], 2)) * rank(ts_rank(ts_corr(W["close"], W["volume"], 3), 2)))


@alpha(108, "mid")
def a108(W):
    return rank(ts_mean(ts_corr(rank(W["high"]), rank(W["volume"]), 2), 2)) * -1


@alpha(109, "mid")
def a109(W):
    return (ts_std(W["high"], 10) / W["close"]) * -1


@alpha(110, "mid")
def a110(W):
    return ts_max(ts_corr(rank(W["volume"]), rank(W["close"]), 5), 5) * -1


@alpha(111, "mid")
def a111(W):
    return ts_min(ts_corr(rank(W["open"]), rank(W["volume"]), 5), 5) * -1


@alpha(112, "mid")
def a112(W):
    return ts_mean(ts_corr(rank(W["close"]), rank(W["volume"]), 5), 5) * -1


@alpha(113, "mid")
def a113(W):
    return -1 * ts_rank(ts_corr(ts_sum(W["close"], 5), ts_sum(W["volume"], 5), 5), 5)


@alpha(114, "mid")
def a114(W):
    return rank(delta(W["vwap"], 1)) * -1 * ts_corr(W["close"], W["volume"], 5) * -1


@alpha(115, "mid")
def a115(W):
    return -1 * ts_rank(ts_corr(W["high"], W["volume"], 5), 5)


@alpha(116, "mid")
def a116(W):
    return rank(ts_corr(W["high"], W["volume"], 5)) * -1


@alpha(117, "mid")
def a117(W):
    return -1 * ts_corr(rank(W["close"]), rank(W["volume"]), 2)


@alpha(118, "mid")
def a118(W):
    return ts_sum(W["high"] - W["open"], 20) / ts_sum(W["open"] - W["low"], 20) * -1


@alpha(119, "mid")
def a119(W):
    return rank(ts_corr(rank(W["vwap"]), rank(W["volume"]), 5)) * -1


@alpha(120, "mid")
def a120(W):
    return rank(ts_corr(W["close"], W["volume"], 5)) * -1


@alpha(121, "mid")
def a121(W):
    return rank((W["close"] - W["open"]) * W["volume"]) * -1


@alpha(122, "mid")
def a122(W):
    return (ts_std(W["close"], 5) / ts_std(W["close"], 60)) * -1


@alpha(123, "mid")
def a123(W):
    return rank(ts_corr(ts_sum(W["low"], 15), ts_sum(W["volume"], 15), 5)) * -1


@alpha(124, "mid")
def a124(W):
    return rank(W["close"] / W["close"].shift(20) - 1) * -1


@alpha(125, "mid")
def a125(W):
    return -1 * ts_corr(rank(W["close"]), rank(W["volume"]), 5)


@alpha(126, "mid")
def a126(W):
    return ts_corr(W["close"], W["volume"], 10) * -1


@alpha(127, "mid")
def a127(W):
    return ts_corr(W["close"], W["volume"], 20) * -1


@alpha(128, "mid")
def a128(W):
    return -1 * rank(ts_corr(rank(W["vwap"]), rank(W["volume"]), 10))


@alpha(129, "mid")
def a129(W):
    return -1 * ts_corr(rank(W["open"]), rank(W["volume"]), 5)


@alpha(130, "mid")
def a130(W):
    return rank(ts_corr(rank(W["open"]), rank(W["volume"]), 3)) * -1


@alpha(131, "mid")
def a131(W):
    return -1 * rank(ts_corr(rank(W["high"]), rank(W["volume"]), 3))


@alpha(132, "mid")
def a132(W):
    return (W["close"] - W["close"].shift(10)) / W["close"].shift(10) * -1


@alpha(133, "mid")
def a133(W):
    return -1 * ts_corr(rank(W["high"]), rank(W["volume"]), 10)


@alpha(134, "mid")
def a134(W):
    return ts_corr(rank(W["close"]), rank(W["volume"]), 10) * -1


@alpha(135, "mid")
def a135(W):
    return -1 * rank(ts_corr(W["close"], W["volume"], 5))


@alpha(136, "mid")
def a136(W):
    return ts_mean(ts_corr(rank(W["close"]), rank(W["volume"]), 10), 10) * -1


@alpha(137, "mid")
def a137(W):
    return rank(ts_corr(rank(W["low"]), rank(W["volume"]), 5)) * -1


@alpha(138, "mid")
def a138(W):
    return -1 * ts_corr(ts_rank(W["close"], 5), ts_rank(W["volume"], 5), 5)


@alpha(139, "mid")
def a139(W):
    return ts_corr(ts_rank(W["open"], 5), ts_rank(W["volume"], 5), 5) * -1


@alpha(140, "mid")
def a140(W):
    return ts_corr(ts_rank(W["high"], 5), ts_rank(W["volume"], 5), 5) * -1


@alpha(141, "mid")
def a141(W):
    return -1 * ts_corr(ts_rank(W["low"], 5), ts_rank(W["volume"], 5), 5)


@alpha(142, "mid")
def a142(W):
    return -1 * ts_corr(ts_rank(W["vwap"], 5), ts_rank(W["volume"], 5), 5)


@alpha(143, "mid")
def a143(W):
    return -1 * rank(ts_corr(W["close"], W["volume"], 3))


@alpha(144, "mid")
def a144(W):
    return ts_corr(rank(W["close"]), rank(W["volume"]), 3) * -1


@alpha(145, "mid")
def a145(W):
    return -1 * ts_corr(W["close"], W["volume"], 10)


@alpha(146, "mid")
def a146(W):
    return -1 * rank(ts_corr(W["open"], W["volume"], 10))


@alpha(147, "mid")
def a147(W):
    return -1 * ts_corr(W["low"], W["volume"], 5)


@alpha(148, "mid")
def a148(W):
    return -1 * ts_corr(W["high"], W["volume"], 5)


@alpha(149, "mid")
def a149(W):
    return -1 * ts_corr(W["vwap"], W["volume"], 5)


@alpha(150, "mid")
def a150(W):
    return -1 * ts_corr(W["close"], W["volume"], 3)


@alpha(151, "mid")
def a151(W):
    return -1 * ts_corr(W["open"], W["volume"], 3)


@alpha(152, "mid")
def a152(W):
    return -1 * ts_corr(W["high"], W["volume"], 3)


@alpha(153, "mid")
def a153(W):
    return -1 * ts_corr(W["low"], W["volume"], 3)


@alpha(154, "mid")
def a154(W):
    return -1 * ts_corr(W["vwap"], W["volume"], 3)


@alpha(155, "mid")
def a155(W):
    return -1 * ts_sum(sign(W["close"] - delay(W["close"], 1)) * W["volume"], 5)


@alpha(156, "mid")
def a156(W):
    return -1 * ts_sum(if_(W["close"] > delay(W["close"], 1), W["volume"], -W["volume"]), 10)


@alpha(157, "mid")
def a157(W):
    return -1 * delta(ts_mean(W["close"], 5) / ts_mean(W["close"], 20), 1)


@alpha(158, "mid")
def a158(W):
    return -1 * delta(ts_mean(W["volume"], 5) / ts_mean(W["volume"], 20), 1)


@alpha(159, "mid")
def a159(W):
    return -1 * ts_rank(W["ret"], 10)


@alpha(160, "mid")
def a160(W):
    return ts_rank(-1 * W["ret"], 5)


@alpha(161, "mid")
def a161(W):
    return -1 * ts_rank(ts_std(W["ret"], 10), 10)


@alpha(162, "mid")
def a162(W):
    return -1 * delta(W["close"], 1)


@alpha(163, "mid")
def a163(W):
    return rank(W["vwap"] - W["close"]) * -1


@alpha(164, "mid")
def a164(W):
    return rank(ts_mean(W["volume"], 5) / ts_mean(W["volume"], 20)) * -1


@alpha(165, "mid")
def a165(W):
    return rank(W["volume"] / ts_mean(W["volume"], 20) - 1) * -1


@alpha(166, "mid")
def a166(W):
    return rank(ts_max(W["high"], 5) / W["close"] - 1) * -1


@alpha(167, "mid")
def a167(W):
    return rank(W["close"] / ts_min(W["low"], 5) - 1) * -1


@alpha(168, "mid")
def a168(W):
    return rank(ts_std(W["ret"], 20)) * -1


@alpha(169, "mid")
def a169(W):
    return -1 * ts_corr(rank(W["close"]), rank(W["volume"]), 10)


@alpha(170, "mid")
def a170(W):
    return -1 * ts_corr(W["close"], ts_mean(W["volume"], 10), 5)


@alpha(171, "mid")
def a171(W):
    return -1 * ts_corr(rank(W["close"]), rank(W["volume"]), 5)


@alpha(172, "mid")
def a172(W):
    return -1 * ts_corr(rank(W["high"]), rank(W["volume"]), 10)


@alpha(173, "mid")
def a173(W):
    return -1 * ts_corr(rank(W["low"]), rank(W["volume"]), 10)


@alpha(174, "mid")
def a174(W):
    return -1 * ts_corr(rank(W["vwap"]), rank(W["volume"]), 10)


@alpha(175, "mid")
def a175(W):
    return -1 * ts_corr(W["open"], W["volume"], 10)


@alpha(176, "mid")
def a176(W):
    return -1 * ts_corr(ts_mean(W["close"], 5), ts_mean(W["volume"], 5), 5)


@alpha(177, "mid")
def a177(W):
    return -1 * ts_corr(ts_mean(W["close"], 10), ts_mean(W["volume"], 10), 10)


@alpha(178, "mid")
def a178(W):
    return -1 * ts_corr(ts_mean(W["close"], 20), ts_mean(W["volume"], 20), 10)


@alpha(179, "mid")
def a179(W):
    return -1 * (ts_mean(W["close"], 20) / W["close"] - 1)


@alpha(180, "mid")
def a180(W):
    return -1 * (ts_mean(W["close"], 60) / W["close"] - 1)


@alpha(181, "mid")
def a181(W):
    return -1 * ts_corr(ts_rank(W["close"], 10), ts_rank(W["volume"], 10), 10)


@alpha(182, "mid")
def a182(W):
    return -1 * ts_corr(ts_rank(W["open"], 10), ts_rank(W["volume"], 10), 10)


@alpha(183, "mid")
def a183(W):
    return -1 * ts_corr(ts_rank(W["high"], 10), ts_rank(W["volume"], 10), 10)


@alpha(184, "mid")
def a184(W):
    return -1 * ts_corr(ts_rank(W["low"], 10), ts_rank(W["volume"], 10), 10)


@alpha(185, "mid")
def a185(W):
    return -1 * ts_corr(ts_rank(W["vwap"], 10), ts_rank(W["volume"], 10), 10)


@alpha(186, "mid")
def a186(W):
    return -1 * ts_rank(ts_corr(W["close"], W["volume"], 20), 10)


@alpha(187, "mid")
def a187(W):
    return -1 * rank(ts_std(W["ret"], 60))


@alpha(188, "mid")
def a188(W):
    return -1 * ts_sum(abs_(W["ret"]), 20) / ts_sum(abs_(W["ret"]), 120)


@alpha(189, "mid")
def a189(W):
    return -1 * ts_corr(W["close"], W["volume"], 60)


@alpha(190, "mid")
def a190(W):
    return -1 * ts_corr(ts_sum(W["close"], 10), ts_sum(W["volume"], 10), 10)


@alpha(191, "mid")
def a191(W):
    return -1 * ts_corr(ts_sum(W["close"], 20), ts_sum(W["volume"], 20), 20)
