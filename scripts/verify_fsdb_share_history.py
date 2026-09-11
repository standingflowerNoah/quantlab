"""验证 fsdb 日线 total_share 的历史真实性（阶梯式变化 = 真历史，常数 = 回填）。

同时测并发拉取速度，为批量回填定容量。
"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quantlab.data.sources import fsdb_source as fs      # noqa: E402

pd.set_option("display.width", 220)


def one(code: str) -> tuple[str, int, bool]:
    try:
        d = fs.day_bars(code, "20000101", "20991231")
        if d is None or d.empty:
            return code, 0, False
        sh = pd.to_numeric(d["total_share"], errors="coerce")
        return code, len(d), sh.nunique() > 1
    except Exception:                                            # noqa: BLE001
        return code, -1, False


CODES = ["600519", "000001", "601939", "300750", "002594", "601318",
         "000002", "600036", "002415", "300059", "688981", "601988",
         "000651", "600030", "002230", "300124", "603259", "601288",
         "000333", "600887"]

print("=== A. 历史 total_share 是否随时间变化（阶梯=真历史）===")
t0 = time.time()
res = []
with ThreadPoolExecutor(max_workers=4) as ex:
    for f in as_completed([ex.submit(one, c) for c in CODES]):
        res.append(f.result())
el = time.time() - t0
for code, n, varies in sorted(res):
    print(f"  {code}  行数={n:6d}  股本随时间变化={'是' if varies else '否(可疑)'}")
print(f"  用时 {el:.1f}s / {len(CODES)} 只  →  单只 {el/len(CODES)*1000:.0f}ms")

print()
print("=== B. 茅台全历史股本阶梯（每变动一次打印）===")
d = fs.day_bars("600519", "20000101", "20991231")
d["date"] = pd.to_datetime(d["date"], format="%Y%m%d")
d["ts"] = pd.to_numeric(d["total_share"], errors="coerce")
d["fs"] = pd.to_numeric(d["float_share"], errors="coerce")
chg = d[(d["ts"].diff().fillna(0) != 0) | (d["fs"].diff().fillna(0) != 0)]
print(f"  总行数 {len(d)}，区间 {d['date'].min().date()} ~ {d['date'].max().date()}")
print(f"  股本变动次数 {len(chg)}")
print(chg[["date", "close", "ts", "fs"]].head(30).to_string(index=False))
