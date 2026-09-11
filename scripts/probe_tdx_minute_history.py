"""通达信服务器分钟数据历史深度探测
=====================================
项目已有 TdxClient（pytdx）。本脚本直接问服务器：
category 8=1分钟、0=5分钟，用大 start 偏移往前翻，看能翻到哪一年。
用法: python scripts/probe_tdx_minute_history.py [code]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from quantlab.data.sources.tdx_source import CATEGORY, TdxClient  # noqa: E402

CODE = sys.argv[1] if len(sys.argv) > 1 else "600000"


def main():
    c = TdxClient.instance()
    for freq, lbl in [("min1", "1分钟"), ("min5", "5分钟")]:
        print(f"\n=== {lbl} (category={CATEGORY[freq]}) {CODE} ===")
        df = c.bars_history(CODE, freq, start_date="2010-01-01",
                            max_rows=400000)
        if df.empty:
            print("  空")
            continue
        print(f"  总行数 {len(df):,}  范围 {df['date'].iloc[0]} → {df['date'].iloc[-1]}"
              f"  ({df['date'].dt.date.nunique()} 个交易日)")
        yr = df.groupby(df["date"].dt.year).size()
        for y, n in yr.items():
            print(f"    {y}: {n:>10,} 行")


if __name__ == "__main__":
    main()
