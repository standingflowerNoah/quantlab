"""纸面组合跟踪（L5 决策层）
=====================================
把每日流水线产生的目标持仓快照存入 DuckDB（signal_portfolio），
并基于快照序列逐日盯市计算纸面净值（paper NAV）。

这是与回测完全独立的**真实前向验证账本**：信号一旦产生即被记录，
之后的表现无论好坏都不可篡改，用于长期检验策略的实盘一致性。

口径说明：
- 流水线在早盘前 07:00 运行，信号日 = 最近数据交易日（昨日），
  该快照的前向收益从下一交易日开始累积
- 盯市方式与回测引擎一致：买入持有、权重随价格漂移、停牌沿用最后价
- 净值随每日流水线运行逐步累积（首期快照无前向收益，第 2 期起有曲线）
"""
from __future__ import annotations

import pandas as pd

from ..config import get_logger
from ..data.store import Store

log = get_logger(__name__)

SIGNAL_DDL = """
date DATE, code VARCHAR, name VARCHAR, weight DOUBLE,
PRIMARY KEY(date, code)
"""

# 多模型账本（2026-09-07）：生产账本 signal_portfolio 保持不动；
# 候选模型（如 sue_i 切换路线 PROD_SI / V3_SI）并行记账积累前向样本，
# 积累充分（≥60 交易日）后再裁决是否切换生产。
SIGNAL_MULTI_DDL = """
model VARCHAR, date DATE, code VARCHAR, name VARCHAR, weight DOUBLE,
PRIMARY KEY(model, date, code)
"""

TRACKED_MODELS = {
    "PROD": "生产：size+amihud_20（与 signal_portfolio 同口径）",
    "PROD_SI": "直加组：size+amihud_20+sue_i+overnight_mom_20",
    "V3_SI": "条件融合：0.6×rank(核心)+0.4×rank(sue_i+overnight)",
    "PROD_DUAL": "两块式：0.9×rank(核心)+0.1×rank(风格卫星块)（观察仓）",
    "EQ3": "核心+sue_i 等权（2026-09-07 第二轮修正的主切换候选："
           "overnight_mom 单腿全历史稀释被剔除）",
    "PROD_HF": "核心+hf_amihud_20（2026-09-07 第五轮：分钟流动性精化版，"
               "hf 系增量检验胜出者，观察仓）",
    "PROD_HFA": "核心+sue_i+hf_amihud_20（2026-09-07 第六轮：两个已验证增量"
                "合并，同窗 71.7%/2.79/-20.7%，月配对胜率 78.9% vs PROD，"
                "网格/滑点/容量全过，当前主切换候选）",
    "EQ3_HFA_ICW": "同 PROD_HFA 但 ICIR 加权（2026-09-07 第七轮：稳健变体，"
                   "同窗 67.9%/2.82/-17.0%，回测回撤最浅，2024 股灾回放最抗跌）",
    "PROD_HFA_W3": "PROD_HFA 因子不变，周三周度调仓卫星口径（2026-09-07 第九轮："
                   "星期效应 7 组合平均周三最优、周五最差，差异温和；"
                   "快照仅周三记录、盯市按周）",
}


def ensure_table(store: Store):
    store.ensure_table("signal_portfolio", SIGNAL_DDL)
    store.ensure_table("signal_portfolio_multi", SIGNAL_MULTI_DDL)


def record_signal(holdings: pd.DataFrame, date) -> int:
    """记录当日目标持仓快照（同日重跑幂等覆盖）"""
    store = Store()
    ensure_table(store)
    df = holdings[["code", "name", "weight"]].copy()
    df.insert(0, "date", pd.Timestamp(date).normalize())
    df["weight"] = df["weight"].astype(float)
    n = store.upsert(df, "signal_portfolio", ["date", "code"])
    store.set_watermark("signal_portfolio", pd.Timestamp(date).date())
    log.info(f"信号跟踪: 记录 {pd.Timestamp(date).date()} 快照 {n} 只")
    return n


def signal_dates() -> list:
    store = Store()
    df = store.q("SELECT DISTINCT date FROM signal_portfolio ORDER BY date")
    return [pd.Timestamp(d) for d in df["date"]]


def record_signal_multi(model: str, holdings: pd.DataFrame, date) -> int:
    """记录候选模型的当日目标持仓快照（同日重跑幂等覆盖）"""
    if model not in TRACKED_MODELS:
        raise ValueError(f"未登记的跟踪模型: {model}")
    store = Store()
    ensure_table(store)
    df = holdings[["code", "name", "weight"]].copy()
    df.insert(0, "model", model)
    df.insert(1, "date", pd.Timestamp(date).normalize())
    df["weight"] = df["weight"].astype(float)
    n = store.upsert(df, "signal_portfolio_multi",
                     ["model", "date", "code"])
    log.info(f"信号跟踪[{model}]: 记录 {pd.Timestamp(date).date()} 快照 {n} 只")
    return n


def multi_signal_dates(model: str) -> list:
    store = Store()
    df = store.q("SELECT DISTINCT date FROM signal_portfolio_multi "
                 "WHERE model=? ORDER BY date", [model])
    return [pd.Timestamp(d) for d in df["date"]]


