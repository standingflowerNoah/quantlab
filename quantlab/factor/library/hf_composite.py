"""hf6 因子集 walk-forward 合成（信号层观察仓）
=====================================================
研究报告《分钟高频因子研究》9.4 节上线判定形态的工程化落地：
  「12 个月训练窗 + ICIR 加权 + hf6（amtrange/corr_rv/dsem/rsk/rvvol/smartq）」

机制（严格无前视，与 scripts/highfreq_walkforward.py 口径一致）：
  - 每个输出日 t：训练窗 = [purge_end - 12 个月, purge_end]，
    其中 purge_end = t 往前第 21 个交易日——窗内任意 d 的 fwd20
    终点 ≤ t-1，不泄漏；**对输入因子迟到 ≤20 交易日天然免疫**
  - 方向 dir_n = sign(训练窗内中性化因子逐日 IC 均值)（IC 日 ≥ 40）
  - 权重 w_n = max(训练窗内 ICIR, 0)（负 ICIR 因子当日零权剔除）
  - 合成值 = Σ w_n·dir_n·z_n / Σ w_n，z_n 为当日截面 zscore
  - 中性化：截面 OLS 残差（size/turnover/volatility_20 截面 rank 归一，
    当日截面信息，无前视；全历史预中性化合法）

判定依据（2026-09-07，16 个 OOS 月）：OOS IC +0.035 / ICIR 0.41 /
多空年化 +11.1% / 夏普 0.83——**观察仓形态，不进实盘加权**，
积累 24+ OOS 月后复评。

工程约定：
  - 注册名 hf_wf_composite 字母序在全部 hf_* 输入之后——compute_all()
    内先算输入后算本因子，从因子湖读到的输入是当日新值
  - 风格因子（size/turnover/volatility_20）字母序在本因子之后，
    **必须在 compute() 内现算**（get_factor().compute()），不可读湖
  - 每日全量重算 + append_factor（date+code 去重幂等），~2.5 分钟
  - 非 SqlFactor：审计 PIT 检查自动跳过（构造保证），结构/分布/
    覆盖/IC 审查照常——IC 审查记录即观察仓的前向跟踪起点
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..base import Factor
from ..registry import register, get_factor

HF6 = ["hf_amtrange_20", "hf_corr_rv_20", "hf_dsem_20",
       "hf_rsk_20", "hf_rvvol_20", "hf_smartq_10"]
STYLES = ["size", "turnover", "volatility_20"]
TRAIN_MONTHS = 12
PURGE_TD = 21            # t 往前第 21 个交易日为训练窗末（其 fwd20 ≤ t-1）
MIN_IC_DAYS = 40         # 训练窗内最少 IC 日数
MIN_XSEC = 100           # 中性化最少截面股票数

_FWD20_SQL = """
SELECT date, code, c_lead / c - 1 AS fwd FROM (
    SELECT date, code, close * adj_factor AS c,
           LEAD(close * adj_factor, 20) OVER (
               PARTITION BY code ORDER BY date) AS c_lead
    FROM kline_daily)
