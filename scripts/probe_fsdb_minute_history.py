"""探测 fsdb 分钟数据的历史深度：现有基础上还能不能向前拿？

对若干老股票按年拉分钟 bar，输出每年行数 / 首末时间。
用法: python scripts/probe_fsdb_minute_history.py [code ...]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from quantlab.data.sources import fsdb_source  # noqa: E402

CODES = sys.argv[1:] or ["000001", "600000", "300750", "002415"]
YEARS = [2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026]

if not fsdb_source.service_alive():
    print("[!] stockdb 服务未启动")
    sys.exit(1)

print(f"fsdb 分钟层历史深度探测 | 样本 {len(CODES)} 只 × {len(YEARS)} 年")
print("-" * 78)
print(f"{'code':<8}{'year':<6}{'rows':>8}  {'first':<20}{'last':<20}")
print("-" * 78)
summary: dict[str, dict[int, int]] = {}
for code in CODES:
    summary[code] = {}
    for y in YEARS:
        try:
            df = fsdb_source.minute_bars(code, f"{y}0101", f"{y}1231")
        except Exception as e:
            print(f"{code:<8}{y:<6}{' ERR':>8}  {str(e)[:40]}")
            continue
        n = 0 if df is None else len(df)
        summary[code][y] = n
        if n:
            ts = df["date"].astype("int64").astype(str)
            first = f"{ts.iloc[0][:4]}-{ts.iloc[0][4:6]}-{ts.iloc[0][6:8]}"
            last = f"{ts.iloc[-1][:4]}-{ts.iloc[-1][4:6]}-{ts.iloc[-1][6:8]}"
        else:
            first = last = "-"
        flag = "" if n else "  <-- 空"
        print(f"{code:<8}{y:<6}{n:>8}  {first:<20}{last:<20}{flag}")

print("-" * 78)
print("按年汇总（有效样本数 / 平均行数）：")
for y in YEARS:
    ns = [v.get(y, 0) for v in summary.values()]
    ok = sum(1 for x in ns if x > 0)
    avg = sum(ns) / len(ns) if ns else 0
    print(f"  {y}: 有数据 {ok}/{len(ns)} 只, 平均 {avg:,.0f} 行")
