"""龙虎榜（东财 datacenter，每日全市场一页拉取）"""
from __future__ import annotations

import pandas as pd

from ...config import get_logger
from ..store import Store
from ..sources.eastmoney_source import datacenter

log = get_logger(__name__)

DDL = """
date DATE, code VARCHAR, name VARCHAR, reason VARCHAR,
net_buy_wan DOUBLE, buy_wan DOUBLE, sell_wan DOUBLE,
close DOUBLE, change_pct DOUBLE, turnover_pct DOUBLE,
PRIMARY KEY(date, code, reason)
"""


def update_dragon_tiger(date=None) -> int:
    from .. import calendar
    if date is None:
        date = calendar.last_trading_day()
    date = pd.Timestamp(date).strftime("%Y-%m-%d")
    rows = datacenter(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        filter_str=f"(TRADE_DATE>='{date}')(TRADE_DATE<='{date}')",
        page_size=500, sort_columns="BILLBOARD_NET_AMT", sort_types="-1",
        max_pages=3)
    if not rows:
        log.info(f"龙虎榜 {date}: 无数据（非交易日或未更新）")
        return 0
    df = pd.DataFrame([{
        "date": str(r.get("TRADE_DATE", ""))[:10],
        "code": r.get("SECURITY_CODE", ""),
        "name": r.get("SECURITY_NAME_ABBR", ""),
        "reason": r.get("EXPLANATION", ""),
        "net_buy_wan": round((r.get("BILLBOARD_NET_AMT") or 0) / 1e4, 1),
        "buy_wan": round((r.get("BILLBOARD_BUY_AMT") or 0) / 1e4, 1),
        "sell_wan": round((r.get("BILLBOARD_SELL_AMT") or 0) / 1e4, 1),
        "close": r.get("CLOSE_PRICE") or 0,
        "change_pct": round(float(r.get("CHANGE_RATE") or 0), 2),
        "turnover_pct": round(float(r.get("TURNOVERRATE") or 0), 2),
    } for r in rows])
    store = Store()
    store.ensure_table("dragon_tiger", DDL)
    store.register_dataset("dragon_tiger", "clean", "eastmoney", "daily",
                           "龙虎榜上榜明细")
    df["date"] = pd.to_datetime(df["date"])
    n = store.upsert(df, "dragon_tiger", ["date", "code", "reason"])
    store.set_watermark("dragon_tiger", date)
    log.info(f"龙虎榜 {date}: {n} 条")
    return n
