#!/usr/bin/env python3
"""Phase 2 探针：Q2 截面条件化（分钟口径 2025+，14:56 执行）
=================================================================
只用 pseudo_daily（不依赖主库）。逐日横截面分组（等频 5 组），
报告各组的 exec_ret（买 14:56 / 卖次开）日均与 t 值。

分组变量：
  tail_mom      当日 09:30→14:56 收益（尾盘动量，S1）
  amount_p      当日截至 14:56 成交额（流动性/规模代理，S3 近似）
  amp           当日振幅 (high_p-low_p)/open（波动）
  close_pos     14:56 价在当日区间中的位置（强度）
  ret_1d        T-1 日全日收益（反转，S2 近似）
  overnight_p   T-1 日隔夜段（自相关，S6 近似）

输出：reports/overnight/q2_probe.json + 控制台表格
"""
from __future__ import annotations

import glob
import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PD_GLOB = str(ROOT / "data/lake/factor/overnight/pseudo_daily/*.parquet")
OUT = ROOT / "reports/overnight"
N_GROUPS = 5


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["code", "date"]).copy()
    df["tail_mom"] = df.tail_ret_930_1456
    df["amp"] = (df.high_p - df.low_p) / df.open.replace(0, np.nan)
    rng = (df.high_p - df.low_p).replace(0, np.nan)
    df["close_pos"] = (df.close_t - df.low_p) / rng
    df["ret_1d"] = df.intraday_eod
    df["overnight_p"] = df.groupby("code")["exec_ret"].shift(1)
    df["in_univ"] = ~df.code.str.startswith(("4", "8")) & ~df.code.str.startswith("920")
    return df


def group_stats(df: pd.DataFrame, col: str, target: str = "exec_ret") -> list[dict]:
    d = df[df.in_univ & df[col].notna() & df[target].notna()].copy()
    # 逐日等频分组（qcut 在每日截面内做）
    d["grp"] = d.groupby("date")[col].transform(
        lambda s: pd.qcut(s.rank(method="first"), N_GROUPS, labels=False) + 1)
    # 组内等权 → 日度序列
    g = d.groupby(["date", "grp"])[target].mean().reset_index()
    out = []
    for k in range(1, N_GROUPS + 1):
        s = g[g.grp == k].set_index("date")[target].dropna()
        mu, sd, n = s.mean(), s.std(ddof=1), len(s)
        out.append({
            "group": k, "n_days": n,
            "mean_bp": float(mu * 1e4),
            "ann_pct": float(mu * 244 * 100),
            "t_stat": float(mu / (sd / np.sqrt(n))) if sd > 0 else None,
            "win": float((s > 0).mean()),
        })
    # 顶组-底组价差（逐日）
    wide = g.pivot(index="date", columns="grp", values=target).dropna()
    sp = wide[N_GROUPS] - wide[1]
    out.append({
        "spread_top_minus_bottom": {
            "mean_bp": float(sp.mean() * 1e4),
            "ann_pct": float(sp.mean() * 244 * 100),
            "t_stat": float(sp.mean() / (sp.std(ddof=1) / np.sqrt(len(sp)))),
            "win": float((sp > 0).mean()),
        }
    })
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    df = pd.concat([pd.read_parquet(f) for f in sorted(glob.glob(PD_GLOB))],
                   ignore_index=True)
    df = add_features(df)
    print(f"[load] rows={len(df):,} codes={df.code.nunique():,} "
          f"{df.date.min()} → {df.date.max()}")

    cols = ["tail_mom", "amount_p", "amp", "close_pos", "ret_1d", "overnight_p"]
    res = {}
    for c in cols:
        res[c] = group_stats(df, c)
        print(f"\n=== {c} → exec_ret（买 14:56 / 卖次开）===")
        print(f"{'grp':>4} {'days':>5} {'mean_bp':>9} {'ann%':>8} {'t':>7} {'win':>6}")
        for r in res[c][:-1]:
            print(f"{r['group']:>4} {r['n_days']:>5} {r['mean_bp']:>9.2f} "
                  f"{r['ann_pct']:>8.2f} {r['t_stat']:>7.2f} {r['win']:>6.1%}")
        sp = res[c][-1]["spread_top_minus_bottom"]
        print(f"  spread(top-bottom): {sp['mean_bp']:+.2f}bp/日 "
              f"(ann {sp['ann_pct']:+.1f}%, t={sp['t_stat']:.2f})")

    # 成本敏感性：最优组的净年化（往返 10/15/25bp）
    print("\n=== 成本敏感性（最优组毛收益 vs 成本线）===")
    for c in cols:
        best = max(res[c][:-1], key=lambda r: r["mean_bp"])
        for cost in (10, 15, 25):
            net = (best["mean_bp"] - cost) / 1e4 * 244 * 100
            print(f"  {c:>12s} grp{best['group']} 毛 {best['mean_bp']:+.2f}bp "
                  f"成本 {cost}bp → 年化 {net:+.1f}%")

    (OUT / "q2_probe.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2, default=float), encoding="utf-8")


if __name__ == "__main__":
    main()
