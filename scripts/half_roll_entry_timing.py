r"""滚动半仓法 —— 「早盘」时点敏感性
================================================================
半仓滚动结构不变，只把「早盘买入」从开盘换成盘中不同分钟 m：
    进场那一半在 T 日 m 分钟买入 → 仍持有到 T+1 收盘
    出场那一半仍是 T-1 日开盘买入 → T 日收盘卖出（全天在场）

组合日收益：
    r_t(m) = 0.5*[(1+g_{t-1})(1+i_t) - 1] + 0.5*ri_m(t)
        ri_m(t) = close_t/close_m - 1   （进场那一半在 T 日剩下的日内收益）
成本不变（每天买 50% + 卖 50%）：0.5*rt

数据：cell_main.parquet（2025+ 分钟层库内聚合，来自 intraday_entry_timing.py）
      + data/lake/factor/overnight/half_roll/daily_v2.parquet（PIT 日序列）
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
REP = ROOT / "reports/overnight"
PER_YEAR = 244.0

KEY_MODS = {569: "09:25开盘", 571: "09:31", 575: "09:35", 585: "09:45", 600: "10:00",
            615: "10:15", 630: "10:30", 660: "11:00", 690: "11:30",
            781: "13:01", 810: "13:30", 850: "14:10", 875: "14:35",
            895: "14:55", 896: "14:56", 897: "14:57", 900: "15:00收盘"}


def stat(x, cost_day_bp):
    x = pd.Series(x).dropna()
    if len(x) < 10:
        return {}
    n = len(x); yrs = n / PER_YEAR
    nav = (1 + x).cumprod()
    dd = float((nav / nav.cummax() - 1).min())
    return {"days": n,
            "gross_cagr": round((float(nav.iloc[-1]) ** (1/yrs) - 1)*100, 1),
            "net_cagr": round(((float(nav.iloc[-1]) - 0) ** (1/yrs) - 1)*100, 1),
            "mean_bp": round(float(x.mean())*1e4, 2),
            "t": round(float(x.mean()/(x.std(ddof=1)/np.sqrt(n))), 2),
            "maxdd": round(dd*100, 1)}


def main():
    day = pd.read_parquet(ROOT / "data/lake/factor/overnight/half_roll/daily_v2.parquet")
    day["d"] = pd.to_datetime(day.index)
    day = day.set_index("d").sort_index()
    cell = pd.read_parquet(ROOT / "data/lake/factor/overnight/entry_timing/cell_main.parquet")
    cell = cell[~cell.buy_lim]
    cell["d"] = pd.to_datetime(cell["d"])

    # 分钟层剩余日内收益 ri_m = close_T/close_m - 1
    m = cell.groupby(["d", "mod"]).agg(n=("n", "sum"), s=("s_intra", "sum")).reset_index()
    m["ri"] = m["s"] / m["n"]
    piv = m.pivot(index="d", columns="mod", values="ri")

    i = day["i0"].reindex(piv.index)
    gl = day["g"].shift(1).reindex(piv.index)
    exiting = 0.5 * ((1 + gl.fillna(0)) * (1 + i) - 1)     # 出场那一半

    rt = 15.0
    rows = []
    for mod, label in KEY_MODS.items():
        if mod not in piv.columns:
            continue
        ri = piv[mod]
        r = exiting + 0.5 * ri
        r = r.dropna()
        gross = (1 + r).cumprod().iloc[-1]
        net = (1 + r - rt/2/1e4).cumprod().iloc[-1]
        yrs = len(r) / PER_YEAR
        rows.append({"hm": label, "mod": mod, "days": len(r),
                     "ri_bp": round(float(ri.mean())*1e4, 2),
                     "gross_cagr_pct": round((gross**(1/yrs)-1)*100, 1),
                     "net_cagr_pct": round((net**(1/yrs)-1)*100, 1),
                     "gross_total_pct": round((gross-1)*100, 1),
                     "t": round(float((r-rt/2/1e4).mean()/((r-rt/2/1e4).std(ddof=1)/np.sqrt(len(r)))), 2),
                     "maxdd_pct": round(float(((1+r-rt/2/1e4).cumprod()/(1+r-rt/2/1e4).cumprod().cummax()-1).min())*100, 1)})
    tab = pd.DataFrame(rows)
    print("=== 滚动半仓法：不同进场时点（2025+ 分钟层，rt=15bp）===")
    print(tab.to_string(index=False))

    # 纯净值口径（把成本分别按 0 / 10 / 15bp 看）
    print("\n=== 进场时点 × 成本 矩阵（net CAGR %）===")
    grid = []
    for mod, label in KEY_MODS.items():
        if mod not in piv.columns:
            continue
        ri = piv[mod]
        r = (exiting + 0.5*ri).dropna()
        yrs = len(r)/PER_YEAR
        grid.append({"hm": label, **{f"rt{c}": round((((1+r-c/2/1e4).cumprod().iloc[-1])**(1/yrs)-1)*100, 1)
                                     for c in [0, 10, 15, 20]}})
    print(pd.DataFrame(grid).to_string(index=False))
    tab.to_csv(REP / "half_roll_entry_timing.csv", index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
