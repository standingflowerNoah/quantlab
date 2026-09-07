#!/usr/bin/env python3
"""生产模型重构 · 第一步：全因子池有效性评估
=====================================
对 data/lake/factor 下全部因子计算 20 日前瞻 Rank IC（与生产 20 日调仓对齐）：
- IC 均值 / ICIR / 胜率 / 年度符号稳定性 / 覆盖率 / 数据起点
- 短名单因子补算 IC10/IC5（衰减参考）
- 复用 reports/factor_corr_matrix_20260907.csv 的截面相关矩阵做去冗余（不重算）

惯例：in-memory duckdb + ATTACH READ_ONLY，不碰主库写锁。
输出: reports/prodmodel/factor_ic20.csv, factor_ic20_series.pkl,
      reports/prodmodel/shortlist.csv
"""
from __future__ import annotations

import json
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
FACTOR_DIR = ROOT / "data/lake/factor"
OUT_DIR = ROOT / "reports/prodmodel"
OUT_DIR.mkdir(parents=True, exist_ok=True)

DB = str(ROOT / "data/quant.duckdb").replace("\\", "/")
con = duckdb.connect()
con.execute(f"ATTACH '{DB}' AS q (READ_ONLY)")

# 前瞻收益：h 日 LEAD，全期一次性物化到内存表
con.execute("""
CREATE TEMP TABLE fwd20 AS
SELECT date, code, c_lead / c - 1 AS fwd
FROM (
    SELECT date, code, close * adj_factor AS c,
           LEAD(close * adj_factor, 20) OVER (
               PARTITION BY code ORDER BY date) AS c_lead
    FROM q.kline_daily
)
WHERE c_lead IS NOT NULL AND c > 0
""")
con.execute("""
CREATE TEMP TABLE fwd10 AS
SELECT date, code, c_lead / c - 1 AS fwd
FROM (
    SELECT date, code, close * adj_factor AS c,
           LEAD(close * adj_factor, 10) OVER (
               PARTITION BY code ORDER BY date) AS c_lead
    FROM q.kline_daily
)
WHERE c_lead IS NOT NULL AND c > 0
""")
# 宇宙（ashare_ex：剔 ST/次新/北交所）——IC 只在研究宇宙内计算
codes = con.execute("""
SELECT i.code FROM q.instruments i
WHERE NOT i.is_st AND i.board <> 'BJ'
  AND (i.list_date IS NULL OR i.list_date <= CURRENT_DATE - INTERVAL 140 DAY)
""").fetchdf()
con.execute("CREATE TEMP TABLE uni AS SELECT code FROM codes")
con.execute("CREATE TEMP TABLE fwd20u AS SELECT f.* FROM fwd20 f "
            "SEMI JOIN uni u ON f.code = u.code")
con.execute("CREATE TEMP TABLE fwd10u AS SELECT f.* FROM fwd10 f "
            "SEMI JOIN uni u ON f.code = u.code")
N_UNI = len(codes)
print(f"universe ashare_ex: {N_UNI} codes")

IC_SQL = """
WITH j AS (
    SELECT CAST(f.date AS DATE) AS date, f.code, f.value, w.fwd
    FROM read_parquet([{paths}]) f
    JOIN {fwd} w ON CAST(f.date AS DATE) = w.date AND f.code = w.code
    WHERE f.value IS NOT NULL AND isfinite(f.value)
      AND abs(f.value) < 1e300
),
rk AS (
    SELECT date, value, fwd,
           rank() OVER (PARTITION BY date ORDER BY value) AS rv,
           rank() OVER (PARTITION BY date ORDER BY fwd) AS rf,
           count(*) OVER (PARTITION BY date) AS n
    FROM j
)
SELECT date, corr(rv, rf) AS ic, max(n) AS n
FROM rk WHERE n >= 300
GROUP BY date ORDER BY date
"""


