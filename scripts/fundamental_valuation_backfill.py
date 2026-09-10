#!/usr/bin/env python3
"""历史日频估值回补（fsdb 日K）→ data/lake/clean/fundamental/valuation_daily/
=============================================================================
背景：daily_snapshot 仅近 10 天；kline_daily 无估值字段。
fsdb 日K 自带 pe_ttm / pb / total_mv / float_mv / float_share / total_share，
覆盖与 kline_daily 同期（2022 起；fsdb 实际更早，2021 起拉取留缓冲）。

产出：data/lake/clean/fundamental/valuation_daily/part-NNN.parquet
  列：code, date, close, pe_ttm, pb, total_mv, float_mv,
      float_share, total_share, is_st, turnover
注意：
  - fsdb pre_close 不可信（复权口径混用），此处不取
  - 不写主库（单写者），直接落 parquet
  - is_st 为 PIT 布尔
用法：python scripts/fundamental_valuation_backfill.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from quantlab.data.sources import fsdb_source as fsdb

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data/lake/clean/fundamental/valuation_daily"
START = "20210101"
CHUNK = 500

KEEP = ["code", "date", "close", "pe_ttm", "pb", "total_mv", "float_mv",
        "float_share", "total_share", "is_st", "turnover"]


def main() -> None:
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    codes = fsdb.all_codes()
    print(f"[fsdb] codes={len(codes):,}，回补 {START} 起日频估值 …", flush=True)

    buf, part, rows_total, fails = [], 0, 0, []
    for i, c in enumerate(codes, 1):
        try:
            d = fsdb.day_bars(c, START)
        except Exception as e:                      # noqa: BLE001
            fails.append((c, str(e)[:80]))
            continue
        if len(d):
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
                  f"elapsed={time.time()-t0:.0f}s", flush=True)

    print(f"[done] rows={rows_total:,} parts={part} fails={len(fails)} "
          f"elapsed={time.time()-t0:.0f}s", flush=True)
    if fails:
        print("  sample fails:", fails[:5], flush=True)


if __name__ == "__main__":
    main()
