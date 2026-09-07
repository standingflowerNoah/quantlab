#!/usr/bin/env python3
"""生产模型重构 · 1b：短名单 IC10 衰减 + IC20 日度序列落盘（供报告与复用）"""
from __future__ import annotations

import json
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "reports/prodmodel"
DB = str(ROOT / "data/quant.duckdb").replace("\\", "/")
FACTOR_DIR = ROOT / "data/lake/factor"

con = duckdb.connect()
con.execute(f"ATTACH '{DB}' AS q (READ_ONLY)")
con.execute("""
CREATE TEMP TABLE fwd10 AS
SELECT date, code, c_lead / c - 1 AS fwd
FROM (
    SELECT date, code, close * adj_factor AS c,
           LEAD(close * adj_factor, 10) OVER (
               PARTITION BY code ORDER BY date) AS c_lead
    FROM q.kline_daily)
WHERE c_lead IS NOT NULL AND c > 0""")

IC_SQL = """
WITH j AS (
    SELECT CAST(f.date AS DATE) AS date, f.code, f.value, w.fwd
    FROM read_parquet([{paths}]) f
    JOIN fwd10 w ON CAST(f.date AS DATE) = w.date AND f.code = w.code
    WHERE f.value IS NOT NULL AND isfinite(f.value) AND abs(f.value) < 1e300),
rk AS (
    SELECT date, value, fwd,
           rank() OVER (PARTITION BY date ORDER BY value) AS rv,
           rank() OVER (PARTITION BY date ORDER BY fwd) AS rf,
           count(*) OVER (PARTITION BY date) AS n
    FROM j)
SELECT date, corr(rv, rf) AS ic, max(n) AS n
FROM rk WHERE n >= 300 GROUP BY date ORDER BY date
"""

summ = pd.read_csv(OUT_DIR / "factor_ic20.csv")
ok = summ[(summ["icir"].abs() >= 0.15) & (summ["coverage"] >= 0.60)
          & (summ["n_days"] >= 500)].copy()
ok["absicir"] = ok["icir"].abs()
ok = ok.sort_values("absicir", ascending=False)
short = ok.head(60)["factor"].tolist()
print(f"shortlist {len(short)}")

rows, series_map = [], {}
t0 = time.time()
for i, name in enumerate(short):
    files = sorted((FACTOR_DIR / name).glob("part-*.parquet"))
    paths = ", ".join("'" + str(f).replace("\\", "/") + "'" for f in files)
    try:
        df = con.execute(IC_SQL.format(paths=paths)).fetchdf()
    except Exception as e:
        print(f"  {name}: ERR {e}")
        continue
    ic = df["ic"]
    rows.append({"factor": name,
                 "ic10_mean": round(float(ic.mean()), 5),
                 "ic10_icir": round(float(ic.mean() / ic.std())
                                    if ic.std() > 0 else 0, 4),
                 "ic10_win": round(float((ic > 0).mean()), 4)})
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    series_map[name] = d.set_index("date")["ic"]
    if (i + 1) % 20 == 0:
        print(f"  ... {i+1}/{len(short)} ({time.time()-t0:.0f}s)")

ic10 = pd.DataFrame(rows)
short_df = ok[ok["factor"].isin(short)].merge(ic10, on="factor", how="left")
short_df.to_csv(OUT_DIR / "shortlist.csv", index=False)
pd.to_pickle(series_map, OUT_DIR / "factor_ic20_series.pkl")
print(f"done {time.time()-t0:.0f}s")
