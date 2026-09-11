"""修正 daily_snapshot 的 mcap_yi / float_mcap_yi 历史错位 + 全列体检。

背景：tencent_source.batch_quotes 曾把接口 [44](流通市值) / [45](总市值) 写反，
导致腾讯源写入的日期上两列互换（2026-09-03/04/07/08/11 等）。
fsdb/新浪回补的日期（09-01/09-09/09-10）不受影响。

安全措施：修正前先把整表备份为 parquet。
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB = ROOT / "data" / "quant.duckdb"
BACKUP = ROOT / "data" / "lake" / "clean" / "mirror" / "daily_snapshot_backup_20260911.parquet"

# 硬约束：总市值 >= 流通市值（总股本 >= 流通股本）。0.1% 容差防浮点噪声。
BAD = "mcap_yi > 0 AND float_mcap_yi > 0 AND mcap_yi < float_mcap_yi * 0.999"

con = duckdb.connect(str(DB))

print("=== 1. 备份 ===")
back = con.execute("SELECT * FROM daily_snapshot").df()
BACKUP.parent.mkdir(parents=True, exist_ok=True)
back.to_parquet(BACKUP, index=False, compression="zstd")
print(f"  {len(back)} 行 → {BACKUP.relative_to(ROOT)}")

print()
print("=== 2. 修正前：逐日错位行数 ===")
for r in con.execute(f"""
    SELECT date, count(*) n,
           sum(CASE WHEN {BAD} THEN 1 ELSE 0 END) bad
    FROM daily_snapshot GROUP BY 1 ORDER BY 1
""").fetchall():
    print(f"  {r[0]}  n={r[1]:5d}  错位={r[2]:5d}")

print()
print("=== 3. 执行交换（逐行判据，只动违反硬约束的行）===")
n_before = con.execute(f"SELECT count(*) FROM daily_snapshot WHERE {BAD}").fetchone()[0]
con.execute(f"""
    UPDATE daily_snapshot
    SET mcap_yi = CASE WHEN {BAD} THEN float_mcap_yi ELSE mcap_yi END,
        float_mcap_yi = CASE WHEN {BAD} THEN mcap_yi ELSE float_mcap_yi END
""")
n_after = con.execute(f"SELECT count(*) FROM daily_snapshot WHERE {BAD}").fetchone()[0]
print(f"  修正行数: {n_before}  →  残余违反: {n_after}")

print()
print("=== 4. 修正后逐日复核 ===")
for r in con.execute("""
    SELECT date, count(*) n,
           sum(CASE WHEN mcap_yi < float_mcap_yi * 0.999 THEN 1 ELSE 0 END) bad,
           round(median(mcap_yi/nullif(float_mcap_yi,0)), 3) med_ratio
    FROM daily_snapshot GROUP BY 1 ORDER BY 1
""").fetchall():
    print(f"  {r[0]}  n={r[1]:5d}  违反={r[2]:3d}  中位总/流通={r[3]}")

print()
print("=== 5. 抽样核对（建行 总2616亿/A流通95.9亿, 平安 总194亿）===")
for r in con.execute("""
    SELECT date, code, price, mcap_yi, float_mcap_yi
    FROM daily_snapshot WHERE code IN ('601939','601318')
    ORDER BY code, date
""").fetchall():
    print("  ", r)

print()
print("=== 6. 其它列体检：turnover_pct 与 kline_daily 成交量是否自洽 ===")
print("  （换手率 = 成交量(股) / 流通股本；用修正后 float_mcap_yi/price 反推流通股本）")
rows = con.execute("""
    SELECT s.date, s.code, s.turnover_pct, k.vol,
           s.float_mcap_yi * 1e8 / s.price AS float_sh,
           ROUND(k.vol / nullif(s.float_mcap_yi * 1e8 / s.price, 0) * 100, 3) AS turn_from_vol
    FROM daily_snapshot s JOIN kline_daily k ON k.code = s.code AND k.date = s.date
    WHERE s.code IN ('601939','600519','300750') AND s.price > 0 AND k.vol > 0
    ORDER BY s.code, s.date
""").fetchall()
for r in rows:
    print(f"  {r[0]} {r[1]}  turn_pct={r[2]:6}  vol={r[3]:>14.0f}  流通股本={r[4]:>16.0f}  由量算={r[5]}")

con.close()
print("\n完成。")
