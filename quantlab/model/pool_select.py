"""池内精选分层选股（L3 模型层）
=====================================
两段式选股：基准评分选出候选池（pool_n 只），精选因子在池内二次排序，
输出 date/code/score（score = 池内精选排名 pct），可直接接入
generate_target / run_backtest / run_optimized_backtest。

适用场景：精选因子信号极强但可成交性存疑（如 dragon_net_20 打板型信号），
用流动性可接受的基准池约束后提取其独立择股维度。
"""
from __future__ import annotations

import pandas as pd

from ..config import get_logger

log = get_logger(__name__)


def build_pool_refined_score(base_score: pd.DataFrame,
                             refine_score: pd.DataFrame,
                             pool_n: int = 400) -> pd.DataFrame:
    """池内精选评分

    参数:
        base_score: date/code/score 基准评分（选候选池）
        refine_score: date/code/score 精选评分（池内二次排序）
        pool_n: 候选池规模
    返回: date/code/score（score=池内精选排名 pct；未入池=0）
    """
    base = base_score.copy()
    base["date"] = pd.to_datetime(base["date"])
    ref = refine_score.copy()
    ref["date"] = pd.to_datetime(ref["date"])

    rows = []
    for d, g in base.groupby("date"):
        pool = g.nlargest(pool_n, "score")["code"]
        gr = ref[ref["date"] == d].set_index("code")["score"]
        ranks = gr.reindex(pool).dropna().rank(pct=True)
        full = pd.Series(0.0, index=g["code"].values)
        full.loc[ranks.index] = ranks.values
        rows.extend((d, code, v) for code, v in full.items())

    out = pd.DataFrame(rows, columns=["date", "code", "score"])
    return out.sort_values(["date", "code"]).reset_index(drop=True)


def build_production_score(universe: str = "ashare_ex",
                           pool_n: int = 400) -> pd.DataFrame:
    """生产选股模型（唯一入口，pipeline 与 cli 共用）

    **size + amihud_20**（小市值 + 低流动性溢价，rank 等权）。
    2026-09-05 前视审计发现 dragon_net_20 因子 SQL 窗口方向反了
    （t 日值含未来事件，IC 虚高至 2.3），池内精选模型因此作废回退。
    修复后 dragon_net_20 实测为负向信号（IC20 -0.061, ICIR -1.14，
    上榜后均值回归），方向已更新入 FACTOR_DIRECTION，留作观察，
    不再进入生产精选。
    """
    from . import build_composite
    return build_composite(["size", "amihud_20"], universe=universe)


def build_dual_score(universe: str = "ashare_ex", w_sat: float = 0.1,
                     sats: list[str] | None = None) -> pd.DataFrame:
    """两块式候选模型（PROD_DUAL，观察仓）

    (1-w)·rank(核心 size+amihud_20) + w·rank(卫星块)。
    卫星块默认 [max_return_20, amount_std_20, ev_high_vol_20,
    turnover_std_20]（2026-09-07 全池评估：与核心正交、ICIR 0.42-0.95）。
    全期回测略逊纯核心（年化 35.4% vs 36.9%），但 IC 逐年更稳
    （ICIR 0.47 vs 0.42）、2024 弱年收益更高——观察仓积累前向样本。
    """
    from . import build_composite
    if sats is None:
        sats = ["max_return_20", "amount_std_20", "ev_high_vol_20",
                "turnover_std_20"]
    rc = build_composite(["size", "amihud_20"], universe=universe)
    rs = build_composite(sats, universe=universe)
    m = rc.rename(columns={"score": "rc"}).merge(
        rs.rename(columns={"score": "rs"}), on=["date", "code"], how="inner")
    m["score"] = (1 - w_sat) * m["rc"] + w_sat * m["rs"]
    return m[["date", "code", "score"]]
