"""官方口径（交易所异常波动标准）严重异动事件：基线 + 思路1/2 假设检验。

输入：reports/_tmp/abnormal_events_official.parquet（v2 官方口径事件表）
复用 abnormal_hypothesis_test 的统计函数（compare/grp_stats/quintile_test）。
主样本：非 ST（is_st 快照的历史时点错配 → ST 档不可靠，剔除并声明）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from abnormal_hypothesis_test import compare, grp_stats, quintile_test  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "reports" / "_tmp"


def main() -> None:
    ev = pd.read_parquet(TMP / "abnormal_events_official.parquet")
    dp = pd.read_parquet(TMP / "daily_panel.parquet",
                         columns=["date", "code", "fwd_5", "fwd_10", "fwd_20",
                                  "exec_5", "exec_10", "exec_20"])
    mkt = dp.groupby("date")[["fwd_5", "fwd_10", "fwd_20",
                              "exec_5", "exec_10", "exec_20"]].mean()
    mkt.columns = [f"mkt_{c}" for c in mkt.columns]
    ev = ev.merge(mkt, on="date", how="left")
    for h in [5, 10, 20]:
        ev[f"xfwd_{h}"] = ev[f"fwd_{h}"] - ev[f"mkt_fwd_{h}"]
        ev[f"xexec_{h}"] = ev[f"exec_{h}"] - ev[f"mkt_exec_{h}"]
    ev["year"] = ev["date"].dt.year

    ev = ev[~ev["is_st"].fillna(False).astype(bool)].copy()  # 主样本：非 ST
    print("=" * 100)
    print("PART A | 官方口径严重异动基线（非ST，市场调整超额）")
    print(f"样本 {len(ev):,} / UP {int((ev['direction']=='UP').sum()):,} / "
          f"DOWN {int((ev['direction']=='DOWN').sum()):,} / "
          f"严重异动子集 sev_abn {int(ev['sev_abn'].sum()):,}")
    print("=" * 100)
    for d_ in ["UP", "DOWN"]:
        sub = ev[ev["direction"] == d_]
        print(f"\n### {d_} (n={len(sub):,})")
        for c in ["xfwd_10", "xexec_5", "xexec_10", "xexec_20"]:
            print(f"  [{c}] {grp_stats(sub, c)}")
        g = sub.groupby("year")["xexec_10"].agg(["count", "mean"])
        print("  分年 xexec_10: " + "  ".join(f"{y}:{m:+.1%}(n={n})" for y, m, n in
                                              zip(g.index, g["mean"], g["count"])))
        print(f"  T+1 一字板比例: {sub['entry_blocked'].mean():.1%}")
    # 严重异动子集（10日累计偏离 ≥100%/≤-50%）
    sv = ev[ev["sev_abn"]]
    print(f"\n### 严重异常波动子集 sev_abn (n={len(sv):,})")
    for c in ["xexec_5", "xexec_10", "xexec_20"]:
        print(f"  [{c}] {grp_stats(sv, c)}")
    # dev3 强度分位（触发强度）
    print("\n### dev3 触发强度五分位 → xexec_10（UP/DOWN 分开）")
    for d_ in ["UP", "DOWN"]:
        print(f"  方向 {d_}:")
        quintile_test(ev[ev["direction"] == d_], "dev3")

    if "main_net_pct" not in ev.columns:
        print("\n!! 无资金流列")
        return

    print("\n" + "=" * 100)
    print("PART B | 思路1：主力净流入 + 散户净流出（官方口径）")
    print("=" * 100)
    for d_ in ["UP", "DOWN"]:
        sub = ev[ev["direction"] == d_]
        print(f"  {d_}: main_net_pct 中位={sub['main_net_pct'].median():+.1%} "
              f"主力净流入>0 比例={np.mean(sub['main_net_pct'] > 0):.0%}")
    sig1 = (ev["main_net_pct"] > 0) & (ev["small_net_pct"] < 0)
    sig1s = (ev["main_net_pct"] >= 0.05) & (ev["small_net_pct"] <= -0.05)
    r1a = compare(ev, sig1, "G1: main>0 & small<0（全方向）")
    evd = ev[ev["direction"] == "DOWN"]
    r1d = compare(evd, sig1[evd.index], "G1-Down: main>0 & small<0")
    r1ds = compare(evd, sig1s[evd.index], "G1s-Down: main>=5% & small<=-5%")
    evu = ev[ev["direction"] == "UP"]
    r1u = compare(evu, sig1[evu.index], "G1-Up: main>0 & small<0")
    print("\n### 单调性：main_net_pct 五分位 → xexec_10")
    for d_ in ["UP", "DOWN"]:
        print(f"  方向 {d_}:")
        quintile_test(ev[ev["direction"] == d_], "main_net_pct")

    print("\n" + "=" * 100)
    print("PART C | 思路2：高换手 + 大资金逆势承接（官方口径）")
    print("=" * 100)
    bins = [0, 0.10, 0.15, 0.20, 0.30, 10]
    labels = ["<10%", "10-15%", "15-20%", "20-30%", "30%+"]
    ev["to_bin"] = pd.cut(ev["turnover"], bins=bins, labels=labels)
    piv = ev.pivot_table(index="to_bin", columns="direction",
                         values="xexec_10", aggfunc=["mean", "count"])
    print("\n### 换手率分档 × 方向（xexec_10）")
    print(piv.to_string())

    adverse = ev["direction"].isin(["DOWN"]) | (ev["close"] < ev["open"])
    sig2 = (ev["turnover"] >= 0.20) & (ev["main_net_pct"] > 0) & adverse
    sig2_loose = (ev["turnover"] >= 0.20) & (ev["main_net_pct"] > 0)
    r2a = compare(ev, sig2_loose, "G2-loose: to>=20% & main>0（全方向）")
    r2b = compare(ev, sig2, "G2-adverse: to>=20% & main>0 & (DOWN|收阴)")
    evd2 = ev[ev["direction"] == "DOWN"]
    sig2d = (evd2["turnover"] >= 0.20) & (evd2["main_net_pct"] > 0)
    r2d = compare(evd2, sig2d, "G2-Down: to>=20% & main>0")
    print(f"\n### 思路1 ∩ 思路2 重叠: {(sig1 & sig2).sum()} / sig1={sig1.sum()} / sig2={sig2.sum()}")

    import json
    with open(TMP / "hypothesis_summary_official.json", "w", encoding="utf-8") as f:
        json.dump({"r1a": r1a, "r1d": r1d, "r1ds": r1ds, "r1u": r1u,
                   "r2a": r2a, "r2b": r2b, "r2d": r2d},
                  f, ensure_ascii=False, indent=2, default=str)
    print(f"\n汇总 → {TMP / 'hypothesis_summary_official.json'}")


if __name__ == "__main__":
    main()
