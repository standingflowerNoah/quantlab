#!/usr/bin/env python3
"""财报回补补充批：kline_daily 宇宙 − instruments 差集（北交所 92x + 沪市漏网 60x）
批号从 200 起，输出 part-2XX.parquet 与主回补同目录，glob 自然合并。
用法：python scripts/fundamental_finance_backfill_extra.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb
import pandas as pd

from fundamental_finance_backfill import (OUT_BASE, TABLES, call_westock,
                                          parse_md_table)

ROOT = Path(__file__).resolve().parent.parent
KD = ROOT / "data/lake/clean/mirror/kline_daily.parquet"
INS = ROOT / "data/lake/clean/mirror/instruments.parquet"
BATCH = 60
START_IDX = 200


def main() -> None:
    con = duckdb.connect()
    diff = con.execute(f"""
        WITH k AS (SELECT DISTINCT code FROM read_parquet('{KD}')),
             i AS (SELECT code FROM read_parquet('{INS}'))
        SELECT code FROM k WHERE code NOT IN (SELECT code FROM i) ORDER BY code
    """).fetch_df()["code"].tolist()
    by_mkt: dict[str, list[str]] = {"sh": [], "sz": [], "bj": []}
    for c in diff:
        if c.startswith("6"):
            by_mkt["sh"].append(c)
        elif c.startswith(("43", "83", "87", "88", "92")):
            by_mkt["bj"].append(c)
        else:
            by_mkt["sz"].append(c)
    print(f"[extra] diff={len(diff)}  sh={len(by_mkt['sh'])} "
          f"sz={len(by_mkt['sz'])} bj={len(by_mkt['bj'])}", flush=True)

    batches: list[tuple[int, list[str]]] = []
    i = START_IDX
    for m in ("sh", "bj", "sz"):
        lst = by_mkt[m]
        for j in range(0, len(lst), BATCH):
            batches.append((i, [m + c for c in lst[j:j + BATCH]]))
            i += 1
    print(f"[extra] batches={len(batches)} (idx {START_IDX}..{i-1})", flush=True)

    t0 = time.time()
    fails = []
    for bidx, bcodes in batches:
        for t in TABLES:
            p = OUT_BASE / t / f"part-{bidx:03d}.parquet"
            if p.exists():
                continue
            out = call_westock(t, bcodes)
            if out is None:
                fails.append((bidx, t))
                print(f"  [FAIL] batch={bidx} table={t}", flush=True)
                continue
            df = parse_md_table(out, None)
            if df is None or len(df) == 0:
                pd.DataFrame(columns=["code"]).to_parquet(p, index=False)
                continue
            df.to_parquet(p, index=False)
        print(f"  {bidx} done  elapsed={time.time()-t0:.0f}s", flush=True)
    print(f"[extra done] fails={fails}", flush=True)


if __name__ == "__main__":
    main()
