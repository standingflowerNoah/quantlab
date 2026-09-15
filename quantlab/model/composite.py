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
    # —— 2026-09-07 高频分钟因子（方向=实测 IC20 符号，2025-01~2026-09 窗口；|IC| 选样负向翻转）——
    "hf_rsk_20": -1,             # 已实现风险（IC20 −0.081/ICIR −0.65，低风险溢价）
    "hf_dsem_20": 1,             # 下行半变差动量（IC20 +0.093/ICIR 0.60）
    "hf_amihud_20": 1,           # 分钟 Amihud（IC20 +0.098；ρ=0.86 vs amihud_20 冗余）
    "hf_topvr_20": -1,           # 高量占比（IC20 −0.083/ICIR −0.59）
    "hf_corr_rv_20": -1,         # 收益-已实现波动相关（IC20 −0.067/ICIR −0.57）
    "hf_amtrange_20": -1,        # 分钟振幅（IC20 −0.066/ICIR −0.55）
    "hf_rlast30_20": -1,         # 尾盘 30 分钟收益（IC20 −0.029）
    "hf_vopen_20": -1,           # 开盘量占比（IC20 −0.055）
    "hf_vampp_20": -1,           # 量价背离（IC20 −0.052）
    "hf_rfirst30_20": -1,        # 开盘 30 分钟收益（IC20 −0.062）
    "hf_rvvol_20": -1,           # 已实现波动（IC20 −0.030）
    "hf_rku_20": -1,             # 分钟峰度（IC20 −0.049）
    "hf_smartq_10": -1, "hf_smartq_20": -1,   # 聪明钱（IC20 −0.040/−0.042）
    "hf_topvupr_20": -1,         # 高量上涨占比（IC20 −0.052）
    "hf_rv_20": -1,              # 已实现方差（IC20 −0.060）
    "hf_vwapbias_20": -1,        # 分钟 VWAP 乖离（IC20 −0.036）
    "hf_hipos_20": -1,           # 高位运行占比（IC20 −0.027）
    "hf_rjv_20": 1,              # 已实现跳跃方差（IC20 +0.049）
    "hf_vclose_20": 1,           # 收盘量占比（IC20 +0.021）
    "hf_vhhi_20": 1,             # 成交量 HHI（IC20 +0.020）
    "hf_wf_composite": 1,        # WF 合成因子（内部已定号）
    # —— 2026-09-14 资金流量因子（tushare moneyflow，方向=实测 IC20 符号）——
    "mf_main_pct_20": -1,        # 主力净流入占比：IC20 −0.025/ICIR −0.44（拉高出货，
                                 # 五年 4 负 1 零），主力持续净流入→后续跌
    "mf_small_pct_20": 1,        # 小单占比：IC 归零（+0.005），无显著方向，留档
    "mf_net_pct_20": 1,          # 全单净流入：IC 归零（−0.010 近零），留档
    "mf_smart_dumb_20": -1,      # 智钱-散户分歧：IC20 −0.016/ICIR −0.24，与 mf_main
                                 # 冗余（corr 0.94），留档
    "mf_main_chg_20": 1,         # 主力流入边际变化：IC 近零（−0.006），留档
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


def factor_coverage(factor_names: list[str],
                    asof=None,
                    min_ratio: float = 0.6) -> pd.DataFrame:
    """因子水位与覆盖度体检（防 NaN-skip 静默降级——2026-09-07 hf 事故防线）

    对每个因子直扫因子湖 parquet（不依赖 DuckDB 视图，写锁安全）：
      - f_max: 因子最新日期；n_max: 最新日覆盖股票数；n_prev: 前一交易日覆盖数
      - ok = (f_max 不落后 asof 超过 1 个自然日) 且 (n_max >= min_ratio * n_prev)

    参数:
        factor_names: 因子名列表
        asof: 参照日期（默认取 kline_daily 最新交易日）
        min_ratio: 覆盖度相对前一交易日的下限（防半写）
    返回: factor/f_max/n_max/n_prev/behind_days/ok 明细表
    """
    import duckdb
    from .. import config

    store = Store()
    if asof is None:
        asof = store.q("SELECT max(date) AS d FROM kline_daily")["d"][0]
    asof = pd.Timestamp(asof)
    rows = []
    con = duckdb.connect()
    try:
        for name in factor_names:
            pat = str(config.FACTOR_DIR / name / "part-*.parquet").replace("\\", "/")
            try:
                r = con.execute(f"""
                    WITH t AS (SELECT CAST(date AS DATE) AS d, code
                               FROM read_parquet('{pat}')),
                    mx AS (SELECT max(d) AS m FROM t)
                    SELECT (SELECT m FROM mx) AS f_max,
                           (SELECT count(DISTINCT code) FROM t WHERE d = (SELECT m FROM mx)) AS n_max,
                           (SELECT count(DISTINCT code) FROM t
                            WHERE d = (SELECT max(d) FROM t WHERE d < (SELECT m FROM mx))) AS n_prev
                """).fetchone()
            except Exception:
                r = (None, 0, 0)
            f_max, n_max, n_prev = r
            f_max = pd.Timestamp(f_max) if f_max is not None else None
            behind = (asof - f_max).days if f_max is not None else 999
            ratio = (n_max / n_prev) if n_prev else 0.0
            rows.append({"factor": name, "f_max": f_max, "n_max": int(n_max or 0),
                         "n_prev": int(n_prev or 0), "behind_days": behind,
                         "ok": behind <= 1 and ratio >= min_ratio})
    finally:
        con.close()
    return pd.DataFrame(rows)


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
