#!/usr/bin/env python3
"""生产模型重构 · 第六步：EQ3 容量与可成交性压力测试
=====================================
切换前最后一道非历史检验（上轮遗留方向）：
  1. 持仓流动性画像：各候选模型最新目标持仓的 ADV（20 日均成交额）与流通市值分布
  2. 容量曲线：单股持仓/ADV ≤10% 红线下的最大容纳资金（ADV 法）
  3. 冲击成本调整：平方根冲击模型下，给定资金规模的年费用拖累对比
  4. 披露季换手集中度：sue_i 事件是否让 EQ3 调仓换手在财报月（1/4/8/10月）尖峰化
输出: reports/prodmodel/capacity.json
"""
from __future__ import annotations

import json
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "reports/prodmodel"
MODELS = ["PROD", "PROD_SI", "PROD_DUAL", "EQ3"]
ADV_REDLINE = 0.10          # 单股持仓 / ADV 红线 10%
BASE_IMPACT_BP = 10.0       # 成本模型基础滑点 10bp（参与率 ~5% 量级假设）


def latest_holdings(store) -> dict[str, pd.DataFrame]:
    df = store.q("""SELECT model, date, code, weight FROM signal_portfolio_multi
                    WHERE date = (SELECT MAX(date) FROM signal_portfolio_multi)""")
    out = {}
    for m, g in df.groupby("model"):
        out[m] = g[["code", "weight"]]
    return out


def liq_profile(store, holds: pd.DataFrame) -> pd.DataFrame:
    codes = ",".join(f"'{c}'" for c in holds["code"])
    adv = store.q(f"""
        SELECT code, AVG(amount) AS adv
        FROM (SELECT code, date, amount FROM kline_daily
              WHERE code IN ({codes}) ORDER BY date DESC
              LIMIT {len(holds) * 20}) GROUP BY code""")
    m = holds.merge(adv, on="code", how="left")
    snap = store.q(f"""SELECT s.code, s.float_mcap_yi FROM daily_snapshot s
                       JOIN (SELECT MAX(date) d FROM daily_snapshot) t
                         ON s.date = t.d AND s.code IN ({codes})""")
    return m.merge(snap, on="code", how="left")


def capacity(m: pd.DataFrame, n: int) -> dict:
    adv_yi = (m["adv"] / 1e8).replace(0, np.nan).dropna()
    cap_q05 = float(adv_yi.quantile(0.05)) * ADV_REDLINE * n * 1e8
    cap_med = float(adv_yi.median()) * ADV_REDLINE * n * 1e8
    caps = {}
    for aum in (3e6, 5e6, 1e7, 2e7, 5e7):
        pos = aum / n
        ratio = pos / (adv_yi * 1e8)
        caps[f"{aum/1e4:.0f}万"] = {
            "p50": round(float(ratio.median()), 5),
            "p90": round(float(ratio.quantile(0.9)), 5),
            "n_over10": int((ratio > ADV_REDLINE).sum()),
        }
    return {"cap_max_5pct_yi": round(cap_q05 / 1e8, 3),
            "cap_max_median_yi": round(cap_med / 1e8, 3),
            "adv_p10_yi": round(float(adv_yi.quantile(0.1)), 3),
            "adv_med_yi": round(float(adv_yi.median()), 3),
            "float_mcap_p10_yi": (round(float(m["float_mcap_yi"].quantile(0.1)), 3)
                                  if m["float_mcap_yi"].notna().any() else None),
            "by_aum": caps}


def impact_drag(m: pd.DataFrame, aum: float, n: int,
                annual_turnover: float) -> dict:
    """平方根冲击：impact_bp = 10bp × sqrt(part / 10%)；
    年拖累 = 年换手 × Σw×(固定成本 + 冲击)（单边×2 近似已含于换手口径：
    run_optimized_backtest 的 turnover 是 L1/2 单边，费用按双边口径乘 2）"""
    adv = (m["adv"] / 1e8).replace(0, np.nan)
    pos = aum / n
    part = ((pos / (adv * 1e8)) / ADV_REDLINE).clip(lower=1e-4)
    w = m["weight"].fillna(1.0 / n) if m["weight"].notna().all() else None
    if w is None:
        w = pd.Series(1.0 / n, index=m.index)
    impact_bp = BASE_IMPACT_BP * np.sqrt(part)
    fixed_bp = 35.0                     # 佣金×2+印花税+滑点 ≈ 35bp 双边
    per_side_bp = float((w / w.sum() * (fixed_bp + impact_bp)).sum())
    drag = per_side_bp / 1e4 * 2 * annual_turnover
    return {"aum_yi": round(aum / 1e8, 3),
            "per_side_bp": round(per_side_bp, 1),
            "annual_drag_pct": round(drag * 100, 2)}


