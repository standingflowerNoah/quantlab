"""诊断 daily_snapshot 的 mcap_yi / float_mcap_yi 列错位。

判据：用 price 反推股本，与 finance_snapshot 的 total_shares / float_shares 比对。
  ts_inf = mcap_yi    * 1e8 / price
  fs_inf = fmcap_yi   * 1e8 / price
若 ts_inf ≈ f.total_shares 且 fs_inf ≈ f.float_shares → 口径正确
若 ts_inf ≈ f.float_shares 且 fs_inf ≈ f.total_shares → 两列互换
"""
from __future__ import annotations

import duckdb

con = duckdb.connect("data/quant.duckdb", read_only=True)

SQL = """
WITH j AS (
  SELECT d.date,
         d.mcap_yi       * 1e8 / nullif(d.price, 0) AS ts_inf,
         d.float_mcap_yi * 1e8 / nullif(d.price, 0) AS fs_inf,
         f.total_shares AS f_ts,
         f.float_shares AS f_fs
  FROM daily_snapshot d
  JOIN finance_snapshot f USING (code)
  WHERE d.price > 0 AND f.total_shares > 0 AND f.float_shares > 0
)
SELECT date,
       count(*) AS n,
       sum(CASE WHEN abs(ts_inf - f_ts) / f_ts < 0.02
                 AND abs(fs_inf - f_fs) / f_fs < 0.02 THEN 1 ELSE 0 END) AS ok_normal,
       sum(CASE WHEN abs(ts_inf - f_fs) / f_fs < 0.02
                 AND abs(fs_inf - f_ts) / f_ts < 0.02 THEN 1 ELSE 0 END) AS swapped,
       sum(CASE WHEN (abs(ts_inf - f_ts) / f_ts >= 0.02 OR abs(fs_inf - f_fs) / f_fs >= 0.02)
                 AND (abs(ts_inf - f_fs) / f_fs >= 0.02 OR abs(fs_inf - f_ts) / f_ts >= 0.02)
                THEN 1 ELSE 0 END) AS neither,
       round(median(abs(ts_inf - f_ts) / f_ts), 5) AS med_err_vs_total,
       round(median(abs(ts_inf - f_fs) / f_fs), 5) AS med_err_vs_float
FROM j
GROUP BY 1
ORDER BY 1
"""

print("date        n     ok_normal  swapped  neither  med_err_vs_total  med_err_vs_float")
for r in con.execute(SQL).fetchall():
    print("%s  %5d  %8d  %7d  %7d  %17s  %17s" % r)

print()
print("=== 抽样：601939 逐日（建行：总股本2616亿 / A股流通95.9亿）===")
for r in con.execute(
    "SELECT date, price, mcap_yi, float_mcap_yi,"
    " mcap_yi*1e8/price AS ts_inf, float_mcap_yi*1e8/price AS fs_inf"
    " FROM daily_snapshot WHERE code='601939' ORDER BY date"
).fetchall():
    print("  ", r)
