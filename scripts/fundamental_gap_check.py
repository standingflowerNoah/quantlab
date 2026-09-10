# -*- coding: utf-8 -*-
"""盘点 mirror 层基本面相关表：行数、覆盖范围、更新水位、缺失诊断"""
import duckdb

con = duckdb.connect()
base = "C:/Users/53497/WorkBuddy/2026-09-02-23-42-28/quantlab/data/lake/clean/mirror/"

tables = {
    "finance_snapshot": ("report_date", "code"),
    "daily_snapshot": ("date", "code"),
    "instruments": (None, "code"),
    "holder_num": ("end_date", "code"),
    "dividend_events": ("date", "code"),
    "kline_daily": ("date", "code"),
}

for t, (dt, cd) in tables.items():
    print("=" * 60)
    print(t)
    try:
        cnt, ncode = con.execute(
            f"SELECT count(*), count(DISTINCT {cd}) FROM read_parquet('{base}{t}.parquet')"
        ).fetchone()
        print(f"  rows={cnt:,}  codes={ncode:,}")
        if dt:
            rng = con.execute(
                f"SELECT min({dt}), max({dt}) FROM read_parquet('{base}{t}.parquet')"
            ).fetchone()
            print(f"  {dt}: {rng[0]} ~ {rng[1]}")
    except Exception as e:
        print("  ERR", e)

# finance_snapshot 细看：报告期分布、公告滞后、关键财务字段缺失率
print("=" * 60)
print("finance_snapshot 诊断")
fs = f"read_parquet('{base}finance_snapshot.parquet')"
rows = con.execute(f"""
    SELECT report_period, count(*) AS n, count(DISTINCT code) AS ncode,
           min(fetched_at) AS first_fetch, max(fetched_at) AS last_fetch
    FROM {fs}
    GROUP BY 1 ORDER BY 1
""").fetchall()
for r in rows:
    print(f"  report_period={r[0]}  rows={r[1]:,}  codes={r[2]:,}  fetched={r[3]}~{r[4]}")

# 关键字段缺失率（最新两期）
rows = con.execute(f"""
    SELECT report_period,
           avg(CASE WHEN revenue IS NULL THEN 1.0 ELSE 0 END) AS rev_null,
           avg(CASE WHEN net_profit IS NULL THEN 1.0 ELSE 0 END) AS np_null,
           avg(CASE WHEN operating_cf IS NULL THEN 1.0 ELSE 0 END) AS ocf_null,
           avg(CASE WHEN net_assets IS NULL THEN 1.0 ELSE 0 END) AS na_null,
           avg(CASE WHEN bvps IS NULL THEN 1.0 ELSE 0 END) AS bvps_null,
           avg(CASE WHEN industry IS NULL OR industry='' THEN 1.0 ELSE 0 END) AS ind_null
    FROM {fs} GROUP BY 1 ORDER BY 1 DESC LIMIT 8
""").fetchall()
for r in rows:
    print(f"  {r[0]} rev_null={r[1]:.1%} np_null={r[2]:.1%} ocf_null={r[3]:.1%} na_null={r[4]:.1%} bvps_null={r[5]:.1%} ind_null={r[6]:.1%}")

# daily_snapshot 覆盖
print("=" * 60)
print("daily_snapshot 诊断")
ds = f"read_parquet('{base}daily_snapshot.parquet')"
rows = con.execute(f"""
    SELECT date, count(*) AS n,
           avg(CASE WHEN pe_ttm IS NULL OR pe_ttm=0 THEN 1.0 ELSE 0 END) AS pe_null,
           avg(CASE WHEN pb IS NULL OR pb=0 THEN 1.0 ELSE 0 END) AS pb_null
    FROM {ds} GROUP BY 1 ORDER BY 1 DESC LIMIT 5
""").fetchall()
for r in rows:
    print(f"  {r[0]} rows={r[1]:,} pe_null/zero={r[2]:.1%} pb_null/zero={r[3]:.1%}")
rows = con.execute(f"SELECT min(date), max(date) FROM {ds}").fetchall()
print("  date range:", rows[0])
