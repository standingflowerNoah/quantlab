"""多因子合成（L3 模型层）
=====================================
把多个已证有效的因子合成一个截面评分：
1. rank 标准化：每日截面因子值排名，归一化到 [0,1]（稳健，抗异常值）
2. 方向校正：负 IC 因子取反（如 size/turnover，做多低值）
3. 等权合成：对每个股票取「有值因子」的均值（可选因子逻辑）

方向表 FACTOR_DIRECTION 依据实测 IC 符号（见 factor 层试跑报告）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import get_logger
from ..data.store import Store

log = get_logger(__name__)

# 因子方向：+1 = 做多高因子值（正 IC），-1 = 做多低因子值（负 IC）
FACTOR_DIRECTION = {
    "reversal_5": 1, "reversal_10": 1,
    "size": -1, "total_mcap": -1, "turnover": -1,
    "volatility_20": -1, "volatility_60": -1,
    "momentum_20": -1, "momentum_60": -1, "momentum_120": -1,
    "rsi_14": -1,
    "amplitude_20": -1, "max_return_20": -1, "price_position_250": -1,
    "roe": 1, "op_margin": 1, "debt_ratio": -1, "ocf_ratio": 1,
    # —— 国泰君安经典因子（实测 IC 方向 2022-01~2026-09）——
    "amihud_20": 1,               # 低流动性溢价（正 IC）
    "turnover_std_20": -1,        # 换手率波动（投机）
    "avg_amount_20": -1,          # 成交额规模（低成交额溢价）
    "amount_std_20": -1,          # 成交额波动（流动性稳定性）
    "downside_volatility_20": -1,  # 低下行波动溢价
    "skewness_20": -1,            # 正偏=彩票股，做多低偏度
    # —— 事件因子（2026-09-05 前视审计修复窗口方向反转 bug 后重测）——
    "dragon_net_20": -1,          # 龙虎榜净买入：修复后为负 IC（ICIR20 -1.14），
                                  # 上榜后均值回归；旧 +2.3 系前视虚高，勿按 +1 用
    "block_premium_20": 1,        # 大宗折溢价：修复后 IC 归零（|IC|<0.01），无显著方向
    "lockup_pressure_60": -1,     # 解禁压力（负 IC，事前公告方向合法）
    # —— 财务截面因子（方向为经济含义常识，未 IC 实证：单期数据）——
    "ep": 1, "bp": 1, "sp": 1, "ocfp": 1,   # 估值（低估值高 EP/BP/SP/OCFP）
    "roa": 1, "net_margin": 1, "asset_turnover": 1,  # 质量
    "current_ratio": 1, "quick_ratio": 1,   # 杠杆/偿债
    "revenue_growth_yoy": 1, "profit_growth_yoy": 1, "asset_growth_yoy": 1,  # 成长
    # —— 2026-09-06 第一/二批挖掘胜出因子（方向为实测 IC 符号）——
    "sue": 1,                    # PEAD：高预期外盈利 → 高未来收益（IC20 +0.031）
    "overnight_mom_20": 1,       # 隔夜动量（IC20 +0.036，五年全正）
    "chip_vwap_bias_250": -1,    # 价格相对筹码成本乖离（IC20 −0.073，低乖离溢价）
    # —— 2026-09-07 第三批挖掘胜出因子（方向为实测 IC 符号）——
    "ev_high_vol_20": -1,        # 高位放量事件计数（IC20 −0.063，五年全负）
    "ep_size_dom": 1,            # EP 市值分域版（IC20 +0.030，域内重排 ρ=0.97 vs ep）
    # —— 2026-09-07 第四批 B1 股东户数（方向为实测 IC 符号）——
    "holder_num_chg": -1,        # 户数环比（IC20 −0.020，五年 4 负 1 近零）
    "holder_num_chg_2": -1,      # 两期累计（IC20 −0.028/ICIR −0.52，五年全负）
    # —— 2026-09-07 第五批 SUE_I 改进（方向为实测 IC 符号）——
    "sue_pred": 1,               # 预告 SUE（IC20 +0.042，比财报早 1-3 月）
    "sue_i": 1,                  # SUE_I 融合版（IC20 +0.044/ICIR 0.70，同位替换 sue 候选）
    # —— 2026-09-07 开放式挖掘（方向为实测 IC 符号；均留库观察）——
    "epd_60": 1,                 # EP 时序偏离（IC20 +0.031 五年全正，与截面 ep 正交 ρ=0.14）
    "epds_60": 1,                # EPD 持续性（IC20 +0.033 五年全正）
    "turn_idio_20": -1,          # 特异换手（IC20 −0.083 五年全负；ρ=0.87 vs turnover 留 turnover）
    "inst_buy_20": 1,            # 机构席位净买入（h5 +0.034 五年全正；覆盖=上榜股，事件池内信号）
    "retail_buy_20": -1,         # 散户通道（拉萨系）净买入（IC20 −0.153/ICIR −0.97 五年全负，全部正交；事件池排除项）
}


def _load_ranked(name: str, store: Store) -> pd.DataFrame:
    """读因子并按每日截面 rank 标准化到 [0,1]（含方向校正）"""
    fv = store.read_factor(name)
    if fv.empty:
        return pd.DataFrame(columns=["date", "code", "r"])
    fv["date"] = pd.to_datetime(fv["date"])
    fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
    fv = fv[np.isfinite(fv["value"])]
    direction = FACTOR_DIRECTION.get(name, 1)
    fv["r"] = fv.groupby("date")["value"].rank(pct=True) * direction
    return fv[["date", "code", "r"]]


def _audit_gate(names: list[str]):
    """审查闸门：合成引用了审查 FAIL 的因子时告警（治理闭环）。

    审查记录由构建即审查机制维护（data/lake/factor_audit/<name>.json）。
    FAIL = PIT 穿越自检不过/重复行/inf——该因子值不可信，不得静默进入合成。
    """
    import json
    from pathlib import Path
    audit_dir = Path("data/lake/factor_audit")
    for n in names:
        p = audit_dir / f"{n}.json"
        if not p.exists():
            log.warning(f"[审查闸门] 因子 {n} 无审查记录（未触发构建审查？）")
            continue
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        v = rec.get("verdict")
        if v == "FAIL":
            raise RuntimeError(
                f"[审查闸门] 因子 {n} 审查 FAIL（{rec.get('issues')}），"
                f"拒绝进入合成——先整改并复核")
        if v == "WARN":
            log.warning(f"[审查闸门] 因子 {n} 带 WARN 进入合成: "
                        f"{'; '.join(rec.get('issues', []))}")


def build_composite(factor_names: list[str], start=None, end=None,
                    universe=None, method: str = "equal",
                    neutralize: str | None = None) -> pd.DataFrame:
    """合成多因子评分

    参数:
        factor_names: 因子名列表（需已 compute 落库）
        universe: 股票池（None=全A / 池名 / 代码列表）
        method: equal（等权，默认）
        neutralize: None=不中性化 / 'industry'=行业 / 'size'=市值 / 'both'
    返回: date/code/score 长格式，score ∈ [0,1]（1 最看多）
    """
    _audit_gate(factor_names)
    store = Store()
    if universe is not None:
        if isinstance(universe, str):
            from ..data.universe import get_universe
            universe = set(get_universe(universe))
        else:
            universe = set(universe)

    industry_map: dict = {}
    size_df: pd.DataFrame | None = None
    if neutralize:
        ind = store.q(
            "SELECT code, industry FROM instruments WHERE industry IS NOT NULL")
        industry_map = dict(zip(ind["code"], ind["industry"]))
        size_df = store.read_factor("size")
        if size_df.empty:
            neutralize = None
            log.warning("size 因子缺失，中性化回退为关闭")

    frames = []
    used_names = []
    for name in factor_names:
        if neutralize and name != "size":
            from ..factor.neutralize import neutralize_section
            fv = store.read_factor(name)
            if fv.empty:
                log.warning(f"因子 {name} 无数据，跳过")
                continue
            fv = neutralize_section(fv, industry_map, size_df, mode=neutralize)
            if fv.empty:
                continue
            fv["date"] = pd.to_datetime(fv["date"])
            direction = FACTOR_DIRECTION.get(name, 1)
            fv["r"] = fv.groupby("date")["value"].rank(
                pct=True) * direction
            f = fv[["date", "code", "r"]]
        else:
            f = _load_ranked(name, store)
        if f.empty:
            log.warning(f"因子 {name} 无数据，跳过")
            continue
        if universe is not None:
            f = f[f["code"].isin(universe)]
        frames.append(f)
        used_names.append(name)
    if not frames:
        raise RuntimeError("无可用的合成因子")

    # IC 加权：按因子 ICIR 分配权重（不足则回退等权）
    weighted = False
    if method == "ic_weighted":
        from ..factor.quality import factor_icir_weights
        w = factor_icir_weights(used_names, store=store)
        total = sum(w.values())
        if total <= 0:
            log.warning("ICIR 权重全为 0，回退等权")
        else:
            weighted = True
            for i in range(len(frames)):
                frames[i] = frames[i].copy()
                frames[i]["r"] = frames[i]["r"] * (w[used_names[i]] / total)

    all_f = pd.concat(frames, ignore_index=True)
    if weighted:
        score = all_f.groupby(["date", "code"])["r"].sum().reset_index()
    else:
        score = all_f.groupby(["date", "code"])["r"].mean().reset_index()
    score = score.rename(columns={"r": "score"})

    if start is not None:
        score = score[score["date"] >= pd.to_datetime(start)]
    if end is not None:
        score = score[score["date"] <= pd.to_datetime(end)]
    return score.sort_values(["date", "code"]).reset_index(drop=True)
