#!/usr/bin/env python3
"""历史分钟特征宽表（minute_feat）回填 —— 按月分块，防内存溢出
=================================================================
背景：minute_feat 此前只有 2025/2026（fsdb 分钟湖边界）。2022-2024 已由
tushare 代理补齐（data/lake/clean/kline_1min/year=2022..2024），本脚本按
**月**分块聚合出这三年的宽表。

为什么按月分块（用户明确要求防 OOM）：
- 单年 1min ≈ 3.0 亿行，6 层 CTE + 多个窗口函数（PARTITION BY code,date），
  DuckDB 默认 memory_limit=80%RAM，与其他进程并存时有 OOM 风险
- 按月切块后单块 ≈ 2,500 万行；显式 SET memory_limit=6GB 并指定
  temp_directory，DuckDB 超限自动落盘而非撑爆内存

用法：
    python scripts/backfill_minute_feat_hist.py --years 2022,2023,2024
    python scripts/backfill_minute_feat_hist.py --years 2022 --mem 4GB --month-chunk 1
"""
from __future__ import annotations

import argparse
import gc
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402
import pandas as pd  # noqa: E402

import quantlab.data.minute_feat as MF  # noqa: E402
from quantlab import config  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(ROOT / "logs" / "backfill_minute_feat_hist.log",
                                                  encoding="utf-8"),
                              logging.StreamHandler(sys.stdout)])
log = logging.getLogger("mf_hist")


def month_chunks(year: int) -> list[tuple[str, str]]:
    out = []
    for m in range(1, 13):
        s = f"{year:04d}-{m:02d}-01"
        nxt = f"{year + 1:04d}-01-01" if m == 12 else f"{year:04d}-{m + 1:02d}-01"
        e = (pd.Timestamp(nxt) - pd.Timedelta(days=1)).date().isoformat()
        out.append((s, e))
    return out


def build_year_chunked(con: duckdb.DuckDBPyConnection, year: int,
                       mem: str, tmp_dir: Path) -> int:
    """单年按月分块聚合 -> 写 part-<year>.parquet。返回行数。"""
    con.execute(f"SET memory_limit='{mem}'")
    con.execute(f"SET temp_directory='{tmp_dir.as_posix()}'")
    con.execute("SET preserve_insertion_order=false")   # 省内存（聚合不依赖行序）

    # 只扫目标年份分区（原 _glob_1min(min_year=Y) 会扫 >=Y 的所有分区，浪费 2/3）
    # 注意 _agg_sql 内部以 _glob_1min(min_year) 调用（可能传 None），参数必须吞掉
    def _single_year_glob(year: int):
        pat = f"'{(config.KLINE_1MIN_DIR / f'year={year}' / 'part-*.parquet').as_posix()}'"

        def _g(*_a, **_k):
            return pat
        return _g
    MF._glob_1min = _single_year_glob(year)

    parts: list[pd.DataFrame] = []
    t_year = time.time()
    for s, e in month_chunks(year):
        t0 = time.time()
        sql = MF._agg_sql(start=s, end=e)
        df = con.execute(sql).df()
        if df.empty:
            log.info(f"  {year}-{s[5:7]}: 0 行（该月无数据/未上市），跳过")
            continue
        df["date"] = pd.to_datetime(df["date"])
        parts.append(df[MF.FEAT_COLS])
        n = len(df)
        del df
        gc.collect()
        log.info(f"  {year}-{s[5:7]}: {n:,} 行  {time.time() - t0:.0f}s"
                 f"  (累计 {sum(len(p) for p in parts):,})")

    if not parts:
        log.warning(f"{year}: 全年无数据")
        return 0
    out = pd.concat(parts, ignore_index=True)
    del parts
    gc.collect()
    out = out.sort_values(["date", "code"]).reset_index(drop=True)
    n = MF._write_year(con, year, out, keep_before=None)
    del out
    gc.collect()
    log.info(f"{year}: 完成 {n:,} 行 × {len(MF.FEAT_COLS)} 列，{time.time() - t_year:.0f}s")
    return n


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--years", required=True, help="逗分隔年份，如 2022,2023,2024")
    p.add_argument("--mem", default="6GB", help="DuckDB memory_limit（默认 6GB）")
    a = p.parse_args()

    if not config.KLINE_1MIN_DIR.exists():
        raise SystemExit("分钟湖不存在")
    MF.FEAT_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = config.CLEAN_DIR / ".duckdb_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()      # 独立连接，不碰 quant.duckdb 写锁
    out: dict[int, int] = {}
    try:
        for y in [int(x) for x in a.years.split(",") if x.strip()]:
            out[y] = build_year_chunked(con, y, a.mem, tmp_dir)
    finally:
        con.close()
    log.info("全部完成: %s", out)


if __name__ == "__main__":
    main()