def paper_nav_multi(model: str) -> pd.DataFrame:
    """候选模型纸面净值（盯市逻辑与 paper_nav 相同，读多模型账本）"""
    store = Store()
    try:
        sig = store.q("SELECT date, code, weight FROM signal_portfolio_multi "
                      "WHERE model=? ORDER BY date", [model])
    except Exception:
        return pd.DataFrame(columns=["date", "ret", "nav"])
    return _mark_to_market(sig)


def _mark_to_market(sig: pd.DataFrame) -> pd.DataFrame:
    """快照序列逐日盯市 → 日收益曲线（与 paper_nav 口径一致）"""
    if sig.empty or sig["date"].nunique() < 2:
        return pd.DataFrame(columns=["date", "ret", "nav"])
    sig["date"] = pd.to_datetime(sig["date"])
    dates = sorted(sig["date"].unique())

    px = store_q_prices()
    if px.empty:
        return pd.DataFrame(columns=["date", "ret", "nav"])
    pmat = px.pivot(index="date", columns="code", values="c").sort_index()

    rows = []
    for i in range(len(dates) - 1):
        d0, d1 = dates[i], dates[i + 1]
        if d0 not in pmat.index or d1 not in pmat.index:
            continue
        hold = sig[sig["date"] == d0]
        codes = hold["code"].tolist()
        w = (hold.set_index("code")["weight"]
             .reindex(codes).fillna(0.0).values)
        seg = pmat.loc[d0:d1, codes].ffill()
        p0row = seg.iloc[0]
        usable = [j for j, c in enumerate(codes)
                  if pd.notna(p0row.get(c)) and p0row[c] > 0]
        if not usable:
            continue
        wu = w[usable]
        wu = wu / wu.sum()
        cols = [codes[j] for j in usable]
        vals = (1 + seg[cols].pct_change().fillna(0)).cumprod()
        port = (vals * wu).sum(axis=1)
        day = port.pct_change().iloc[1:]          # d0 收盘建仓，次日起计收益
        for t, r in day.items():
            rows.append({"date": t, "ret": float(r)})

    if not rows:
        return pd.DataFrame(columns=["date", "ret", "nav"])
    curve = pd.DataFrame(rows).drop_duplicates(subset="date", keep="last")
    curve = curve.sort_values("date").reset_index(drop=True)
    curve["nav"] = (1 + curve["ret"]).cumprod()
    return curve


def store_q_prices() -> pd.DataFrame:
    """后复权价格宽表原料（独立函数便于 paper_nav_multi 复用）"""
    store = Store()
    return store.q("SELECT date, code, close*adj_factor AS c FROM kline_daily")


def paper_nav() -> pd.DataFrame:
    """按快照序列逐日盯市，计算纸面净值曲线（date/ret/nav）

    相邻两个快照日之间按前一快照的持仓逐日盯市（期间不调仓）。
    快照不足 2 期或无已实现前向收益时返回空表。
    """
    store = Store()
    try:
        sig = store.q(
            "SELECT date, code, weight FROM signal_portfolio ORDER BY date")
    except Exception:
        return pd.DataFrame(columns=["date", "ret", "nav"])
    if sig.empty or sig["date"].nunique() < 2:
        return pd.DataFrame(columns=["date", "ret", "nav"])
    sig["date"] = pd.to_datetime(sig["date"])
    dates = sorted(sig["date"].unique())

    px = store.q("SELECT date, code, close*adj_factor AS c FROM kline_daily")
    px["date"] = pd.to_datetime(px["date"])
    pmat = px.pivot(index="date", columns="code", values="c").sort_index()

    rows = []
    for i in range(len(dates) - 1):
        d0, d1 = dates[i], dates[i + 1]
        if d0 not in pmat.index or d1 not in pmat.index:
            continue
        hold = sig[sig["date"] == d0]
        codes = hold["code"].tolist()
        w = (hold.set_index("code")["weight"]
             .reindex(codes).fillna(0.0).values)
        seg = pmat.loc[d0:d1, codes].ffill()
        p0row = seg.iloc[0]
        usable = [j for j, c in enumerate(codes)
                  if pd.notna(p0row.get(c)) and p0row[c] > 0]
        if not usable:
            continue
        wu = w[usable]
        wu = wu / wu.sum()
        cols = [codes[j] for j in usable]
        vals = (1 + seg[cols].pct_change().fillna(0)).cumprod()
        port = (vals * wu).sum(axis=1)
        day = port.pct_change().iloc[1:]          # d0 收盘建仓，次日起计收益
        for t, r in day.items():
            rows.append({"date": t, "ret": float(r)})

    if not rows:
        return pd.DataFrame(columns=["date", "ret", "nav"])
    curve = pd.DataFrame(rows).drop_duplicates(subset="date", keep="last")
    curve = curve.sort_values("date").reset_index(drop=True)
    curve["nav"] = (1 + curve["ret"]).cumprod()
    return curve


def paper_summary(curve: pd.DataFrame) -> dict | None:
    """纸面组合摘要（快照期数 / 已实现天数 / 累计收益）"""
    n_snap = len(signal_dates())
    if curve.empty:
        return {"n_snapshots": n_snap, "n_days": 0, "cum_return": None}
    return {
        "n_snapshots": n_snap,
        "n_days": len(curve),
        "cum_return": float(curve["nav"].iloc[-1] - 1),
        "start": str(curve["date"].iloc[0].date()),
        "end": str(curve["date"].iloc[-1].date()),
    }
