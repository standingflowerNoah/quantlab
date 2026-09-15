"""严重异动事件：基线研究 + 思路1/思路2 假设检验。

市场调整口径
------------
daily_panel.parquet 已对全部 (code,date) 计算了 fwd_N / exec_N。
对每个事件日 T：excess = 事件收益 − 当日全市场截面均值收益（等权、同一执行
口径），等价于"市场调整超额"，且天然规避事件日在热点期扎堆的市场整体漂移。

思路1（洗盘吸筹假设）
---------------------
信号：事件日 main_net_pct > 0（主力净流入）且 small_net_pct < 0（散户净流出）
强化档：main_net_pct >= +5% 且 small_net_pct <= -5%
检验：该子集前瞻收益是否系统性优于事件全体/对照组，分方向、分年稳定性、
     main_net_pct 五分位单调性。

思路2（高换手大资金承接假设）
-----------------------------
信号：turnover >= 25%（事件池内高档）且 main_net_pct > 0，
     且 direction ∈ {DOWN, SWING} 或 close < open（"逆势"）
检验：同上 + turnover 分档 × main_net 方向二维表。

统计呈现
--------
- 组间均值差 + Welch t 值（事件数大，重点看经济量级与分年一致性）
- 分年（2022-2026）各年组间差符号一致性
- 分位数单调性（五分位均值）
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "reports" / "_tmp"
HORIZONS = [1, 5, 10, 20]


def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    ev = pd.read_parquet(TMP / "abnormal_events.parquet")
    dp = pd.read_parquet(TMP / "daily_panel.parquet",
                         columns=["date", "code", "fwd_1", "fwd_5", "fwd_10",
                                  "fwd_20", "exec_5", "exec_10", "exec_20"])
    # 当日全市场截面均值（市场基准，同执行口径）
    mkt = dp.groupby("date")[["fwd_1", "fwd_5", "fwd_10", "fwd_20",
                              "exec_5", "exec_10", "exec_20"]].mean()
    mkt.columns = [f"mkt_{c}" for c in mkt.columns]
    ev = ev.merge(mkt, on="date", how="left")
    for h in HORIZONS:
        ev[f"xfwd_{h}"] = ev[f"fwd_{h}"] - ev[f"mkt_fwd_{h}"]
    for h in [5, 10, 20]:
        ev[f"xexec_{h}"] = ev[f"exec_{h}"] - ev[f"mkt_exec_{h}"]
    ev["year"] = ev["date"].dt.year
    return ev, dp


def grp_stats(df: pd.DataFrame, col: str) -> str:
    s = df[col].dropna()
    if len(s) < 10:
        return f"n={len(s)}"
    t = s.mean() / (s.std() / np.sqrt(len(s))) if s.std() > 0 else np.nan
    return (f"n={len(s):>6}  mean={s.mean():+.2%}  med={s.median():+.2%}  "
            f"win={np.mean(s > 0):.0%}  t={t:.1f}")


def compare(ev: pd.DataFrame, mask: pd.Series, label: str, cols=("xexec_5", "xexec_10", "xexec_20"),
            by_year: bool = True) -> dict:
    """信号组 vs 对照组（事件内非信号组）。"""
    out = {"label": label, "n_sig": int(mask.sum()), "n_ctl": int((~mask).sum())}
    print(f"\n--- {label}  信号 n={mask.sum()} / 对照 n={(~mask).sum()} ---")
    sub, ctl = ev[mask], ev[~mask]
    for c in cols:
        print(f"  [{c}] 信号: {grp_stats(sub, c)}")
        print(f"          对照: {grp_stats(ctl, c)}")
        s1, s0 = sub[c].dropna(), ctl[c].dropna()
        if len(s1) > 30 and len(s0) > 30 and s1.std() > 0 and s0.std() > 0:
            t = stats.ttest_ind(s1, s0, equal_var=False)
            out[f"{c}_diff"] = s1.mean() - s0.mean()
            out[f"{c}_t"] = t.statistic
    if by_year:
        rows = []
        for y, g in ev.groupby("year"):
            s1 = g.loc[mask[g.index], "xexec_10"].dropna()
            s0 = g.loc[~mask[g.index], "xexec_10"].dropna()
            if len(s1) > 5 and len(s0) > 5:
                rows.append((y, len(s1), s1.mean(), len(s0), s0.mean(), s1.mean() - s0.mean()))
        if rows:
            print("  分年 xexec_10: (年, n信号, 信号均值, n对照, 对照均值, 差)")
            for r in rows:
                print(f"    {r[0]}  {r[1]:>5}  {r[2]:+.2%}  {r[3]:>5}  {r[4]:+.2%}  {r[5]:+.2%}")
            out["yearly_diffs"] = [r[5] for r in rows]
    return out


def quintile_test(ev: pd.DataFrame, col: str, ret: str = "xexec_10",
                  within: str | None = None) -> None:
    """条件变量五分位 → 前瞻收益单调性。"""
    d = ev.dropna(subset=[col, ret]).copy()
    if len(d) < 200:
        print(f"  [{col}] 样本不足")
        return
    try:
        d["q"] = pd.qcut(d[col], 5, labels=False, duplicates="drop")
    except ValueError:
        return
    g = d.groupby("q")[ret].agg(["count", "mean"])
    print(f"  [{col} → {ret}] 五分位均值: " +
          "  ".join(f"Q{int(i)+1}={v:+.2%}(n={n})" for i, (n, v) in
                    zip(g.index, zip(g['count'], g['mean']))))


def main() -> None:
    ev, dp = load()
    print("=" * 100)
    print("PART A | 严重异动事件基线（市场调整超额，等权截面基准）")
    print("=" * 100)
    print(f"\n全样本 {len(ev):,} 事件；市场基准=当日全市场截面均值（同执行口径）")
    for d_ in ["UP", "DOWN", "SWING"]:
        sub = ev[ev["direction"] == d_]
        print(f"\n### 方向 {d_} (n={len(sub):,})")
        print(f"  [fwd_5 ] {grp_stats(sub, 'fwd_5')}")
        print(f"  [xfwd_5] {grp_stats(sub, 'xfwd_5')}")
        print(f"  [xexec_5 ] {grp_stats(sub, 'xexec_5')}")
        print(f"  [xexec_10] {grp_stats(sub, 'xexec_10')}")
        print(f"  [xexec_20] {grp_stats(sub, 'xexec_20')}")
        if d_ == "UP":
            print(f"  T+1 一字板买不进比例: {sub['entry_blocked'].mean():.1%}")
        # 分年
        g = sub.groupby("year")["xexec_10"].agg(["count", "mean"])
        print("  分年 xexec_10: " + "  ".join(f"{y}:{m:+.1%}(n={n})" for y, m, n in
                                              zip(g.index, g["mean"], g["count"])))

    # 尾部风险（风控设计输入）
    print("\n### 事件后 20 日内最大不利（近似：fwd 负分位）")
    for d_ in ["UP", "DOWN", "SWING"]:
        sub = ev[ev["direction"] == d_]["fwd_20"].dropna()
        if len(sub):
            print(f"  {d_}: fwd_20 p5={sub.quantile(0.05):+.1%}  "
                  f"p25={sub.quantile(0.25):+.1%}  p50={sub.median():+.1%}")

    mf_cols = [c for c in ev.columns if c.endswith("_net_pct")]
    if not mf_cols:
        print("\n!! 事件表无资金流列，PART B/C 跳过（资金流回补后重跑）")
        return

    print("\n" + "=" * 100)
    print("PART B | 思路1：主力净流入 + 散户净流出（洗盘吸筹假设）")
    print("=" * 100)
    print("\n### 资金流占比分布（按方向，%）")
    for d_ in ["UP", "DOWN", "SWING"]:
        sub = ev[ev["direction"] == d_]
        print(f"  {d_}: main_net_pct 中位={sub['main_net_pct'].median():+.1%} "
              f"p25={sub['main_net_pct'].quantile(0.25):+.1%} "
              f"p75={sub['main_net_pct'].quantile(0.75):+.1%}  "
              f"主力净流入占比>0 比例={np.mean(sub['main_net_pct'] > 0):.0%}")

    sig1 = (ev["main_net_pct"] > 0) & (ev["small_net_pct"] < 0)
    sig1s = (ev["main_net_pct"] >= 0.05) & (ev["small_net_pct"] <= -0.05)
    print("\n### 思路1 全方向")
    r1a = compare(ev, sig1, "G1: main>0 & small<0（全方向）")
    print("\n### 思路1 仅 DOWN（下跌异动=洗盘叙事主场景）")
    evd = ev[ev["direction"] == "DOWN"]
    r1d = compare(evd, sig1[evd.index], "G1-Down: main>0 & small<0")
    print("\n### 思路1 仅 SWING")
    evs = ev[ev["direction"] == "SWING"]
    r1s = compare(evs, sig1[evs.index], "G1-Swing: main>0 & small<0")
    print("\n### 思路1 强化档（|5%| 阈值）仅 DOWN")
    r1ds = compare(evd, sig1s[evd.index], "G1s-Down: main>=5% & small<=-5%")

    print("\n### 单调性：main_net_pct 五分位 → xexec_10")
    for d_ in ["UP", "DOWN", "SWING"]:
        print(f"  方向 {d_}:")
        quintile_test(ev[ev["direction"] == d_], "main_net_pct")

    print("\n" + "=" * 100)
    print("PART C | 思路2：高换手 + 大资金逆势承接")
    print("=" * 100)
    # 换手分档
    print("\n### 换手率分档 × 方向（xexec_10 均值）")
    bins = [0.15, 0.20, 0.25, 0.30, 0.40, 10]
    labels = ["15-20%", "20-25%", "25-30%", "30-40%", "40%+"]
    ev["to_bin"] = pd.cut(ev["turnover"], bins=bins, labels=labels)
    piv = ev.pivot_table(index="to_bin", columns="direction",
                         values="xexec_10", aggfunc=["mean", "count"])
    print(piv.to_string())

    # 逆势承接：DOWN/SWING 且主力净流入
    adverse = ev["direction"].isin(["DOWN", "SWING"]) | (ev["close"] < ev["open"])
    sig2 = (ev["turnover"] >= 0.25) & (ev["main_net_pct"] > 0) & adverse
    sig2_loose = (ev["turnover"] >= 0.25) & (ev["main_net_pct"] > 0)
    print("\n### 思路2 宽档：turnover>=25% & main>0（全方向）")
    r2a = compare(ev, sig2_loose, "G2-loose: to>=25% & main>0")
    print("\n### 思路2 逆势档：turnover>=25% & main>0 & (DOWN|SWING|收阴)")
    r2b = compare(ev, sig2, "G2-adverse: to>=25% & main>0 & adverse")
    print("\n### 思路2 仅 DOWN 高换手主力承接")
    evd2 = ev[ev["direction"] == "DOWN"]
    sig2d = (evd2["turnover"] >= 0.25) & (evd2["main_net_pct"] > 0)
    r2d = compare(evd2, sig2d, "G2-Down: to>=25% & main>0")

    # 思路1 vs 思路2 重叠
    both = (sig1 & sig2).sum()
    print(f"\n### 思路1 ∩ 思路2 重叠: {both} / sig1={sig1.sum()} / sig2={sig2.sum()}")

    # 保存结果供报告引用
    summary = {"r1a": r1a, "r1d": r1d, "r1s": r1s, "r1ds": r1ds,
               "r2a": r2a, "r2b": r2b, "r2d": r2d}
    import json
    with open(TMP / "hypothesis_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n汇总 → {TMP / 'hypothesis_summary.json'}")


if __name__ == "__main__":
    main()
