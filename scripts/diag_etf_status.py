"""ETF 数据现状盘点
================================================================
一次性输出 ETF/基金类数据在各层的覆盖情况：
  1. etf_daily 湖（日线，已回补全史）
  2. kline_1min 是否含 ETF（分钟层）
  3. mirror/daily_snapshot 快照是否含 ETF
  4. fsdb ETF 分钟可用性（实测）
  5. ETF 复权事件覆盖
  6. tushare 代理 xd 分区是否含 ETF
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb                                                # noqa: E402

from quantlab.data.etf import load_etf_daily                 # noqa: E402
from quantlab.data.sources import fsdb_source as fs          # noqa: E402

con = duckdb.connect()


def hr(t: str) -> None:
    print(f"\n{'='*62}\n{t}\n{'='*62}")


# ── 1. ETF 日线湖 ───────────────────────────────────────────
hr("1. ETF 日线湖  data/lake/clean/etf_daily/")
df = load_etf_daily(etf_only=False)
print(f"总行数 {len(df):,} | 标的 {df['code'].nunique()} | "
      f"{df['date'].min().date()} -> {df['date'].max().date()}")
print(f"其中 name 含 ETF：{len(df[df['is_etf']]):,} 行 / "
      f"{df[df['is_etf']]['code'].nunique()} 只")
print("按年行数：")
yr = df.groupby(df["date"].dt.year).size()
print("  " + " | ".join(f"{y}:{n:,}" for y, n in yr.items()))
print("字段非空率：")
nn = df.notna().mean().round(3).sort_values()
for k, v in nn.items():
    print(f"  {k:<14} {v:.3f}")
import subprocess                                        # noqa: E402
sz = subprocess.run(["du", "-sh", "data/lake/clean/etf_daily"],
                    capture_output=True, text=True).stdout.strip()
fs_n = len(list((Path("data/lake/clean/etf_daily")).rglob("*.parquet")))
print(f"磁盘 {sz} | 文件 {fs_n} 个")

# ── 2. kline_1min 是否含 ETF ────────────────────────────────
hr("2. kline_1min 湖 是否含 ETF/基金代码")
try:
    r = con.execute("""
        SELECT count(*) n, count(DISTINCT code) c,
               min(datetime) d0, max(datetime) d1
        FROM read_parquet('data/lake/clean/kline_1min/year=*/*.parquet',
                          hive_partitioning=false)
    """).df()
    print(r.to_string(index=False))
    r2 = con.execute("""
        SELECT CASE
                 WHEN code LIKE '5%' OR code LIKE '1%' THEN '疑似ETF/基金'
                 ELSE '其他' END g, count(DISTINCT code) c
        FROM read_parquet('data/lake/clean/kline_1min/year=*/*.parquet',
                          hive_partitioning=false) GROUP BY 1
    """).df()
    print(r2.to_string(index=False))
except Exception as e:                                    # noqa: BLE001
    print("查询失败:", str(e)[:200])

# ── 3. daily_snapshot / mirror 是否含 ETF ───────────────────
hr("3. mirror/daily_snapshot 快照是否含 ETF")
try:
    r = con.execute("""
        SELECT count(*) n, count(DISTINCT code) c, max(date) d1
        FROM read_parquet('data/lake/clean/mirror/daily_snapshot.parquet')
    """).df()
    print(r.to_string(index=False))
    r = con.execute("""
        SELECT count(DISTINCT code) etf_like
        FROM read_parquet('data/lake/clean/mirror/daily_snapshot.parquet')
        WHERE code LIKE '5%' OR code LIKE '1%'
    """).df()
    print("其中 5x/1x 开头：", int(r["etf_like"][0]), "只")
except Exception as e:                                    # noqa: BLE001
    print("查询失败:", str(e)[:200])

# ── 4. fsdb ETF 分钟实测 ────────────────────────────────────
hr("4. fsdb ETF 分钟可用性（实测）")
for c in ["510300", "159915"]:
    try:
        d = fs.minute_bars(c, "20260910", "20260910")
        n = 0 if d is None else len(d)
        print(f"  {c} 2026-09-10: {n} 根"
              + (f"  {str(d['date'].iloc[0])[:12]} -> {str(d['date'].iloc[-1])[:12]}"
                 if n else ""))
    except Exception as e:                                # noqa: BLE001
        print(f"  {c}: ERR {str(e)[:80]}")
for d0 in ["20240102", "20241231", "20250102"]:
    try:
        x = fs.minute_bars("510300", d0, d0)
        print(f"  510300 {d0}: {0 if x is None else len(x)} 根")
    except Exception as e:                                # noqa: BLE001
        print(f"  510300 {d0}: ERR {str(e)[:60]}")

# ── 5. ETF 复权事件 ─────────────────────────────────────────
hr("5. ETF 复权事件（fsdb 复权表）")
hit = 0
for c in ["510300", "159915", "518880", "512880", "159901", "510500"]:
    try:
        a = fs.adj_factors(c)
        k = 0 if a is None or a.empty else len(a)
        hit += 1 if k else 0
        print(f"  {c}: {k} 条")
    except Exception as e:                                # noqa: BLE001
        print(f"  {c}: ERR {str(e)[:60]}")

# ── 6. tushare 代理 xd 分区是否含 ETF ───────────────────────
hr("6. tushare 代理分钟分区 是否含 ETF")
try:
    r = con.execute("""
        SELECT year, count(DISTINCT code) codes,
               count(*) FILTER (WHERE code LIKE '5%' OR code LIKE '1%') etf_rows
        FROM read_parquet('data/lake/clean/kline_1min/year=*/*xd*.parquet',
                          hive_partitioning=false, union_by_name=true)
        GROUP BY 1 ORDER BY 1
    """).df()
    print(r.to_string(index=False) if len(r) else "（无 xd 分区文件）")
except Exception as e:                                    # noqa: BLE001
    print("查询失败:", str(e)[:200])

con.close()
