# -*- coding: utf-8 -*-
"""P1 质检：westock finance_q + fsdb valuation_daily 回补结果体检
=================================================================
1. 覆盖率：codes / 期数分布 / 关键字段缺失率
2. PIT 合法性：InfoPublDate > EndDate、落在法定披露窗口内
3. 两源交叉验证：westock income vs 主库 finance_history（东财）同报告期
   revenue / net_profit 相关性与中位偏差
4. valuation_daily：对 kline_daily 行覆盖率、pe/pb 缺失率、市值合理性
5. 抽查：茅台 600519 已知披露日
"""
import duckdb

con = duckdb.connect()
FQ = ("C:/Users/53497/WorkBuddy/2026-09-02-23-42-28/quantlab/"
      "data/lake/clean/fundamental/finance_q")
VD = ("C:/Users/53497/WorkBuddy/2026-09-02-23-42-28/quantlab/"
      "data/lake/clean/fundamental/valuation_daily/part-*.parquet")

print("=" * 70)
print("1) finance_q 覆盖率")
for t in ("income", "balance", "cashflow"):
    try:
        q = f"read_parquet('{FQ}/{t}/part-*.parquet')"
        cnt, nc = con.execute(
            f"SELECT count(*), count(DISTINCT code) FROM {q}").fetchone()
        rng = con.execute(
            f"SELECT min(EndDate), max(EndDate) FROM {q}").fetchone()
        # 期数分布
        dist = con.execute(f"""
            SELECT CASE WHEN n>=40 THEN '>=40' WHEN n>=20 THEN '20-39'
                        WHEN n>=8 THEN '8-19' ELSE '<8' END AS bucket,
                   count(*) AS stocks
            FROM (SELECT code, count(*) AS n FROM {q} GROUP BY code)
            GROUP BY 1 ORDER BY 1
        """).fetchall()
        print(f"  {t}: rows={cnt:,} codes={nc:,}  {rng[0]}~{rng[1]}  期数分布={dist}")
    except Exception as e:
        print(f"  {t}: ERR {e}")

print("=" * 70)
print("2) PIT 合法性（income）")
q = f"read_parquet('{FQ}/income/part-*.parquet')"
r = con.execute(f"""
    SELECT
      count(*) AS n,
      sum(CASE WHEN InfoPublDate IS NULL THEN 1 ELSE 0 END) AS pub_null,
      sum(CASE WHEN InfoPublDate <= EndDate THEN 1 ELSE 0 END) AS pub_le_end,
      avg(date_diff('day', EndDate, InfoPublDate)) AS avg_lag_days,
      quantile_cont(date_diff('day', EndDate, InfoPublDate), 0.95) AS p95_lag
    FROM {q}
""").fetchone()
print(f"  rows={r[0]:,}  pub_null={r[1]}  pub<=EndDate(非法)={r[2]}  "
      f"平均滞后={r[3]:.0f}天  p95滞后={r[4]:.0f}天")
# 超法定截止（>250 天）异常
r = con.execute(f"""
    SELECT count(*) FROM {q}
    WHERE date_diff('day', EndDate, InfoPublDate) > 250
""").fetchone()
print(f"  披露滞后>250天（异常/更正）: {r[0]}")

print("  茅台披露日抽查:")
r = con.execute(f"""
    SELECT EndDate, InfoPublDate FROM {q}
    WHERE code='600519' ORDER BY EndDate DESC LIMIT 6
""").fetchall()
for x in r:
    print(f"    报告期 {x[0]} → 披露 {x[1]}")

print("=" * 70)
print("3) 两源交叉验证 westock vs finance_history（东财，2021Q1 起）")
con.execute("ATTACH 'C:/Users/53497/WorkBuddy/2026-09-02-23-42-28/quantlab/data/quant.duckdb' AS qdb (READ_ONLY)")
r = con.execute(f"""
    WITH w AS (
        SELECT code, EndDate AS rp, OperatingRevenue AS rev,
               NPParentCompanyOwners AS np
        FROM {q} WHERE EndDate >= DATE '2021-01-01'
    ),
    e AS (
        SELECT code, report_period AS rp, revenue, net_profit AS np
        FROM qdb.finance_history
        WHERE report_period >= DATE '2021-01-01' AND net_profit IS NOT NULL
    )
    SELECT count(*) AS n,
           avg(CASE WHEN abs(w.np-e.np) < 1e-6 THEN 1.0
                    WHEN abs(w.np-e.np)/NULLIF(abs(e.np),0) < 0.001 THEN 1.0
                    ELSE 0 END) AS np_match,
           avg(CASE WHEN abs(w.rev-e.revenue) < 1e-6 THEN 1.0
                    WHEN abs(w.rev-e.revenue)/NULLIF(abs(e.revenue),0) < 0.001 THEN 1.0
                    ELSE 0 END) AS rev_match
    FROM w JOIN e ON w.code=e.code AND w.rp=e.rp
""").fetchone()
print(f"  可比对行={r[0]:,}  net_profit 一致率={r[1]:.1%}  revenue 一致率={r[2]:.1%}")

print("=" * 70)
print("4) valuation_daily 覆盖与质量")
try:
    r = con.execute(f"""
        SELECT count(*), count(DISTINCT code), min(date), max(date)
        FROM read_parquet('{VD}')
    """).fetchone()
    print(f"  rows={r[0]:,} codes={r[1]:,}  {r[2]} ~ {r[3]}")
    r = con.execute(f"""
        SELECT avg(CASE WHEN pe_ttm IS NULL OR pe_ttm=0 THEN 1.0 ELSE 0 END),
               avg(CASE WHEN pb IS NULL OR pb=0 THEN 1.0 ELSE 0 END),
               avg(CASE WHEN total_mv IS NULL OR total_mv<=0 THEN 1.0 ELSE 0 END)
        FROM read_parquet('{VD}')
    """).fetchone()
    print(f"  pe缺失/零={r[0]:.1%}  pb缺失/零={r[1]:.1%}  市值异常={r[2]:.1%}")
    # 与 kline_daily 对齐抽查（2026-09-08 收盘价 vs close）
    r = con.execute(f"""
        WITH v AS (SELECT code, close, total_mv FROM read_parquet('{VD}')
                   WHERE date = DATE '2026-09-08'),
             k AS (SELECT code, close FROM read_parquet(
                   'C:/Users/53497/WorkBuddy/2026-09-02-23-42-28/quantlab/data/lake/clean/mirror/kline_daily.parquet')
                   WHERE date = DATE '2026-09-08')
        SELECT count(*), avg(CASE WHEN abs(v.close-k.close) < 0.01 THEN 1.0 ELSE 0 END)
        FROM v JOIN k USING (code)
    """).fetchone()
    print(f"  与 kline_daily 2026-09-08 对齐: {r[0]:,} 只, 收盘价一致率={r[1]:.1%}")
except Exception as e:
    print("  ERR", e)
