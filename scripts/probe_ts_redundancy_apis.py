#!/usr/bin/env python3
"""探测 xiaodefa 代理对冗余候选 API 的支持性（阳性对照 date=20260911）。

代理对无数据接口静默返回空（code=0 rows=0），必须先探测再进注册表。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
from quantlab.data.sources.xd_tushare import api, to_df  # noqa: E402

TD = "20260911"

PROBES = [
    ("daily",        {"trade_date": TD}),
    ("daily_basic",  {"trade_date": TD}),
    ("index_daily",  {"ts_code": "000300.SH", "start_date": TD, "end_date": TD}),
    ("margin",       {"start_date": TD, "end_date": TD}),
    ("block_trade",  {"start_date": TD, "end_date": TD}),
    ("top_list",     {"trade_date": TD}),
    ("share_float",  {"start_date": TD, "end_date": TD}),
    ("stk_holdernumber", {"start_date": "20260901", "end_date": TD}),
    ("forecast",     {"ann_date": TD}),
    ("bak_daily",    {"trade_date": TD}),
]

for name, params in PROBES:
    try:
        f, it = api(name, params, retry=3, timeout=40.0)
        if not it:
            print(f"[{name}] EMPTY（代理无此接口或该日期无数据）")
            continue
        df = to_df(f, it)
        print(f"[{name}] rows={len(df)}")
        print("  fields:", list(df.columns))
        print("  sample:", df.iloc[0].to_dict())
    except Exception as e:  # noqa: BLE001
        print(f"[{name}] ERROR: {str(e)[:120]}")
