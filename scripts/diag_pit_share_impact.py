"""量化 PIT 股本切换的影响：旧口径（finance_snapshot 当前股本）vs 新口径（逐日真值）。

产出：
  1. 覆盖度：kline_daily 每个交易日的股本 join 命中率
  2. size / turnover 的逐日截面 rank 相关性 + 分位迁移
  3. 受影响最大的个股样本（便于人眼核对真伪）
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

con = duckdb.connect(str(ROOT / "data" / "quant.duckdb"), read_only=True)
pd.set_option("display.width", 200)

print("=== 1. 覆盖度：kline_daily 交易日 × 股 能否命中股本 ===")
print(con.execute("""
    SELECT
      count(*) AS kline_rows,
      sum(CASE WHEN sc.code IS NOT NULL THEN 1 ELSE 0 END) AS hit,
      round(sum(CASE WHEN sc.code IS NOT NULL THEN 1 ELSE 0 END)*100.0/count(*), 3) AS hit_pct
    FROM kline_daily k
    LEFT JOIN share_capital_daily sc ON sc.code = k.code AND sc.date = k.date
""").fetchall())

print()
print("=== 2. ASOF 命中率（<= 当日最近一条，实际因子用法）===")
print(con.execute("""
    SELECT count(*) AS n,
           sum(CASE WHEN sc.float_shares IS NOT NULL THEN 1 ELSE 0 END) AS hit
    FROM kline_daily k
    ASOF JOIN share_capital_daily sc ON sc.code = k.code AND k.date >= sc.date
""").fetchall())

print()
print("=== 3. size 因子：旧（当前股本）vs 新（PIT 股本）逐日截面 rank 相关 ===")
df = con.execute("""
    WITH o AS (
      SELECT k.date, k.code,
             LN(k.close * f.float_shares / 1e8) AS size_old
      FROM kline_daily k JOIN finance_snapshot f ON f.code = k.code
      WHERE f.float_shares > 0 AND k.close > 0
    ), n AS (
      SELECT k.date, k.code,
             LN(k.close * sc.float_shares / 1e8) AS size_new
      FROM kline_daily k
      ASOF JOIN share_capital_daily sc ON sc.code = k.code AND k.date >= sc.date
      WHERE sc.float_shares > 0 AND k.close > 0
    ), j AS (
      SELECT o.date, o.code, o.size_old, n.size_new,
             rank() OVER (PARTITION BY o.date ORDER BY o.size_old) AS r_old,
             rank() OVER (PARTITION BY o.date ORDER BY n.size_new) AS r_new
      FROM o JOIN n USING (date, code)
    )
    SELECT date,
           count(*) AS n,
           corr(size_old, size_new) AS pearson,
           corr(r_old, r_new) AS spearman,
           avg(abs(r_old - r_new)) AS mean_rank_shift,
           max(abs(r_old - r_new)) AS max_rank_shift
    FROM j
    GROUP BY 1 ORDER BY 1
""").df()

print(f"  截面数 {len(df)}，样本 {df['n'].iloc[0]} 只/日")
print(f"  Pearson  中位 {df['pearson'].median():.4f}   最小 {df['pearson'].min():.4f}")
print(f"  Spearman 中位 {df['spearman'].median():.4f}   最小 {df['spearman'].min():.4f}")
print(f"  平均 rank 位移 中位 {df['mean_rank_shift'].median():.1f} 位"
      f"（占截面 {df['mean_rank_shift'].median()/df['n'].iloc[0]*100:.1f}%）")
print(f"  最大 rank 位移 中位 {df['max_rank_shift'].median():.0f} 位"
      f"（占截面 {df['max_rank_shift'].median()/df['n'].iloc[0]*100:.1f}%）")

print()
print("=== 4. 位移最大的 15 只（最新截面，看是否真发生过大额增发/送转）===")
top = con.execute("""
    WITH o AS (
      SELECT k.date, k.code, LN(k.close * f.float_shares / 1e8) AS old_v
      FROM kline_daily k JOIN finance_snapshot f ON f.code = k.code
      WHERE f.float_shares > 0 AND k.close > 0
    ), n AS (
      SELECT k.date, k.code, sc.float_shares AS fs_now,
             LN(k.close * sc.float_shares / 1e8) AS new_v
      FROM kline_daily k
      ASOF JOIN share_capital_daily sc ON sc.code = k.code AND k.date >= sc.date
      WHERE sc.float_shares > 0 AND k.close > 0
    ), j AS (
      SELECT o.date, o.code, o.old_v, n.new_v, n.fs_now, f.float_shares AS fs_latest
      FROM o JOIN n USING (date, code)
      JOIN (SELECT MAX(date) d FROM kline_daily) t ON t.d = o.date
      JOIN finance_snapshot f ON f.code = o.code
    )
    SELECT j.date, j.code, i.name,
           round(j.fs_latest/1e8, 2) AS now_shares_yi,
           round(j.fs_now/1e8, 2)    AS then_shares_yi,
           round(j.fs_latest/nullif(j.fs_now,0), 2) AS ratio,
           round(j.old_v - j.new_v, 3) AS size_diff
    FROM j LEFT JOIN instruments i ON i.code = j.code
    ORDER BY abs(j.old_v - j.new_v) DESC LIMIT 15
""").df()
print(top.to_string(index=False))
