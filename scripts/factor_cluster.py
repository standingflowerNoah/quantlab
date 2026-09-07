"""因子库全量聚类降维：258 因子 → 独立信号图谱
======================================================================
目的：
1) ML 重试前置：258 → 独立代表集（|ρ|≤0.7 簇内去重）
2) 因子库结构图谱：家族/簇结构、独立信号总数
3) 冗余判定基准：新因子入库时的簇归属速查

方法：
- 相关矩阵：月度首个交易日抽样（~57 日）× 全截面 rank pct，
  向量化 spearman（258×258 一次算完）；corr 缓存断点续跑
- 层次聚类：distance = 1 − |corr|，average linkage，阈值 0.3（即 |ρ|=0.7 切簇）
- 簇代表：medoid（簇内与其他成员平均 |corr| 最低的因子——簇中心）
输出：reports/factor_clusters_20260907.json + .csv + corr 缓存
"""
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import glob

import numpy as np
import pandas as pd
import duckdb
from scipy.cluster.hierarchy import linkage, fcluster
from scipy.spatial.distance import squareform

ROOT = Path(__file__).resolve().parent.parent
FACTOR_DIR = ROOT / "data" / "lake" / "factor"
CORR_CACHE = ROOT / "reports" / "_factor_corr_cache.pkl"

_con = duckdb.connect()


def q(sql):
    return _con.execute(sql).df()


def _build_corr() -> pd.DataFrame:
    factor_names = sorted(p.name for p in FACTOR_DIR.iterdir()
                          if p.is_dir() and not p.name.startswith("_"))
    print(f"因子库 {len(factor_names)} 个，抽样截面构建相关矩阵...", flush=True)
    dates = q("""
        SELECT DISTINCT CAST(date AS DATE) AS d FROM read_parquet('{}')
        WHERE date >= '2022-01-01' ORDER BY d
    """.format(str(ROOT / "data/lake/clean/mirror/kline_daily.parquet").replace("\\", "/")))
    ds = pd.to_datetime(dates["d"])
    samp = ds.groupby([ds.dt.year, ds.dt.month]).min().tolist()
    print(f"抽样 {len(samp)} 个月度截面", flush=True)
    dlist = ",".join(f"'{d.date()}'" for d in samp)

    cols = {}
    for i, name in enumerate(factor_names):
        pattern = str(FACTOR_DIR / name / "*.parquet").replace("\\", "/")
        if not glob.glob(pattern):
            continue
        try:
            fv = q(f"""
                SELECT CAST(date AS DATE) AS date, code, rp FROM (
                    SELECT date, code,
                           rank() OVER (PARTITION BY CAST(date AS DATE)
                                        ORDER BY value) * 1.0 /
                           NULLIF(COUNT(value) OVER (PARTITION BY
                                  CAST(date AS DATE)), 0) AS rp
                    FROM read_parquet('{pattern}')
                    WHERE CAST(date AS DATE) IN ({dlist})
                      AND value IS NOT NULL AND isfinite(value)
                ) WHERE rp IS NOT NULL
            """)
        except Exception as e:
            print(f"  跳过 {name}: {e}", flush=True)
            continue
        cols[name] = fv.set_index(["date", "code"])["rp"].astype("float32")
        if (i + 1) % 50 == 0:
            print(f"  读取 {i+1}/{len(factor_names)}", flush=True)

    feat = pd.DataFrame(cols)
    print(f"抽样矩阵 {feat.shape}，计算 spearman 相关...", flush=True)
    corr = feat.corr(method="spearman").astype("float32")
    corr.to_pickle(CORR_CACHE)
    corr.to_csv(ROOT / "reports" / "factor_corr_matrix_20260907.csv")
    return corr


def main():
    t0 = time.time()
    if CORR_CACHE.exists():
        corr = pd.read_pickle(CORR_CACHE)
        print(f"缓存相关矩阵 {corr.shape}（{time.time()-t0:.0f}s）", flush=True)
    else:
        corr = _build_corr()
        print(f"相关矩阵完成（{time.time()-t0:.0f}s）", flush=True)

    # 层次聚类（缺失对置 NaN → 填 0 相关；copy 保证可写）
    c = np.array(corr.fillna(0.0), dtype="float32")
    np.fill_diagonal(c, 1.0)
    dist = 1.0 - np.abs(c)
    np.fill_diagonal(dist, 0.0)
    dist = (dist + dist.T) / 2
    Z = linkage(squareform(dist, checks=False), method="average")
    labels = fcluster(Z, t=0.30, criterion="distance")   # |ρ|=0.7 切簇

    clus = pd.DataFrame({"factor": corr.index, "cluster": labels})
    n_clusters = clus["cluster"].nunique()
    sizes = clus.groupby("cluster")["factor"].count().sort_values(ascending=False)
    print(f"\n=== 聚类结果：{n_clusters} 个独立簇（阈值 |ρ|=0.7）===")
    print(f"簇大小分布: max={sizes.max()} median={sizes.median():.0f} "
          f"single={int((sizes == 1).sum())}")

    # 簇代表：medoid
    reps = []
    for cl, g in clus.groupby("cluster"):
        members = g["factor"].tolist()
        if len(members) == 1:
            reps.append({"cluster": int(cl), "rep": members[0],
                         "size": 1, "members": members})
            continue
        sub = corr.loc[members, members].abs().fillna(0.0)
        avg = sub.mean(axis=1)
        rep = avg.idxmin()
        reps.append({"cluster": int(cl), "rep": rep, "size": len(members),
                     "members": members})
    reps_df = pd.DataFrame(reps).sort_values("size", ascending=False)

    print(f"\n=== 独立代表清单（{len(reps_df)} 个）===")
    big = reps_df[reps_df["size"] >= 3]
    for _, r in big.iterrows():
        others = [m for m in r["members"] if m != r["rep"]][:6]
        print(f"  [{r['size']:>3d}] {r['rep']:24s} ← {', '.join(others)}"
              + (" ..." if r["size"] > 7 else ""))

    reps_df.to_csv(ROOT / "reports" / "factor_clusters_20260907.csv",
                   index=False)
    import json
    json.dump({"n_factors": int(len(corr)), "n_clusters": int(n_clusters),
               "threshold": 0.7,
               "representatives": reps_df.assign(
                   members=reps_df["members"].apply(list)).to_dict("records")},
              open(ROOT / "reports" / "factor_clusters_20260907.json", "w",
                   encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s → reports/factor_clusters_20260907.csv/.json")


if __name__ == "__main__":
    main()