def eval_factor(name: str) -> dict | None:
    files = sorted((FACTOR_DIR / name).glob("part-*.parquet"))
    if not files:
        return None
    paths = ", ".join("'" + str(f).replace("\\", "/") + "'" for f in files)
    if "score" in name:
        return None
    try:
        df = con.execute(IC_SQL.format(paths=paths, fwd="fwd20u")).fetchdf()
    except Exception as e:
        print(f"  {name}: ERR {e}")
        return None
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"])
    ic = df["ic"]
    row = {
        "factor": name,
        "n_days": len(df),
        "data_start": str(df["date"].min().date()),
        "ic_mean": round(float(ic.mean()), 5),
        "ic_std": round(float(ic.std()), 5),
        "icir": round(float(ic.mean() / ic.std()) if ic.std() > 0 else 0, 4),
        "ic_win": round(float((ic > 0).mean()), 4),
        "avg_n": int(df["n"].mean()),
        "coverage": round(float(df["n"].mean()) / N_UNI, 4),
    }
    # 年度符号稳定性
    df["year"] = df["date"].dt.year
    yearly = df.groupby("year")["ic"].mean()
    row["yearly_ic"] = json.dumps({int(y): round(float(v), 5)
                                   for y, v in yearly.items()})
    row["n_years_pos"] = int((yearly > 0).sum())
    row["n_years"] = int(len(yearly))
    return row


def main():
    t0 = time.time()
    names = sorted(p.name for p in FACTOR_DIR.iterdir() if p.is_dir())
    rows, series_map = [], {}
    for i, name in enumerate(names):
        r = eval_factor(name)
        if r is not None:
            rows.append(r)
        if (i + 1) % 40 == 0:
            print(f"  ... {i+1}/{len(names)} ({time.time()-t0:.0f}s)")
    summ = pd.DataFrame(rows)
    summ.to_csv(OUT_DIR / "factor_ic20.csv", index=False)
    print(f"evaluated {len(summ)} factors in {time.time()-t0:.0f}s")

    # 短名单：有效 |ICIR|>=0.15、覆盖>=60%、数据>=2 年（500 交易日）
    ok = summ[(summ["icir"].abs() >= 0.15) & (summ["coverage"] >= 0.60)
              & (summ["n_days"] >= 500)].copy()
    ok["absicir"] = ok["icir"].abs()
    ok = ok.sort_values("absicir", ascending=False)
    short = ok.head(60)["factor"].tolist()

    # 短名单补算 IC10（衰减参考）
    ic10_rows = []
    for name in short:
        files = sorted((FACTOR_DIR / name).glob("part-*.parquet"))
        paths = ", ".join(str(f).replace("\\", "/") for f in files)
        try:
            df = con.execute(IC_SQL.format(paths=paths, fwd="fwd10u")).fetchdf()
        except Exception:
            continue
        ic = df["ic"]
        ic10_rows.append({
            "factor": name,
            "ic10_mean": round(float(ic.mean()), 5),
            "ic10_icir": round(float(ic.mean() / ic.std())
                               if ic.std() > 0 else 0, 4),
            "ic10_win": round(float((ic > 0).mean()), 4),
        })
    ic10 = pd.DataFrame(ic10_rows)
    short_df = ok[ok["factor"].isin(short)].merge(ic10, on="factor",
                                                  how="left")
    short_df.to_csv(OUT_DIR / "shortlist.csv", index=False)

    # 保存全池 IC20 日度序列（供后续合成与贡献度分析复用）
    for name in short:
        files = sorted((FACTOR_DIR / name).glob("part-*.parquet"))
        paths = ", ".join(str(f).replace("\\", "/") for f in files)
        try:
            df = con.execute(IC_SQL.format(paths=paths, fwd="fwd20u")).fetchdf()
            series_map[name] = df.set_index("date")["ic"]
        except Exception:
            pass
    pd.to_pickle(series_map, OUT_DIR / "factor_ic20_series.pkl")
    print(f"shortlist {len(short_df)} factors; done {time.time()-t0:.0f}s")
    print(short_df[["factor", "ic_mean", "icir", "ic_win", "n_years_pos",
                    "ic10_icir"]].to_string(index=False))


if __name__ == "__main__":
    main()
