# -*- coding: utf-8 -*-
"""hf 批次 R4 同族去重：A 层 9 因子族内相关矩阵 + 核心风格暴露复核
=================================================================
评价框架 R4 闸门（research/factor_eval_dimensions_20260912.md §4），
全部无锁（因子湖 + factor_corr_daily + R1 汇总 CSV）：

  1) 族内两两相关：月末截面 pairwise spearman，跨月末平均
     （9 因子分钟数据全、覆盖接近，pairwise 稳定）
  2) 核心风格相关：factor_corr_daily L2 聚合 mean rho
     （hf 因子 × 6 风格代表 + sue_i + amihud_20 + hf_amihud_20）
  3) 去重规则（先定后跑）：|rho_intra| > 0.7 归簇（union-find），
     簇内保留 |ICIR20| 最大者（平手取 FDR_q 更小），
     其余标记剔除并注明被谁吸收。

用法：python scripts/hf_r4_dedup.py
产出：reports/_tmp/hf_r4_dedup_summary.csv
      reports/_tmp/hf_r4_corr_matrix.csv
"""
from __future__ import annotations

import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FACTOR_DIR = ROOT / "data/lake/factor"
CORR_GLOB = str(ROOT / "data/lake/factor_corr_daily/part-*.parquet")
R1_CSV = ROOT / "reports/_tmp/hf_r1_robustness_summary.csv"
OUT_CSV = ROOT / "reports/_tmp/hf_r4_dedup_summary.csv"
OUT_MAT = ROOT / "reports/_tmp/hf_r4_corr_matrix.csv"

A9 = ["hf_amihud_20", "hf_rsk_20", "hf_rku_20", "hf_dsem_20",
      "hf_vopen_20", "hf_vampp_20", "hf_rfirst30_20", "hf_corr_rv_20",
      "hf_topvr_20"]
THR = 0.7          # 归簇阈值（|rho|）
MIN_XSEC = 300     # 月末截面最少共同股票数

CON = duckdb.connect()
CON.execute("SET memory_limit='2GB'")
CON.execute("SET threads=4")
CON.execute("SET preserve_insertion_order=false")


def month_end_dates(dates: pd.Series) -> list:
    s = pd.Series(pd.to_datetime(dates)).drop_duplicates().sort_values()
    return list(s.groupby(s.dt.to_period("M")).max())


def load_me_values(factors: list[str], me_list: list) -> pd.DataFrame:
    """因子湖月末截面（与 hf_r2_cost.py 同口径）。"""
    dl = ",".join(f"'{pd.Timestamp(d).date()}'" for d in me_list)
    frames = []
    for fac in factors:
        f = CON.execute(f"""
            SELECT CAST(date AS DATE) AS date, code, value
            FROM read_parquet('{(FACTOR_DIR / fac).as_posix()}/*.parquet')
            WHERE CAST(date AS DATE) IN ({dl})
        """).fetchdf()
        f["factor"] = fac
        frames.append(f)
    return pd.concat(frames, ignore_index=True)


def intra_corr_matrix(me: pd.DataFrame, factors: list[str]) -> pd.DataFrame:
    """月末截面 pairwise spearman → 跨月末平均。"""
    piv = me.pivot_table(index=["date", "code"], columns="factor",
                         values="value")
    mats = []
    for d, g in piv.groupby(level=0):
        g = g.droplevel(0).dropna(axis=0, how="any")
        if len(g) < MIN_XSEC:
            continue
        mats.append(g[factors].rank().corr())
    mat = sum(mats) / len(mats)
    return mat


def core_corr(factors: list[str]) -> pd.DataFrame:
    """factor_corr_daily 聚合：hf 因子 vs 9 个 core 的日频 rho 均值。"""
    lst = ",".join(f"'{f}'" for f in factors)
    df = CON.execute(f"""
        SELECT factor, core_factor, avg(rho) AS rho_mean,
               count(*) AS n_days
        FROM read_parquet('{CORR_GLOB}')
        WHERE factor IN ({lst})
        GROUP BY factor, core_factor ORDER BY factor, core_factor
    """).fetchdf()
    return df.pivot(index="factor", columns="core_factor",
                    values="rho_mean")


def cluster_factors(mat: pd.DataFrame, keep_rank: pd.Series) -> list[list[str]]:
    """|rho|>THR 归簇（union-find），返回簇列表。"""
    names = list(mat.columns)
    parent = {n: n for n in names}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if abs(mat.loc[a, b]) > THR:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb
    groups: dict[str, list[str]] = {}
    for n in names:
        groups.setdefault(find(n), []).append(n)
    return list(groups.values())


def main() -> None:
    t0 = time.time()
    r1 = pd.read_csv(R1_CSV).set_index("factor")

    print("加载月末截面 ...", flush=True)
    me_list = month_end_dates(
        CON.execute(f"""
            SELECT date FROM read_parquet(
                '{(FACTOR_DIR / A9[0]).as_posix()}/*.parquet')"""
        ).fetchdf()["date"])
    me = load_me_values(A9, me_list)
    print(f"  {len(me):,} 行 / 月末 {len(me_list)} 个", flush=True)

    print("族内相关矩阵 ...", flush=True)
    mat = intra_corr_matrix(me, A9)
    mat.to_csv(OUT_MAT, encoding="utf-8-sig")
    print(mat.round(3).to_string(), flush=True)

    print("核心风格相关（factor_corr_daily 聚合）...", flush=True)
    cc = core_corr(A9)
    print(cc.round(3).to_string(), flush=True)

    keep_rank = (r1.loc[A9, "full_icir20"].abs()
                 .rename("keep_key").to_frame()
                 .join(r1.loc[A9, "FDR_q"].rename("fdr")))
    clusters = cluster_factors(mat, keep_rank["keep_key"])

    rows = []
    for cl in clusters:
        cl_sorted = sorted(cl, key=lambda f: (-keep_rank.loc[f, "keep_key"],
                                              keep_rank.loc[f, "fdr"]))
        keep = cl_sorted[0]
        for f in cl:
            peer = keep if f != keep else (
                cl_sorted[1] if len(cl_sorted) > 1 else "")
            rows.append({
                "factor": f,
                "cluster": "|".join(sorted(cl)),
                "cluster_n": len(cl),
                "keep": keep,
                "peer": peer,
                "peer_rho": round(float(mat.loc[f, peer]), 3) if peer else np.nan,
                "icir20_abs": round(keep_rank.loc[f, "keep_key"], 3),
                "FDR_q": keep_rank.loc[f, "fdr"],
                "R4_keep": f == keep,
            })
    out = pd.DataFrame(rows).sort_values(["R4_keep", "icir20_abs"],
                                         ascending=[False, False])
    out.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    survivors = out[out["R4_keep"]]["factor"].tolist()
    print(f"\n去重幸存（{len(survivors)}）: {survivors}")
    print(f"汇总 → {OUT_CSV}")
    print(f"矩阵 → {OUT_MAT}")
    print(out.to_string(index=False))
    print(f"({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
