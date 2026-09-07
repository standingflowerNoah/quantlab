"""a191_wf_composite：Alpha191 top 因子 walk-forward 合成（生产因子）
=====================================================
《Alpha191深化检验》（research/Alpha191深化检验.md）判定形态的工程化：
  「12 个月训练窗 + ICIR 加权 + top10 去重（剔 alpha148）9 因子 + 风格中性」

判定依据（2026-09-07，48 个 OOS 月 2022-10~2026-08）：OOS IC +0.0302 /
ICIR +0.55 / 多空年化 +6.7% / 夏普 0.85，前视上界 0.56——WF 衰减≈0，
项目内首个以足够样本量通过 WF 硬约束的因子族。
风险纪律：2023 型失效年真实存在（当年 OOS 多空 -8.9%），上线后监控
滚动 IC；6 成预测力为风格暴露，禁止裸用 raw 口径。

机制（与 hf_wf_composite 同构，scripts/alpha191_deep.py 口径）：
  - 每个输出日 t：训练窗 = [purge_end - 12 个月, purge_end]，
    purge_end = t 往前第 21 个交易日（窗内 fwd20 终点 ≤ t-1，无前视）
  - 方向 dir_n = sign(训练窗内中性化因子逐日 IC 均值)（IC 日 ≥ 40）
  - 权重 w_n = max(训练窗内 ICIR, 0)
  - 合成值 = Σ w_n·dir_n·z_n / Σ w_n（当日截面 zscore）
  - 中性化：截面 OLS 残差（size/turnover/volatility_20 rank 归一）

工程约定（与 hf_composite 的关键差异）：
  - **Alpha191 因子湖为静态快照（compute_alpha191 按需批量，无日更）**
    ——本因子 compute() 内部 build_wide + 9 个公式现算，不读 alpha 湖
  - 注册名 a191_wf_composite 字母序在全部因子之前（a 开头），但无湖内
    依赖（公式/风格/fwd 全部现算），compute_all 执行序无影响
  - 宽表起点 2022-06-01（与研究口径一致，rolling 预热）
  - 每日全量重算 ~10 分钟（build_wide ~2 min + 9 公式 ~2 min +
    中性化 ~5 min），夜间流水线可接受
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..base import Factor
from ..registry import register, get_factor
from ..alpha191 import build_wide, _to_long
from ..alpha191_formulas import ALPHAS

# top10 去重：alpha148 与 alpha116 相关 +0.977（同公式变体，纯净 IC
# alpha116 更优 0.031/0.48 vs 0.022/0.34），剔 alpha148 留 9 因子
A191 = ["alpha013", "alpha015", "alpha016", "alpha026", "alpha055",
        "alpha116", "alpha119", "alpha126", "alpha183"]
STYLES = ["size", "turnover", "volatility_20"]
TRAIN_MONTHS = 12
PURGE_TD = 21            # t 往前第 21 个交易日为训练窗末（其 fwd20 ≤ t-1）
MIN_IC_DAYS = 40
MIN_XSEC = 100
WIDE_START = "2022-06-01"

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
class A191WfComposite(Factor):
    name = "a191_wf_composite"
    description = ("Alpha191 walk-forward 合成（12月训练窗+ICIR 加权+"
                   "9 因子去重集+风格中性，purge 21 交易日无前视；"
                   "48 个 OOS 月 ICIR 0.55，见 research/Alpha191深化检验.md）")
    category = "alpha191"

    def compute(self, store, start=None, end=None,
                universe: list[str] | None = None) -> pd.DataFrame:
        # 1) 宽表 + 公式现算（alpha 湖为静态快照，不可读）
        wide = build_wide(store, WIDE_START)
        if universe:
            keep = [c for c in wide["close"].columns if c in set(universe)]
            if not keep:
                return pd.DataFrame(columns=["date", "code", "value"])
            for k in wide:
                wide[k] = wide[k][keep]
        fvs = {}
        for n in A191:
            fn, _conf = ALPHAS[n]
            try:
                res = fn(wide)
            except Exception as e:
                raise RuntimeError(f"{n} 公式计算失败: {e}") from e
            long = _to_long(res)
            if len(long) < 50_000:
                raise RuntimeError(f"{n} 公式结果覆盖不足（{len(long)} 行）")
            fvs[n] = long

        # 2) 风格因子现算
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

        # 4) 交易日历（kline 全历史；不取 fwd 日期集——输出日不需要 fwd）
        days = (store.q("SELECT DISTINCT date FROM kline_daily "
                        "ORDER BY date")["date"]
                .dt.normalize().drop_duplicates().tolist())
        days = sorted(pd.to_datetime(d) for d in days)
        day_idx = {d: i for i, d in enumerate(days)}

        # 5) 中性化 + 逐日 IC
        pure, ic = {}, {}
        for n in A191:
            p = _neutralize(fvs[n], st)
            if p.empty:
                continue
            pure[n] = p
            ic[n] = _day_ic(p, fwd)
        if not pure:
            return pd.DataFrame(columns=["date", "code", "value"])

        # 6) 逐输出日：训练窗 → 方向/权重 → 合成
        z = {n: p.assign(z=p.groupby("date")["value"].transform(_zscore))
             for n, p in pure.items()}
        base = None
        for n in A191:
            if n not in z:
                continue
            v = z[n][["date", "code", "z"]].rename(columns={"z": n})
            base = v if base is None else base.merge(
                v, on=["date", "code"], how="inner")
        if base is None:
            return pd.DataFrame(columns=["date", "code", "value"])
        cols = [n for n in A191 if n in base.columns]

        out_days = sorted(d for d in base["date"].unique()
                          if d in day_idx and day_idx[d] >= PURGE_TD)
        W = {}
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

        bsub = base[base["date"].isin(W.keys())]
        Z = bsub[cols].to_numpy(dtype=float)
        uniq, inv = np.unique(bsub["date"].to_numpy(),
                              return_inverse=True)
        dirU = np.array([[W[pd.Timestamp(t)][0].get(n, 0.0) for n in cols]
                         for t in uniq])
        wU = np.array([[W[pd.Timestamp(t)][1].get(n, 0.0) for n in cols]
                       for t in uniq])
        dirM, wM = dirU[inv], wU[inv]
        num = np.nansum(Z * dirM * wM, axis=1)
        den = wM.sum(axis=1)
        comp = np.where(den > 0, num / np.where(den > 0, den, 1.0), np.nan)

        out = bsub[["date", "code"]].assign(value=comp)
        out = out.dropna(subset=["value"])
        return (out.sort_values(["date", "code"])
                   .reset_index(drop=True)[["date", "code", "value"]])
