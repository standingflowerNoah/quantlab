#!/usr/bin/env python3
"""Phase 0a-alt：fsdb 日线全市场回补（长样本兜底源）
=================================================================
背景：data/quant.duckdb 被单写者长期占用时，kline_daily 不可读；
fsdb 本地引擎（127.0.0.1:7899）日线可与 kline_daily 直接对齐，且自带
PIT 的 is_st / pre_close / float_mv / turnover 字段。

产出：data/lake/factor/overnight/daily_fsdb/part-*.parquet
      列：code, date, open, high, low, close, pre_close, vol, amount,
          is_st, float_mv, turnover
"""
from __future__ import annotations

import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from quantlab.data.sources import fsdb_source as fsdb

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data/lake/factor/overnight/daily_fsdb"
START = "20210601"          # 早于主样本 6 个月，用于次新 120 日过滤缓冲
CHUNK = 500

KEEP = ["code", "date", "open", "high", "low", "close", "pre_close",
        "vol", "amount", "is_st", "float_mv", "turnover"]


def main() -> None:
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    codes = fsdb.all_codes()
    print(f"[fsdb] codes={len(codes):,}，开始回补 {START} 起日线 …")

    buf, part, rows_total, fails = [], 0, 0, []
    for i, c in enumerate(codes, 1):
        try:
            d = fsdb.day_bars(c, START)
        except Exception as e:                      # noqa: BLE001
            fails.append((c, str(e)[:80]))
            continue
        if len(d):
            d = d.rename(columns={"volume": "vol"})
            d = d[[k for k in KEEP if k in d.columns]].copy()
            d["date"] = pd.to_datetime(d["date"].astype("int64").astype(str),
                                       format="%Y%m%d").dt.date
            d["code"] = c
            buf.append(d)
        if i % CHUNK == 0 or i == len(codes):
            if buf:
                g = pd.concat(buf, ignore_index=True)
                p = OUT_DIR / f"part-{part:03d}.parquet"
                g.to_parquet(p, index=False)
                rows_total += len(g)
                part += 1
                buf = []
            print(f"  {i:>5}/{len(codes)}  rows={rows_total:,}  "
                  f"elapsed={time.time()-t0:.0f}s")

    print(f"[done] rows={rows_total:,} parts={part} fails={len(fails)} "
          f"elapsed={time.time()-t0:.0f}s")
    if fails:
        print("  sample fails:", fails[:5])


if __name__ == "__main__":
    main()
