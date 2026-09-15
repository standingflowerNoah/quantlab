#!/usr/bin/env python3
"""单位口径交叉验证：tushare 代理拉数 vs 主库镜像 2026-09-11 行。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
from quantlab.data.sources.xd_tushare import api, to_df  # noqa: E402

TD = "20260911"
M = ROOT / "data" / "lake" / "clean" / "mirror"


def sec(code_col):
    return code_col.str.split(".").str[0]


# ── margin：tushare 分交易所求和 → 对比镜像 ──
f, it = api("margin", {"start_date": TD, "end_date": TD})
m = to_df(f, it)
tot = m.groupby("trade_date")[["rzye", "rqye", "rzrqye", "rzmre"]].sum() / 1e8
print("== margin(tushare 汇总/亿) ==")
print(tot.round(1).to_string())
print("== margin(镜像/亿) ==")
mm = pd.read_parquet(M / "margin_total.parquet")
print(mm[mm["date"] == "2026-09-11"].round(1).to_string())

# ── block_trade：条数对比 ──
f, it = api("block_trade", {"start_date": TD, "end_date": TD})
b = to_df(f, it)
bt = pd.read_parquet(M / "block_trade.parquet")
bt_d = bt[bt["date"] == "2026-09-11"]
print(f"\n== block_trade: tushare {len(b)} 笔 vs 镜像 {len(bt_d)} 笔 ==")
if len(bt_d):
    tc = dict(zip(sec(b["ts_code"]), b["price"]))
    hit = bt_d["code"].isin(tc).sum()
    print(f"  代码交集 {hit}；镜像 amount_wan 中位 {bt_d['amount_wan'].median():.0f}"
          f" vs tushare amount 中位 {b['amount'].median():.0f}")

# ── dragon_tiger：条数与净额对比 ──
f, it = api("top_list", {"trade_date": TD})
t = to_df(f, it)
dt = pd.read_parquet(M / "dragon_tiger.parquet")
dt_d = dt[dt["date"] == "2026-09-11"]
print(f"\n== dragon_tiger: tushare {len(t)} 条 vs 镜像 {len(dt_d)} 条 ==")
if len(dt_d):
    print(f"  镜像 net_buy_wan 中位 {dt_d['net_buy_wan'].median():.0f}"
          f" vs tushare net_amount/1e4 中位 {(t['net_amount']/1e4).median():.0f}")

# ── lockup：单位对比（镜像某行 shares 万股 vs tushare float_share 股） ──
f, it = api("share_float", {"start_date": TD, "end_date": TD})
s = to_df(f, it)
lk = pd.read_parquet(M / "lockup.parquet")
lk_d = lk[lk["date"] == "2026-09-11"]
print(f"\n== lockup: tushare {len(s)} 批 vs 镜像 {len(lk_d)} 批 ==")
if len(s) and len(lk_d):
    j = lk_d.copy()
    j["ts"] = s.set_index(sec(s["ts_code"]))["float_share"].reindex(j["code"]).values
    print(j[["code", "date", "shares", "ratio", "ts"]].head(8).to_string())
