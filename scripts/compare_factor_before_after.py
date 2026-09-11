"""对比因子重算前后的差异（旧=finance_snapshot 当前股本，新=PIT 逐日股本）。

旧值来自 data/backup_factors_20260911/。
全部聚合在 DuckDB 内完成（11 个因子 × 586 万行，用 pandas 会爆内存）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FACTORS = ["size", "total_mcap", "turnover", "turnover_std_20", "ep", "bp", "sp",
           "ocfp", "chip_vwap_bias_250", "dragon_net_20", "lockup_pressure_60"]
NEW = (ROOT / "data" / "lake" / "factor").as_posix()
OLD = (ROOT / "data" / "backup_factors_20260911").as_posix()

con = duckdb.connect()

print(f"{'factor':<22}{'旧行数':>10}{'新行数':>10}{'共有':>10}"
      f"{'Pearson':>10}{'Spearman':>10}{'均rank位移':>11}{'占截面':>8}")
print("-" * 92)

rows = []
for f in FACTORS:
    q = f"""
    WITH o AS (
      SELECT date, code, value FROM read_parquet('{OLD}/{f}/*.parquet',
             hive_partitioning=false) WHERE value IS NOT NULL
    ), n AS (
      SELECT date, code, value FROM read_parquet('{NEW}/{f}/*.parquet',
             hive_partitioning=false) WHERE value IS NOT NULL
    ), j AS (
      SELECT o.date, o.code, o.value AS vo, n.value AS vn,
             rank() OVER (PARTITION BY o.date ORDER BY o.value) AS ro,
             rank() OVER (PARTITION BY o.date ORDER BY n.value) AS rn
      FROM o JOIN n USING (date, code)
    ), agg AS (
      SELECT count(*) AS n_common, corr(vo, vn) AS pearson, corr(ro, rn) AS spearman,
             avg(abs(ro - rn)) AS shift, count(*) / count(DISTINCT date) AS cs
      FROM j
    )
    SELECT (SELECT count(*) FROM o) AS n_old,
           (SELECT count(*) FROM n) AS n_new,
           agg.* FROM agg
    """
    r = con.execute(q).fetchone()
    if not r or not r[3]:
        print(f"{f:<22}{r[0] if r else 0:>10}{r[1] if r else 0:>10}   ——（无交集）")
        continue
    n_old, n_new, n_common, pear, spear, shift, cs = r
    pct = shift / cs * 100
    print(f"{f:<22}{n_old:>10}{n_new:>10}{n_common:>10}"
          f"{pear:>10.4f}{spear:>10.4f}{shift:>11.1f}{pct:>7.1f}%")
    rows.append({"factor": f, "pearson": pear, "spearman": spear,
                 "rank_shift_pct": pct, "n_common": n_common})

print()
print("=== 按 Spearman 升序（越小=改动越大）===")
for r in sorted(rows, key=lambda x: x["spearman"]):
    print(f"  {r['factor']:<22} Spearman={r['spearman']:.4f}  "
          f"rank位移={r['rank_shift_pct']:.1f}%  样本={r['n_common']:,}")

con.close()
