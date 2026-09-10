# -*- coding: utf-8 -*-
"""核查主库 finance_history / perf_forecast / goodwill_snapshot 等财务历史表覆盖"""
import duckdb

con = duckdb.connect()
try:
    con.execute("ATTACH 'C:/Users/53497/WorkBuddy/2026-09-02-23-42-28/quantlab/data/quant.duckdb' AS q (READ_ONLY)")
except Exception as e:
    print("ATTACH 失败（主库被占）：", e)
    raise SystemExit

tables = [r[0] for r in con.execute(
    "SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()]
print("主库全部表:", tables)

for t in ["finance_history", "perf_forecast", "goodwill_snapshot"]:
    if t not in tables:
        print(f"--- {t}: 不存在")
        continue
    print("=" * 60)
    print(t)
    cols = con.execute(f"DESCRIBE q.{t}").fetchall()
    print("  cols:", [c[0] for c in cols])
    cnt, nc = con.execute(f"SELECT count(*), count(DISTINCT code) FROM q.{t}").fetchone()
    print(f"  rows={cnt:,} codes={nc:,}")
    if "report_period" in [c[0] for c in cols]:
        r = con.execute(f"""
            SELECT min(report_period), max(report_period), count(DISTINCT report_period)
            FROM q.{t}""").fetchone()
        print(f"  report_period: {r[0]} ~ {r[1]}  ({r[2]} 期)")
        rows = con.execute(f"""
            SELECT report_period, count(*) AS n, count(DISTINCT code) AS ncode,
                   sum(CASE WHEN net_profit IS NULL THEN 1 ELSE 0 END) AS np_null
            FROM q.{t} GROUP BY 1 ORDER BY 1""").fetchall() if t == "finance_history" else []
        for x in rows:
            print(f"    {x[0]}  rows={x[1]:,} codes={x[2]:,} np_null={x[3]}")
    if "notice_eff" in [c[0] for c in cols]:
        r = con.execute(f"SELECT count(*) FROM q.{t} WHERE notice_eff IS NULL").fetchone()
        print(f"  notice_eff NULL: {r[0]}")
