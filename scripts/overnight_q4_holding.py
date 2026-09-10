#!/usr/bin/env python3
"""Q4：持有期扫描 —— 摊薄换手成本后能否转正？
=================================================================
背景：Q3 合成打分（微盘+低流动性+低波）把隔夜段从 −4bp 拉到 +12.7bp/日，
      但 1 日全换手的 15bp 成本吃掉全部。若把持有期拉长到 H 日：
        毛收益/日 = 选中股票 H 日总收益 / H
        成本/日   = 15bp / H     （每 H 日一次往返）
      成本随 H 线性下降，毛收益取决于 H 日累计（含 H 个隔夜段 + H−1 个日内段）。
      这同时回答了"生产模型（20 日调仓、size+amihud）收益从哪来"。
      ⚠️ 同时报告【隔夜段单腿版】：每天收盘买、次日开盘卖再买回——不可行，
      故此处按"持有 H 日"（不日内进出）计算，成本按 H 日一次往返。

输出：reports/overnight/q4_holding.json
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SEG_GLOB = str(ROOT / "data/lake/factor/overnight/daily_segments_fsdb/*.parquet").replace("\\", "/")
SCORE = ROOT / "data/lake/factor/overnight/composite_score/score.parquet"
OUT = ROOT / "reports/overnight"
TOPN = 100
COST_BP = 15.0
TRADING_DAYS = 244.0
HOLDS = [1, 2, 5, 10, 20]
IS_END = pd.Timestamp("2024-12-31")


def main() -> None:
    con = duckdb.connect()
    con.execute("PRAGMA threads=6")
    print("[load] 合成打分 + 日度总收益 …")
    score = pd.read_parquet(SCORE)
    seg = con.execute(f"""
        SELECT code, date, total_ret FROM read_parquet('{SEG_GLOB}')
        WHERE in_ex AND NOT halted AND total_ret IS NOT NULL
    """).df()
    score = score.dropna(subset=["score"])
    score = score.merge(seg, on=["code", "date"], how="inner")
    print(f"  score rows={len(score):,}  日期 {score.date.min()} → {score.date.max()}")

    # 收益率宽表 → 累计对数收益（复权连续）；成交额宽表用于容量代理
    piv = seg.pivot(index="date", columns="code", values="total_ret").sort_index()
    amt_piv = (score.pivot(index="date", columns="code", values="amount")
               .reindex(index=piv.index, columns=piv.columns))
    r = piv.fillna(0.0).to_numpy()
    lr = np.log1p(np.clip(r, -0.9, 5.0))
    cum = np.cumsum(lr, axis=0)
    amt = amt_piv.to_numpy()
    dates = piv.index.to_numpy()
    codes = piv.columns.to_numpy()
    dpos = {d: i for i, d in enumerate(dates)}
    cpos = {c: i for i, c in enumerate(codes)}
    # 每个交易日选 top100
    sel_idx = {}
    for date, g in score.groupby("date"):
        if date not in dpos:
            continue
        top = g.nlargest(TOPN, "score")
        ii = [cpos[c] for c in top.code if c in cpos]
        if ii:
            sel_idx[dpos[date]] = np.array(ii)

    res = {}
    print(f"\n{'H(日)':>6} {'毛bp/日':>9} {'t':>6} {'OOS毛':>8} {'成本bp/日':>9} "
          f"{'净年化':>8} {'持仓中位成交额(万)':>16}")
    for H in HOLDS:
        daily, amts = [], []
        for i, ii in sel_idx.items():
            if i + H >= len(dates):
                continue
            fwd = np.expm1(cum[i + H, ii] - cum[i, ii])
            daily.append((dates[i], float(np.mean(fwd)) / H))
            amts.append(float(np.nanmedian(amt[i, ii])))
        s = pd.Series(dict(daily)).sort_index()
        st = s.dropna()
        mu, sd, n = st.mean(), st.std(ddof=1), len(st)
        oos = st[st.index > IS_END]
        cost_day = COST_BP / 1e4 / H
        net = (mu - cost_day) * TRADING_DAYS * 100
        res[f"H{H}"] = {
            "gross_bp_per_day": float(mu * 1e4),
            "t": float(mu / (sd / np.sqrt(n))),
            "oos_bp_per_day": float(oos.mean() * 1e4) if len(oos) else None,
            "cost_bp_per_day": COST_BP / H,
            "net_ann_pct": float(net),
            "median_amount_top100": float(np.nanmedian(amts)),
            "days": int(n),
        }
        print(f"{H:>6} {mu*1e4:>9.2f} {mu/(sd/np.sqrt(n)):>6.2f} "
              f"{(oos.mean()*1e4 if len(oos) else float('nan')):>8.2f} "
              f"{COST_BP/H:>9.2f} {net:>7.1f}% {np.nanmedian(amts)/1e4:>16,.0f}")

    # 分年度（H=5 与 H=20 两个代表关口）
    for H in [5, 20]:
        key = f"H{H}"
        daily = []
        for i, ii in sel_idx.items():
            if i + H >= len(dates):
                continue
            fwd = np.expm1(cum[i + H, ii] - cum[i, ii])
            daily.append((dates[i], float(np.mean(fwd)) / H))
        s = pd.Series(dict(daily)).sort_index()
        by_year = {}
        for y, g in s.groupby(pd.DatetimeIndex(s.index).year):
            by_year[str(y)] = {
                "gross_bp_per_day": float(g.mean() * 1e4),
                "net_ann_pct": float((g.mean() - COST_BP / 1e4 / H) * TRADING_DAYS * 100),
            }
        res[key]["by_year"] = by_year
        print(f"\n[H={H}] 分年度净年化: " + "  ".join(
            f"{y}:{v['net_ann_pct']:+.1f}%" for y, v in by_year.items()))

    (OUT / "q4_holding.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    print("\n[done] → reports/overnight/q4_holding.json")


if __name__ == "__main__":
    main()
