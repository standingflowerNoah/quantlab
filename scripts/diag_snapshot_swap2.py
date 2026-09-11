"""诊断 2：用「总市值 >= 流通市值」硬约束定位列错位，并实测腾讯接口字段。"""
from __future__ import annotations

import duckdb

con = duckdb.connect("data/quant.duckdb", read_only=True)

print("=== A. 逐日：mcap_yi < float_mcap_yi 的股票数（正常应≈0） ===")
print("date         n     mcap<fmcap   占比     全流通(n)  非全流通中错位占比")
for r in con.execute("""
    WITH j AS (
      SELECT d.date, d.code, d.mcap_yi, d.float_mcap_yi,
             f.total_shares, f.float_shares
      FROM daily_snapshot d JOIN finance_snapshot f USING (code)
      WHERE d.mcap_yi > 0 AND d.float_mcap_yi > 0
    )
    SELECT date, count(*) AS n,
           sum(CASE WHEN mcap_yi < float_mcap_yi THEN 1 ELSE 0 END) AS bad,
           round(sum(CASE WHEN mcap_yi < float_mcap_yi THEN 1 ELSE 0 END)*100.0/count(*), 1) AS pct,
           sum(CASE WHEN abs(total_shares - float_shares)/total_shares < 0.001 THEN 1 ELSE 0 END) AS full_circ,
           round(sum(CASE WHEN mcap_yi < float_mcap_yi
                           AND abs(total_shares - float_shares)/total_shares >= 0.001
                     THEN 1 ELSE 0 END)*100.0
                 / nullif(sum(CASE WHEN abs(total_shares - float_shares)/total_shares >= 0.001
                                   THEN 1 ELSE 0 END), 0), 1) AS bad_pct_nonfull
    FROM j GROUP BY 1 ORDER BY 1
""").fetchall():
    print("%s  %5d  %9d  %6s%%  %9d  %15s%%" % r)

print()
print("=== B. 实测腾讯接口原始字段（茅台 600519） ===")
try:
    import urllib.request
    url = "https://qt.gtimg.cn/q=sh600519"
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    data = urllib.request.urlopen(req, timeout=10).read().decode("gbk")
    vals = data.split('"')[1].split("~")
    print(f"  字段总数: {len(vals)}")
    for i, v in enumerate(vals):
        if i <= 50:
            print(f"    [{i:2d}] {v}")
except Exception as e:
    print(f"  接口不可达: {e}")
