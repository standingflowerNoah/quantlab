#!/usr/bin/env python3
"""Q5：8 因子合成 vs 生产模型口径（size+amihud 等权）—— 持有期扫描对比
=================================================================
问题：Q3/Q4 的合成打分把隔夜腿拉正（+12.7bp/日），持有 ≥2 天扣费转正。
      但它选的因子全是微盘/低流动性族——**是不是就等于生产模型（size+amihud
      top100、20 日调仓）？** 若是，则"因子选股获取正收益"的答案是"已知 alpha
      的再确认"，而非新发现。

做法：同一持有期扫描框架下对比两个打分
  PROD-like  = rank(amihud_20) − rank(size)        （生产模型口径，等权）
  COMPOSITE  = Q3 的 8 因子 IS 择优合成
输出：reports/overnight/q5_vs_prod.json
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
FACTOR_DIR = ROOT / "data/lake/factor"
COMPOSITE = ROOT / "data/lake/factor/overnight/composite_score/score.parquet"
OUT = ROOT / "reports/overnight"
TOPN = 100
COST_BP = 15.0
TRADING_DAYS = 244.0
HOLDS = [1, 2, 5, 10, 20]
IS_END = pd.Timestamp("2024-12-31")


def main() -> None:
    con = duckdb.connect()
    con.execute("PRAGMA threads=6")

    seg = con.execute(f"""
        SELECT code, date, total_ret FROM read_parquet('{SEG_GLOB}')
        WHERE in_ex AND NOT halted AND total_ret IS NOT NULL
    """).df()
    comp = pd.read_parquet(COMPOSITE)[["code", "date", "score"]].dropna()

    # PROD-like 打分
    print("[build] PROD-like = rank(amihud_20) − rank(size) …")
    parts = []
    for name, sign in [("amihud_20", +1.0), ("size", -1.0)]:
        glob_p = str(FACTOR_DIR / name / "*.parquet").replace("\\", "/")
        d = con.execute(f"""
            SELECT code, date, rank() OVER (PARTITION BY date ORDER BY value) AS r
            FROM read_parquet('{glob_p}')
            WHERE value IS NOT NULL AND date >= DATE '2022-01-01'
        """).df()
        d["s"] = sign * d["r"]
        parts.append(d[["code", "date", "s"]])
    prod = parts[0].merge(parts[1], on=["code", "date"], how="outer")
    prod["score"] = prod[["s_x", "s_y"]].mean(axis=1)
    prod = prod[["code", "date", "score"]].dropna()

    # 收益率矩阵
    piv = seg.pivot(index="date", columns="code", values="total_ret").sort_index()
    lr = np.log1p(np.clip(piv.fillna(0.0).to_numpy(), -0.9, 5.0))
    cum = np.cumsum(lr, axis=0)
    dates, codes = piv.index.to_numpy(), piv.columns.to_numpy()
    dpos = {d: i for i, d in enumerate(dates)}
    cpos = {c: i for i, c in enumerate(codes)}

    def sweep(sc: pd.DataFrame) -> dict:
        sc = sc[sc.code.isin(set(codes))]
        sel = {}
        for date, g in sc.groupby("date"):
            if date not in dpos:
                continue
            ii = [cpos[c] for c in g.nlargest(TOPN, "score").code]
            if ii:
                sel[dpos[date]] = np.array(ii)
        out = {}
        for H in HOLDS:
            rows = []
            for i, ii in sel.items():
                if i + H >= len(dates):
                    continue
                rows.append((dates[i],
                             float(np.mean(np.expm1(cum[i + H, ii] - cum[i, ii]))) / H))
            s = pd.Series(dict(rows)).sort_index().dropna()
            oos = s[s.index > IS_END]
            mu, sd, n = s.mean(), s.std(ddof=1), len(s)
            by_year = {str(y): float((g.mean() - COST_BP / 1e4 / H) * TRADING_DAYS * 100)
                       for y, g in s.groupby(pd.DatetimeIndex(s.index).year)}
            out[f"H{H}"] = {
                "gross_bp_per_day": float(mu * 1e4),
                "t": float(mu / (sd / np.sqrt(n))),
                "oos_gross_bp_per_day": float(oos.mean() * 1e4),
                "net_ann_pct": float((mu - COST_BP / 1e4 / H) * TRADING_DAYS * 100),
                "by_year_net_pct": by_year,
            }
        return out

    res = {"prod_like": sweep(prod), "composite": sweep(comp)}
    print(f"\n{'口径':>10} {'H':>4} {'毛bp/日':>9} {'t':>6} {'OOS毛':>8} {'净年化':>8}  分年度净")
    for k, v in res.items():
        for h, st in v.items():
            yrs = " ".join(f"{y}:{x:+.0f}%" for y, x in st["by_year_net_pct"].items())
            print(f"{k:>10} {h:>4} {st['gross_bp_per_day']:>9.2f} {st['t']:>6.2f} "
                  f"{st['oos_gross_bp_per_day']:>8.2f} {st['net_ann_pct']:>7.1f}%  {yrs}")

    (OUT / "q5_vs_prod.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    print("\n[done] → reports/overnight/q5_vs_prod.json")


if __name__ == "__main__":
    main()
