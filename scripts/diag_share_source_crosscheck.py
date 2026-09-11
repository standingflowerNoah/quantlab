"""三方交叉验证流通股本口径：finance_snapshot(通达信) vs share_capital_daily(fsdb)
vs daily_snapshot(已修复的腾讯/新浪快照) vs 腾讯实时接口。

目的：确认 PIT 股本表用的是正确口径，并查明次新股上的分歧谁对谁错。
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pd.set_option("display.width", 240)
con = duckdb.connect(str(ROOT / "data" / "quant.duckdb"), read_only=True)

CODES = ["301507", "301575", "688549", "603370", "301157", "688795",
         "601939", "600519", "300750", "000001"]

print("=== A. 四源对比（最新可得日）===")
df = con.execute("""
    WITH sc AS (
      SELECT code, total_shares, float_shares, date FROM (
        SELECT code, total_shares, float_shares, date,
               row_number() OVER (PARTITION BY code ORDER BY date DESC) rn
        FROM share_capital_daily
      ) WHERE rn = 1
    ), ds AS (
      SELECT code, price, mcap_yi, float_mcap_yi FROM daily_snapshot
      WHERE date = (SELECT MAX(date) FROM daily_snapshot)
    )
    SELECT f.code, i.name,
           f.total_shares/1e8  AS tdx_total_yi,
           f.float_shares/1e8  AS tdx_float_yi,
           sc.total_shares/1e8 AS fsdb_total_yi,
           sc.float_shares/1e8 AS fsdb_float_yi,
           ds.mcap_yi*1e8/ds.price/1e8       AS snap_total_yi,
           ds.float_mcap_yi*1e8/ds.price/1e8 AS snap_float_yi,
           sc.date AS fsdb_last
    FROM finance_snapshot f
    LEFT JOIN sc ON sc.code = f.code
    LEFT JOIN ds ON ds.code = f.code
    LEFT JOIN instruments i ON i.code = f.code
    WHERE f.code IN ({})
    ORDER BY f.code
""".format(",".join(f"'{c}'" for c in CODES))).df()

for _, r in df.iterrows():
    print(f"  {r['code']} {r['name']}")
    print(f"      通达信: 总={r['tdx_total_yi']:>9.3f}亿  流通={r['tdx_float_yi']:>9.3f}亿")
    print(f"      fsdb  : 总={r['fsdb_total_yi']:>9.3f}亿  流通={r['fsdb_float_yi']:>9.3f}亿"
          f"  (至 {r['fsdb_last']})")
    print(f"      快照  : 总={r['snap_total_yi']:>9.3f}亿  流通={r['snap_float_yi']:>9.3f}亿")

print()
print("=== B. 腾讯实时接口对照（第三/第四方裁判）===")
try:
    from quantlab.data.sources.tencent_source import batch_quotes
    t = batch_quotes(CODES)
    for _, r in t.iterrows():
        sh_t = r["mcap_yi"] * 1e8 / r["price"] / 1e8
        sh_f = r["float_mcap_yi"] * 1e8 / r["price"] / 1e8
        print(f"  {r['code']} {r['name']:<8s} 总={sh_t:>9.3f}亿  流通={sh_f:>9.3f}亿"
              f"   (price={r['price']})")
except Exception as e:
    print("  腾讯接口不可达:", e)

print()
print("=== C. 全市场口径分歧统计（最新日，fsdb vs 通达信）===")
print(con.execute("""
    WITH sc AS (
      SELECT code, float_shares FROM (
        SELECT code, float_shares, row_number() OVER (PARTITION BY code ORDER BY date DESC) rn
        FROM share_capital_daily
      ) WHERE rn = 1
    )
    SELECT count(*) AS n,
           sum(CASE WHEN abs(f.float_shares - sc.float_shares)/f.float_shares < 0.01
                    THEN 1 ELSE 0 END) AS agree_1pct,
           sum(CASE WHEN abs(f.float_shares - sc.float_shares)/f.float_shares > 0.10
                    THEN 1 ELSE 0 END) AS disagree_10pct,
           round(median(abs(f.float_shares - sc.float_shares)/f.float_shares), 4) AS med_relerr
    FROM finance_snapshot f JOIN sc USING (code)
    WHERE f.float_shares > 0
""").fetchall())
