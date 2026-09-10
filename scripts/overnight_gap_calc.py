#!/usr/bin/env python3
"""Phase 0a：隔夜段序列（除权除息调整）—— 尾盘隔夜策略基础数据层
=================================================================
产出：日线口径的隔夜段 / 日内段 / 全日收益序列，供 Q1 基础事实与后续
      条件化筛选使用。

定义（T 日）：
    overnight_ret = open_T / ref_close_{T-1} - 1     隔夜段（prev_close→open）
    intraday_ret  = close_T / open_T - 1             日内段
    total_ret     = close_T / ref_close_{T-1} - 1    全日

除权除息调整（关键）：
    kline_daily 价格为不复权。若 T 日为除权除息日（dividend_events.date），
    ref_close_{T-1} 按理论除权参考价调整：
        ref = (prev_close - fenhong + peigu * peigujia) / (1 + songzhuangu + peigu)
    fenhong 单位=每股元（税前）；songzhuangu=每股送转股；peigu=每股配股数。
    未调整时送转样本会出现 -27% 级别的虚假隔夜跳空。

惯例：in-memory duckdb + ATTACH (READ_ONLY)，不碰主库写锁。
输出：data/lake/factor/overnight/daily_segments/part-{year}.parquet
      reports/overnight/phase0a_summary.json
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
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB = str(ROOT / "data/quant.duckdb").replace("\\", "/")
DIV = str(ROOT / "data/lake/clean/mirror/dividend_events.parquet").replace("\\", "/")
OUT_DIR = ROOT / "data/lake/factor/overnight/daily_segments"
REPORT_DIR = ROOT / "reports/overnight"
START = "2022-01-01"


def load_daily(con, source: str) -> None:
    """构造 temp 表 px(code,date,open,high,low,close,vol,amount,board,is_st,list_date)"""
    if source == "db":
        con.execute(f"ATTACH '{DB}' AS q (READ_ONLY)")
        con.execute(f"""
        CREATE TEMP TABLE px AS
        SELECT k.code, k.date, k.open, k.high, k.low, k.close, k.vol, k.amount,
               COALESCE(i.board, '') AS board, COALESCE(i.is_st, FALSE) AS is_st,
               i.list_date
        FROM q.kline_daily k
        LEFT JOIN q.instruments i ON i.code = k.code
        WHERE k.date >= DATE '{START}'
        """)
    else:
        glob_p = str(ROOT / "data/lake/factor/overnight/daily_fsdb/*.parquet").replace("\\", "/")
        con.execute(f"""
        CREATE TEMP TABLE raw AS
        SELECT * FROM read_parquet('{glob_p}')
        """)
        con.execute("""
        CREATE TEMP TABLE px AS
        WITH f AS (
          SELECT *, min(date) OVER (PARTITION BY code) AS first_dt FROM raw
        )
        SELECT code, date, open, high, low, close, vol, amount,
               CASE WHEN substr(code,1,1) IN ('0','3') THEN 'GEM_OR_MAIN'
                    WHEN substr(code,1,1) = '6' THEN 'MAIN'
                    ELSE 'BJ' END AS board,
               COALESCE(is_st, FALSE) AS is_st,
               first_dt AS list_date
        FROM f
        """)
    # 窗口函数（统一在 px 上做）
    con.execute("""
    CREATE TEMP TABLE pxw AS
    SELECT *, LAG(close) OVER w AS prev_close, ROW_NUMBER() OVER w AS rn
    FROM px
    WINDOW w AS (PARTITION BY code ORDER BY date)
    """)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["db", "fsdb"], default="db")
    args = ap.parse_args()

    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_dir = OUT_DIR if args.source == "db" else \
        ROOT / "data/lake/factor/overnight/daily_segments_fsdb"
    out_dir.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute("PRAGMA threads=8")
    print(f"[source] {args.source}")
    load_daily(con, args.source)

    # ── 1. 日线 + 事件 + 股票池属性 ────────────────────────────────
    con.execute("""
    CREATE TEMP TABLE px2 AS
    SELECT code, date, open, high, low, close, vol, amount, board, is_st, list_date,
           prev_close, rn
    FROM pxw
    WHERE date >= DATE '2022-01-01'
    """)

    # 除权除息事件（按除权除息日 T 对齐：调整的是 prev_close）
    con.execute(f"""
    CREATE TEMP TABLE ev AS
    SELECT code, date,
           COALESCE(fenhong, 0)     AS fenhong,
           COALESCE(songzhuangu, 0) AS songzhuangu,
           COALESCE(peigu, 0)       AS peigu,
           COALESCE(peigujia, 0)    AS peigujia
    FROM read_parquet('{DIV}')
    WHERE date >= DATE '{START}'
    """)
    con.execute("""
    CREATE TEMP TABLE ev1 AS
    SELECT code, date, SUM(fenhong) fenhong, SUM(songzhuangu) songzhuangu,
           SUM(peigu) peigu,
           CASE WHEN SUM(peigu) > 0 THEN SUM(peigu * peigujia) / SUM(peigu) ELSE 0 END peigujia
    FROM ev GROUP BY code, date
    """)

    con.execute("""
    CREATE TEMP TABLE seg AS
    SELECT p.code, p.date, p.open, p.high, p.low, p.close, p.vol, p.amount,
           p.board, p.is_st, p.list_date, p.prev_close, p.rn,
           COALESCE(e.fenhong, 0)     AS fenhong,
           COALESCE(e.songzhuangu, 0) AS songzhuangu,
           COALESCE(e.peigu, 0)       AS peigu,
           CASE WHEN e.code IS NULL THEN p.prev_close
                ELSE (p.prev_close - COALESCE(e.fenhong,0) + COALESCE(e.peigu,0)*COALESCE(e.peigujia,0))
                     / NULLIF(1 + COALESCE(e.songzhuangu,0) + COALESCE(e.peigu,0), 0)
           END AS ref_close,
           CASE WHEN e.code IS NULL THEN 0 ELSE 1 END AS is_ex_date
    FROM px2 p
    LEFT JOIN ev1 e ON e.code = p.code AND e.date = p.date
    WHERE p.rn > 1 AND p.prev_close IS NOT NULL
    """)

    # ── 2. 收益段 + 质量标记 ──────────────────────────────────────
    con.execute("""
    CREATE TEMP TABLE seg2 AS
    SELECT code, date, open, high, low, close, vol, amount, board, is_st, list_date,
           prev_close, ref_close, is_ex_date, fenhong, songzhuangu, peigu,
           CASE WHEN ref_close > 0 THEN open / ref_close - 1 END AS overnight_ret,
           CASE WHEN open > 0       THEN close / open - 1      END AS intraday_ret,
           CASE WHEN ref_close > 0 THEN close / ref_close - 1   END AS total_ret,
           CASE WHEN ref_close > 0 THEN prev_close / ref_close - 1 END AS adj_gap,
           (vol IS NULL OR vol <= 0) AS halted,
           CASE
             WHEN code LIKE '688%' OR code LIKE '300%' THEN 0.20
             WHEN board = 'BJ'                          THEN 0.30
             WHEN is_st                                THEN 0.05
             ELSE 0.10
           END AS limit_pct
    FROM seg
    """)

    con.execute("""
    CREATE TEMP TABLE seg3 AS
    SELECT *,
           CASE WHEN prev_close > 0 AND close >= prev_close * (1 + limit_pct) - 0.005 THEN TRUE
                WHEN prev_close > 0 AND close <= prev_close * (1 - limit_pct) + 0.005 THEN TRUE
                ELSE FALSE END AS close_at_limit,
           CASE WHEN ref_close > 0 AND open >= ref_close * (1 + limit_pct) - 0.005 THEN TRUE
                WHEN ref_close > 0 AND open <= ref_close * (1 - limit_pct) + 0.005 THEN TRUE
                ELSE FALSE END AS open_at_limit,
           -- ashare_ex 近似：剔除 ST/北交所/上市不足 120 日
           -- 注：instruments.list_date 当前全为 NULL → 次新过滤仅在有值时生效
           --      is_st 为当前快照（非 PIT），沿用项目 ashare_ex 既有口径
           (COALESCE(is_st, FALSE) = FALSE
            AND substr(code,1,1) NOT IN ('4','8') AND substr(code,1,3) <> '920'
            AND (list_date IS NULL OR date >= list_date + INTERVAL 120 DAY)) AS in_ex,
           (substr(code,1,1) NOT IN ('4','8') AND substr(code,1,3) <> '920') AS in_all_nobj,
           CASE WHEN substr(code,1,1) IN ('0','3') THEN 'SZ'
                WHEN substr(code,1,1) = '6' THEN 'SH'
                ELSE 'BJ' END AS mkt
    FROM seg2
    """)

    df = con.execute("""
        SELECT code, date, open, high, low, close, vol, amount, prev_close, ref_close,
               overnight_ret, intraday_ret, total_ret, adj_gap, is_ex_date,
               halted, close_at_limit, open_at_limit, in_ex, in_all_nobj, mkt, board
        FROM seg3 ORDER BY code, date
    """).df()
    print(f"[segments] rows={len(df):,} codes={df.code.nunique()} "
          f"{df.date.min()} → {df.date.max()}")

    for yr, g in df.groupby(df.date.astype(str).str[:4]):
        p = out_dir / f"part-{yr}.parquet"
        g.to_parquet(p, index=False)
        print(f"  wrote {p.name} rows={len(g):,}")

    # ── 3. 基础摘要（供 Q1 参考，正式 Q1 另跑） ────────────────────
    u = df[df.in_ex & ~df.halted & df.overnight_ret.notna()]
    uall = df[df.in_all_nobj & ~df.halted & df.overnight_ret.notna()]
    summary = {
        "rows_total": int(len(df)),
        "rows_universe": int(len(u)),
        "rows_all_nobj": int(len(uall)),
        "codes": int(df.code.nunique()),
        "span": [str(df.date.min()), str(df.date.max())],
        "ex_dates_adjusted": int((df.is_ex_date == 1).sum()),
        "overall": {
            "overnight_mean_bp": float(u.overnight_ret.mean() * 1e4),
            "intraday_mean_bp": float(u.intraday_ret.mean() * 1e4),
            "total_mean_bp": float(u.total_ret.mean() * 1e4),
            "overnight_median_bp": float(u.overnight_ret.median() * 1e4),
            "overnight_win_rate": float((u.overnight_ret > 0).mean()),
        },
        "overall_all_nobj": {
            "overnight_mean_bp": float(uall.overnight_ret.mean() * 1e4),
            "rows": int(len(uall)),
        },
        "adjusted_rows": int((df.adj_gap.abs() > 1e-9).sum()),
        "elapsed_sec": round(time.time() - t0, 1),
    }
    (REPORT_DIR / f"phase0a_summary_{args.source}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
