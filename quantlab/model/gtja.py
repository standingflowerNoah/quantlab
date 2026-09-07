"""国泰君安风格多因子合成模型（L3 模型层）
=====================================
复刻国泰君安经典多因子选股的标准合成流程：

1. 单因子预处理：MAD 去极值 → 行业+市值中性化 → 截面 z-score；
2. 方向校正：按实测 IC 符号对因子取反（做多「未来收益更高」的一侧）；
3. 合成：z-score 加权求和（等权或 ICIR 加权），得到截面评分。

与 composite.py 的 rank 等权合成互补：本模块是国泰君安标准的
「去极值 + 中性化 + z-score + ICIR 加权」流程，更贴近券商研报范式。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import get_logger
from ..data.store import Store
from ..factor.preprocess import preprocess

log = get_logger(__name__)


def _neutralize_context(store: Store, neutralize: str):
    """返回 (industry_map, size_df)，供 preprocess 中性化使用"""
    industry_map: dict = {}
    size_df: pd.DataFrame | None = None
    if not neutralize:
        return industry_map, size_df
    ind = store.q(
        "SELECT code, industry FROM instruments WHERE industry IS NOT NULL")
    industry_map = dict(zip(ind["code"], ind["industry"]))
    if neutralize in ("size", "both"):
        size_df = store.read_factor("size")
        if size_df.empty:
            log.warning("size 因子缺失，市值中性化回退关闭")
            size_df = None
            if neutralize == "size":
                return industry_map, None
            neutralize = "industry"
    return industry_map, size_df


def build_gtja_score(factor_names: list[str],
                     directions: dict[str, int] | None = None,
                     universe: str | None = "ashare_ex",
                     neutralize: str = "both",
                     mad_n: float = 3.0,
                     icir_weights: dict[str, float] | None = None,
                     skip_neutralize: set[str] | None = None,
                     start=None, end=None) -> pd.DataFrame:
    """国泰君安风格多因子合成评分

    参数:
        factor_names: 因子名列表（需已 compute 落库）
        directions: {name: +1/-1} 方向（None 则全部 +1）
        universe: 股票池（None=全A / 池名 / 代码列表）
        neutralize: both/industry/size/None（中性化口径）
        mad_n: MAD 去极值倍数
        icir_weights: {name: w}（None=等权，权重为 0 的因子自动剔除）
        skip_neutralize: 跳过中性化的因子集合（如 size 本身作为市值暴露锚点）
    返回: date/code/score 长格式，score 为 z-score 量纲（越大越看多）
    """
    store = Store()
    industry_map, size_df = _neutralize_context(store, neutralize)
    skip_neutralize = skip_neutralize or set()
    if universe is not None:
        if isinstance(universe, str):
            from ..data.universe import get_universe
            universe = set(get_universe(universe))
        else:
            universe = set(universe)

    direction = directions or {n: 1 for n in factor_names}

    frames = []
    used = []
    for name in factor_names:
        fv = store.read_factor(name)
        if fv.empty:
            log.warning(f"因子 {name} 无数据，跳过")
            continue
        fv["date"] = pd.to_datetime(fv["date"])
        fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
        fv = fv[np.isfinite(fv["value"])]
        if fv.empty:
            continue
        if universe is not None:
            fv = fv[fv["code"].isin(universe)]

        # 国泰君安标准预处理：去极值 → 中性化 → z-score
        # size/total_mcap 作为市值暴露锚点跳过中性化（否则 alpha 被剥离）
        nmode = None if name in skip_neutralize else neutralize
        z = preprocess(fv[["date", "code", "value"]],
                       industry_map=industry_map,
                       size_df=size_df, mode=nmode, mad_n=mad_n)
        if z.empty:
            log.warning(f"因子 {name} 预处理后为空，跳过")
            continue
        z["value"] = z["value"] * direction.get(name, 1)
        z = z.rename(columns={"value": "z"})
        frames.append(z[["date", "code", "z"]])
        used.append(name)

    if not frames:
        raise RuntimeError("无可用的合成因子")

    # 加权：ICIR 权重或等权
    if icir_weights:
        wsum = sum(abs(w) for n, w in icir_weights.items() if n in used)
        if wsum > 0:
            for f, n in zip(frames, used):
                w = icir_weights.get(n, 0.0)
                if w != 0:
                    f["z"] = f["z"] * (w / wsum)
        else:
            log.warning("ICIR 权重全为 0，回退等权")

    allf = pd.concat(frames, ignore_index=True)
    score = allf.groupby(["date", "code"])["z"].sum().reset_index()
    score = score.rename(columns={"z": "score"})

    if start is not None:
        score = score[score["date"] >= pd.to_datetime(start)]
    if end is not None:
        score = score[score["date"] <= pd.to_datetime(end)]
    return score.sort_values(["date", "code"]).reset_index(drop=True)