WHERE c_lead IS NOT NULL AND c > 0
"""


def _zscore(g: pd.Series) -> pd.Series:
    s = g.std()
    return (g - g.mean()) / s if s and np.isfinite(s) else g * np.nan


def _neutralize(fv: pd.DataFrame, st: pd.DataFrame) -> pd.DataFrame:
    """截面 OLS 残差（风格 rank 归一）；st: date,code,<STYLES>"""
    df = fv.merge(st, on=["date", "code"], how="left")
    for c in STYLES:
        df[c] = df.groupby("date")[c].transform(
            lambda x: x.rank(pct=True) - 0.5)
    df = df.dropna(subset=STYLES + ["value"])
    parts = []
    for d, g in df.groupby("date"):
        if len(g) < MIN_XSEC:
            continue
        X = np.column_stack([np.ones(len(g))] +
                            [g[c].values for c in STYLES])
        y = g["value"].values.astype(float)
        try:
            beta = np.linalg.lstsq(X, y, rcond=None)[0]
            parts.append(g[["date", "code"]].assign(value=y - X @ beta))
        except np.linalg.LinAlgError:
            continue
    return (pd.concat(parts, ignore_index=True)
              .sort_values(["date", "code"]).reset_index(drop=True))


def _day_ic(fv: pd.DataFrame, fwd: pd.DataFrame) -> pd.Series:
    """逐日截面 Spearman（index=date）"""
    m = fv.merge(fwd, on=["date", "code"], how="inner")
    out = {}
    for d, g in m.groupby("date"):
        if len(g) < 30:
            continue
        out[d] = g["value"].rank().corr(g["fwd"].rank())
    return pd.Series(out, dtype=float)


@register
class HfWfComposite(Factor):
    name = "hf_wf_composite"
    description = ("hf6 walk-forward 合成（12月训练窗+ICIR 加权+风格中性，"
                   "purge 20 交易日无前视；观察仓形态——OOS ICIR 0.41，"
                   "不进实盘加权，见研究报告 9.4 节）")
    category = "highfreq"

    def compute(self, store, start=None, end=None,
                universe: list[str] | None = None) -> pd.DataFrame:
        # 1) 输入因子：因子湖（compute_all 字母序保证当日新鲜）
        fvs = {}
        for n in HF6:
            fv = store.read_factor(n)
            if fv.empty:
                raise RuntimeError(f"输入因子 {n} 不存在，先 compute_all")
            fv["date"] = pd.to_datetime(fv["date"])
            fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
            if universe:
                fv = fv[fv["code"].isin(universe)]
            fvs[n] = fv.dropna(subset=["value"])
        if min(len(v) for v in fvs.values()) < 50_000:
            raise RuntimeError("hf6 输入因子覆盖不足（<5 万行）")

        # 2) 风格因子：现算（字母序在本因子之后，湖内无当日值）
        st = None
        for c in STYLES:
            fv = get_factor(c).compute(store, universe=universe)
            fv = fv.rename(columns={"value": c})[["date", "code", c]]
            st = fv if st is None else st.merge(
                fv, on=["date", "code"], how="outer")
        if st is None or st.empty:
            raise RuntimeError("风格因子计算为空")

        # 3) fwd20（训练窗 IC 用；输出日 t 本身不需要 fwd）
        fwd = store.q(_FWD20_SQL)
        fwd["date"] = pd.to_datetime(fwd["date"])
        fwd = fwd[["date", "code", "fwd"]].dropna()
        if universe:
            fwd = fwd[fwd["code"].isin(universe)]

        # 4) 交易日历（kline 全历史，用于 purge 定位）——不取 fwd 的
        #    日期集：输出日 t 本身不需要 fwd20（训练窗 IC 已被 purge
        #    限制在 ≤ t-21td），合成值应延伸到最新输入日
        days = (store.q("SELECT DISTINCT date FROM kline_daily "
                        "ORDER BY date")["date"]
                .dt.normalize().drop_duplicates().tolist())
        days = sorted(pd.to_datetime(d) for d in days)
        if len(days) < PURGE_TD + 10:
            return pd.DataFrame(columns=["date", "code", "value"])
        day_idx = {d: i for i, d in enumerate(days)}

        # 5) 中性化 + 逐日 IC（每因子一次）
        pure, ic = {}, {}
        for n in HF6:
            p = _neutralize(fvs[n], st)
            if p.empty:
                continue
            pure[n] = p
            ic[n] = _day_ic(p, fwd)
        if not pure:
            return pd.DataFrame(columns=["date", "code", "value"])

        # 6) 逐输出日：训练窗 → 方向/权重 → 合成（矩阵化）
        #    输出日 = 有训练窗数据的交易日（含非调仓日，日更观察）
        z = {n: p.assign(z=p.groupby("date")["value"].transform(_zscore))
             for n, p in pure.items()}
        # 宽表：date,code + 每因子 z 列（inner 口径与 walkforward 一致）
        base = None
        for n in HF6:
            if n not in z:
                continue
            v = z[n][["date", "code", "z"]].rename(columns={"z": n})
            base = v if base is None else base.merge(
                v, on=["date", "code"], how="inner")
        if base is None:
            return pd.DataFrame(columns=["date", "code", "value"])
        cols = [n for n in HF6 if n in base.columns]

        # 逐日权重（dir × w），仅对可训练的输出日
        out_days = sorted(d for d in base["date"].unique()
                          if d in day_idx and day_idx[d] >= PURGE_TD)
        W = {}                     # date -> (weights dict)
        for t in out_days:
            purge_end = days[day_idx[t] - PURGE_TD]
            w_start = purge_end - pd.DateOffset(months=TRAIN_MONTHS)
            wd, ww = {}, {}
            for n in cols:
                s = ic[n]
                w = s[(s.index >= w_start) & (s.index <= purge_end)]
                if len(w) < MIN_IC_DAYS or not np.isfinite(w.mean()):
                    continue
                wd[n] = np.sign(w.mean())
                ww[n] = max(w.mean() / w.std(), 0.0) if w.std() > 0 else 0.0
            if wd and sum(ww.values()) > 0:
                W[t] = (wd, ww)

        if not W:
            return pd.DataFrame(columns=["date", "code", "value"])

        # 合成：Σ w·dir·z / Σ w（NaN z 按 0 计入分子、权重照常计入
        # 分母——与 walkforward 事件口径一致）
        bsub = base[base["date"].isin(W.keys())]
        Z = bsub[cols].to_numpy(dtype=float)          # (rows, k)
        uniq, inv = np.unique(bsub["date"].to_numpy(),
                              return_inverse=True)    # inv: 行 -> 唯一日
        dirU = np.array([[W[pd.Timestamp(t)][0].get(n, 0.0) for n in cols]
                         for t in uniq])              # (n_day, k)
        wU = np.array([[W[pd.Timestamp(t)][1].get(n, 0.0) for n in cols]
                       for t in uniq])
        dirM, wM = dirU[inv], wU[inv]                 # 广播到行
        num = np.nansum(Z * dirM * wM, axis=1)
        den = wM.sum(axis=1)
        comp = np.where(den > 0, num / np.where(den > 0, den, 1.0), np.nan)

        out = bsub[["date", "code"]].assign(value=comp)
        out = out.dropna(subset=["value"])
        return (out.sort_values(["date", "code"])
                   .reset_index(drop=True)[["date", "code", "value"]])