def main():
    t0 = time.time()
    from quantlab.data.store import Store
    store = Store()
    holds = latest_holdings(store)
    res = {"profiles": {}, "capacity": {}, "impact": {}, "turnover_monthly": {}}

    annual_turn = {"PROD": 4.9, "PROD_SI": 7.5, "PROD_DUAL": 4.9, "EQ3": 7.5}
    # 年换手 ≈ 平均单期换手 × 12（20日调仓年 ~12 期）；
    # EQ3/PROD_SI 用 si_deep 实测 0.62/期，PROD/PROD_DUAL 用 0.41/0.30×~16 期折算
    annual_turn = {"PROD": 0.236 * 12, "PROD_SI": 0.622 * 12,
                   "PROD_DUAL": 0.2447 * 12, "EQ3": 0.617 * 12}

    for mname in MODELS:
        if mname not in holds:
            continue
        prof = liq_profile(store, holds[mname])
        n = len(prof)
        res["profiles"][mname] = {
            "n": n,
            "adv_med_yi": round(float((prof["adv"] / 1e8).median()), 3),
            "adv_p10_yi": round(float((prof["adv"] / 1e8).quantile(0.1)), 3),
            "adv_p90_yi": round(float((prof["adv"] / 1e8).quantile(0.9)), 3),
        }
        res["capacity"][mname] = capacity(prof, n)
        res["impact"][mname] = {
            f"{a/1e4:.0f}万": impact_drag(prof, a, n, annual_turn[mname])
            for a in (3e6, 1e7, 5e7)}
        print(f"{mname}: ADV中位 {res['profiles'][mname]['adv_med_yi']} 亿, "
              f"容量(5%线) {res['capacity'][mname]['cap_max_5pct_yi']} 亿 "
              f"({time.time()-t0:.0f}s)")

    # 披露季换手集中度：top100 名单在调仓日的更替率（月均），2025-05+
    from quantlab.model import build_composite
    for mname, factors in (("PROD", ["size", "amihud_20"]),
                           ("EQ3", ["size", "amihud_20", "sue_i"]),
                           ("PROD_SI", ["size", "amihud_20", "sue_i",
                                        "overnight_mom_20"])):
        try:
            sc = build_composite(factors, universe="ashare_ex",
                                 start="2025-05-01")
        except Exception as e:
            print(f"{mname} score failed: {e}")
            continue
        dates = sorted(pd.to_datetime(sc["date"].unique()))
        rb = dates[::20]
        tops = []
        for d in rb:
            s = sc[sc["date"] == d].nlargest(100, "score")["code"]
            tops.append(set(s))
        tos = [1 - len(tops[i] & tops[i - 1]) / 100
               for i in range(1, len(tops))]
        mm = pd.DataFrame({"date": rb[1:], "to": tos})
        mm["month"] = pd.to_datetime(mm["date"]).dt.month
        monthly = mm.groupby("month")["to"].mean()
        res["turnover_monthly"][mname] = {
            "by_month": {int(k): round(float(v), 3)
                         for k, v in monthly.items()},
            "disclosure_months_mean": round(float(
                monthly[monthly.index.isin([1, 4, 8, 10])].mean()), 3),
            "other_months_mean": round(float(
                monthly[~monthly.index.isin([1, 4, 8, 10])].mean()), 3),
        }
        print(f"{mname} 换手集中度: 披露月 {res['turnover_monthly'][mname]['disclosure_months_mean']}"
              f" vs 其他月 {res['turnover_monthly'][mname]['other_months_mean']} "
              f"({time.time()-t0:.0f}s)")

    (OUT_DIR / "capacity.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"done {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
