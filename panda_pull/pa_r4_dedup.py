# -*- coding: utf-8 -*-
"""PandaAI 复现因子批次 R4 去重：族内 + 核心风格相关矩阵（月末截面 spearman 平均）
=============================================================================
评价框架 R4 闸门（research/factor_eval_dimensions_20260912.md §4），无锁。
复用 corr_check.py 的加载/相关函数，扩展为全矩阵（含 pa×pa 族内 6 对）。

规则（先定后跑，对齐 hf_r4_dedup）：|rho|>0.7 归簇（union-find），
簇内保留 |ICIR20| 最大者（现场重算口径，2023-01~2026-09）。
ICIR20 硬编码自 run_eval 报告：040 0.659 / 088 0.634 / 044 0.852 / 5d 0.468。

用法：quantlab venv python panda_pull/pa_r4_dedup.py
产出：reports/_tmp/pa_r4_corr_matrix.csv + 控制台去重结论
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, ".")

from corr_check import load_month_end, mean_rank_corr  # noqa: E402

PA = ["pa_a101_040", "pa_a101_044", "pa_a101_088", "pa_5d_min_low_ratio"]
REF = ["volatility_20", "reversal_5", "reversal_10", "momentum_20",
       "turnover", "size", "amihud_20", "alpha040", "alpha044", "alpha088"]
ALL = PA + REF

ICIR20 = {"pa_a101_040": 0.659, "pa_a101_088": 0.634,
          "pa_a101_044": 0.852, "pa_5d_min_low_ratio": 0.468}
THR = 0.7
OUT_CSV = Path("reports/_tmp/pa_r4_corr_matrix.csv")


def main() -> None:
    from quantlab.config import FACTOR_DIR  # noqa: F401  (corr_check 依赖)

    avail = {}
    for f in ALL:
        m = load_month_end(f)
        if not m.empty:
            avail[f] = m
        else:
            print(f"[skip] {f}: 无湖数据")
    names = [f for f in ALL if f in avail]

    mat = pd.DataFrame(np.nan, index=names, columns=names)
    for i, a in enumerate(names):
        mat.loc[a, a] = 1.0
        for b in names[i + 1:]:
            r = mean_rank_corr(avail[a], avail[b])
            mat.loc[a, b] = mat.loc[b, a] = r

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    mat.to_csv(OUT_CSV, encoding="utf-8-sig")
    print("\n=== 相关矩阵（月末截面 spearman 跨月平均）===")
    print(mat.round(3).to_string())

    # 归簇（仅 pa 族参与 keep 竞争；REF 仅作暴露参考，不参与归簇）
    parent = {f: f for f in PA}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pairs = []
    for i, a in enumerate(PA):
        for b in PA[i + 1:]:
            r = mat.loc[a, b]
            if pd.notna(r) and abs(r) > THR:
                parent[find(a)] = find(b)
                pairs.append((a, b, round(float(r), 3)))
    clusters: dict[str, list[str]] = {}
    for f in PA:
        clusters.setdefault(find(f), []).append(f)

    print("\n=== R4 去重结论（|rho|>0.7 归簇，保留 |ICIR20| 最大）===")
    survivors = []
    for cl in clusters.values():
        keep = max(cl, key=lambda f: ICIR20.get(f, 0))
        survivors.append(keep)
        if len(cl) > 1:
            absorbed = [f for f in cl if f != keep]
            print(f"簇 {' | '.join(sorted(cl))} → 保留 {keep}"
                  f"（ICIR {ICIR20.get(keep):.3f}），吸收 {absorbed}")
        else:
            print(f"独立：{keep}（ICIR {ICIR20.get(keep):.3f}）")
    print(f"\n超阈族内对: {pairs if pairs else '无'}")
    print(f"幸存: {survivors}")
    print(f"\n矩阵 → {OUT_CSV}")

    # 风格暴露摘要（|rho|>0.6 提示）
    print("\n=== 风格暴露 |rho|>0.6 ===")
    for pa in PA:
        for ref in REF:
            r = mat.loc[pa, ref] if ref in mat.columns else np.nan
            if pd.notna(r) and abs(r) > 0.6:
                print(f"  {pa:22s} x {ref:16s} {r:+.3f}")


if __name__ == "__main__":
    main()
