#!/usr/bin/env python3
"""Phase 0b：伪日线构造 + 14:56 执行价隔夜段（分钟口径，2025+）
=================================================================
设计见 research/overnight-execution/研究设计.md §2.5。

时点定义（决策时点 t* = T 日 14:56，1min bar 按起始分钟标注，故 14:56 bar
覆盖 14:56:00–14:56:59，其 close 即 t* 时点价格）：
    open_t      = 09:30 bar open（真实开盘 = 集合竞价价 = 日线 open）
    high/low_t  = [09:30, 14:56] 区间 1min 极值（≤ 真实日线）
    close_t*    = 14:56 bar close == 尾盘买入 fill 价（同口径，无前视）
    vol/amount  = 截至 14:56 累计（≈ 全日 95%，逐日口径一致）
    close_eod   = 15:00 bar close（收盘集合竞价价 = 日线 close，仅对照）

收益段（**除权调整挂在卖出日 T+1**，因为持有期 T→T+1 若遇 T+1 除权，
持有者获得现金/送转，须把次日开盘价还原为除权前等价价格）：
    adj_next_open = next_open*(1+N+R) + D - R*S          （T+1 事件）
    exec_ret      = adj_next_open / close_t* - 1   策略可执行隔夜段（买 14:56 卖次开）
    ref_ret       = adj_next_open / close_eod - 1  日线收盘对照（前视，不可交易）

惯例：in-memory duckdb + 直读 parquet，不碰主库写锁。
输出：data/lake/factor/overnight/pseudo_daily/part-{year}.parquet
      reports/overnight/phase0b_summary.json
"""
from __future__ import annotations

import json
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb

ROOT = Path(__file__).resolve().parent.parent
MIN_GLOB = str(ROOT / "data/lake/clean/kline_1min/**/*.parquet").replace("\\", "/")
DIV = str(ROOT / "data/lake/clean/mirror/dividend_events.parquet").replace("\\", "/")
OUT_DIR = ROOT / "data/lake/factor/overnight/pseudo_daily"
REPORT_DIR = ROOT / "reports/overnight"


def main() -> None:
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute("PRAGMA threads=6")
    con.execute("PRAGMA memory_limit='10GB'")

    print("[1/4] 聚合伪日线（1min → t*=14:56）…")
    con.execute(f"""
    CREATE TEMP TABLE pd AS
    SELECT code,
           CAST(datetime AS DATE) AS date,
           min(CASE WHEN CAST(datetime AS TIME) = TIME '09:30:00' THEN open END) AS open,
           max(high) FILTER (WHERE CAST(datetime AS TIME) <= TIME '14:56:00')      AS high_p,
           min(low)  FILTER (WHERE CAST(datetime AS TIME) <= TIME '14:56:00')      AS low_p,
           any_value(CASE WHEN CAST(datetime AS TIME) = TIME '14:56:00' THEN close END) AS close_t,
           sum(vol)    FILTER (WHERE CAST(datetime AS TIME) <= TIME '14:56:00')    AS vol_p,
           sum(amount) FILTER (WHERE CAST(datetime AS TIME) <= TIME '14:56:00')    AS amount_p,
           any_value(CASE WHEN CAST(datetime AS TIME) = TIME '15:00:00' THEN close END) AS close_eod,
           count(*) AS n_bars
    FROM read_parquet('{MIN_GLOB}')
    GROUP BY code, CAST(datetime AS DATE)
    """)
    n = con.execute("SELECT count(*) FROM pd").fetchone()[0]
    print(f"      伪日线 {n:,} 行，用时 {time.time()-t0:.0f}s")

    print("[2/4] 拼接除权调整与次日开盘 …")
    con.execute(f"""
    CREATE TEMP TABLE ev1 AS
    SELECT code, date, SUM(COALESCE(fenhong,0)) fenhong,
           SUM(COALESCE(songzhuangu,0)) songzhuangu, SUM(COALESCE(peigu,0)) peigu,
           CASE WHEN SUM(COALESCE(peigu,0)) > 0
                THEN SUM(COALESCE(peigu,0)*COALESCE(peigujia,0)) / SUM(COALESCE(peigu,0)) ELSE 0 END peigujia
    FROM read_parquet('{DIV}') GROUP BY code, date
    """)

    con.execute("""
    CREATE TEMP TABLE seg AS
    WITH x AS (
      SELECT p.*,
             LEAD(p.open) OVER (PARTITION BY p.code ORDER BY p.date) AS next_open,
             LEAD(p.date) OVER (PARTITION BY p.code ORDER BY p.date) AS next_date
      FROM pd p
    ),
    y AS (
      SELECT x.*, COALESCE(e.fenhong,0) fh, COALESCE(e.songzhuangu,0) sz,
             COALESCE(e.peigu,0) pg,
             CASE WHEN e.code IS NULL THEN x.next_open
                  ELSE x.next_open * (1 + COALESCE(e.songzhuangu,0) + COALESCE(e.peigu,0))
                       + COALESCE(e.fenhong,0)
                       - COALESCE(e.peigu,0) * COALESCE(e.peigujia,0)
             END AS adj_next_open
      FROM x
      LEFT JOIN ev1 e ON e.code = x.code AND e.date = x.next_date
    )
    SELECT code, date, open, high_p, low_p, close_t, close_eod, vol_p, amount_p, n_bars,
           next_open, next_date, adj_next_open, fh, sz, pg,
           adj_next_open / NULLIF(close_t,0)   - 1 AS exec_ret,
           adj_next_open / NULLIF(close_eod,0) - 1 AS ref_ret,
           next_open / NULLIF(close_t,0) - 1       AS exec_raw,
           close_eod / NULLIF(open,0) - 1          AS intraday_eod,
           close_t / NULLIF(open,0) - 1            AS tail_ret_930_1456,
           close_t / NULLIF(close_eod,0) - 1       AS close_t_vs_eod
    FROM y
    WHERE next_open IS NOT NULL AND close_t IS NOT NULL AND close_t > 0
    """)

    df = con.execute("SELECT * FROM seg ORDER BY code, date").df()
    print(f"[3/4] 落盘 … rows={len(df):,} {df.date.min()} → {df.date.max()}")

    for yr, g in df.groupby(df.date.astype(str).str[:4]):
        p = OUT_DIR / f"part-{yr}.parquet"
        g.to_parquet(p, index=False)
        print(f"      wrote {p.name} rows={len(g):,}")

    ok = df[df.exec_ret.notna() & df.ref_ret.notna()]
    summary = {
        "rows": int(len(df)),
        "codes": int(df.code.nunique()),
        "span": [str(df.date.min()), str(df.date.max())],
        "bars_median_per_code_day": float(df.n_bars.median()),
        "exec_ret_mean_bp": float(ok.exec_ret.mean() * 1e4),
        "ref_ret_mean_bp": float(ok.ref_ret.mean() * 1e4),
        "exec_ret_win_rate": float((ok.exec_ret > 0).mean()),
        "exec_raw_mean_bp": float(ok.exec_raw.mean() * 1e4),
        "tail_ret_930_1456_mean_bp": float(ok.tail_ret_930_1456.mean() * 1e4),
        "intraday_eod_mean_bp": float(ok.intraday_eod.mean() * 1e4),
        "close_t_vs_eod_median_bp": float((ok.close_t_vs_eod * 1e4).median()),
        "ex_adjusted_rows": int(((ok.fh != 0) | (ok.sz != 0) | (ok.pg != 0)).sum()),
        "elapsed_sec": round(time.time() - t0, 1),
    }
    (REPORT_DIR / "phase0b_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
