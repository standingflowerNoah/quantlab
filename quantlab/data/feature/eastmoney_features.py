"""两融汇总 + 解禁 + 大宗交易 + 股东户数（东财 datacenter，市场级日频）"""
from __future__ import annotations

import pandas as pd

from ...config import get_logger
from ..store import Store
from ..sources.eastmoney_source import datacenter

log = get_logger(__name__)

# ── 融资融券（市场级汇总，沪深两市合并） ──────────────────────────
MARGIN_DDL = """
date DATE PRIMARY KEY, rzye DOUBLE, rqye DOUBLE, rzrqye DOUBLE,
rzmre DOUBLE, rqye_chg DOUBLE
"""


def update_margin(date=None, days: int = 5) -> int:
    """两融余额汇总（RPTA_RZRQ_LSHJ 历史合计，拉最近 days 日幂等入库）
    实测字段: DIM_DATE/RZYE/RQYE/RZRQYE/RZMRE/RZRQYECZ（单位元）"""
    from .. import calendar
    if date is None:
        date = calendar.last_trading_day()
    start = (pd.Timestamp(date) - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
    rows = datacenter("RPTA_RZRQ_LSHJ",
                      filter_str=f"(DIM_DATE>='{start}')",
                      page_size=50, sort_columns="DIM_DATE", sort_types="-1",
                      max_pages=1)
    if not rows:
        log.info(f"两融 {date}: 无数据")
        return 0
    df = pd.DataFrame([{
        "date": str(r.get("DIM_DATE", ""))[:10],
        "rzye": float(r.get("RZYE") or 0) / 1e8,      # 融资余额 亿
        "rqye": float(r.get("RQYE") or 0) / 1e8,      # 融券余额 亿
        "rzrqye": float(r.get("RZRQYE") or 0) / 1e8,  # 两融合计 亿
        "rzmre": float(r.get("RZMRE") or 0) / 1e8,    # 融资买入额 亿
        "rqye_chg": float(r.get("RZRQYECZ") or 0) / 1e8,  # 两融余额变动 亿
    } for r in rows if r.get("DIM_DATE")])
    if df.empty:
        return 0
    store = Store()
    store.ensure_table("margin_total", MARGIN_DDL)
    store.register_dataset("margin_total", "clean", "eastmoney", "daily",
                           "两融余额市场汇总（亿元）")
    df["date"] = pd.to_datetime(df["date"])
    n = store.upsert(df, "margin_total", ["date"])
    store.set_watermark("margin_total", df["date"].max())
    log.info(f"两融: {n} 日（最新 {df['date'].max().date()}）")
    return n


# ── 限售解禁 ────────────────────────────────────────────────────────
LOCKUP_DDL = """
code VARCHAR, name VARCHAR, date DATE, shares DOUBLE, ratio DOUBLE,
type VARCHAR, PRIMARY KEY(code, date, type)
"""


def update_lockup(date=None, look_days: int = 1) -> int:
    """解禁日历（按日拉取当日/未来解禁批次）"""
    from .. import calendar
    if date is None:
        date = calendar.last_trading_day()
    start = (pd.Timestamp(date) - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    end = (pd.Timestamp(date) + pd.Timedelta(days=90)).strftime("%Y-%m-%d")
    rows = datacenter(
        "RPT_LIFT_STAGE",
        filter_str=f"(FREE_DATE>='{start}')(FREE_DATE<='{end}')",
        page_size=500, sort_columns="FREE_DATE", sort_types="1", max_pages=4)
    if not rows:
        log.info("解禁: 无数据")
        return 0
    df = pd.DataFrame([{
        "code": r.get("SECURITY_CODE", ""),
        "name": r.get("SECURITY_NAME_ABBR", ""),
        "date": str(r.get("FREE_DATE", ""))[:10],
        "shares": float(r.get("FREE_SHARES") or 0) / 1e4,    # 万股
        "ratio": round(float(r.get("FREE_RATIO") or 0), 4),  # 占总股本比
        "type": r.get("FREE_SHARES_TYPE", ""),
    } for r in rows if r.get("SECURITY_CODE")])
    if df.empty:
        return 0
    store = Store()
    store.ensure_table("lockup", LOCKUP_DDL)
    store.register_dataset("lockup", "clean", "eastmoney", "daily",
                           "限售解禁日历（含未来90天）")
    df["date"] = pd.to_datetime(df["date"])
    n = store.upsert(df, "lockup", ["code", "date"])
    store.set_watermark("lockup", date)
    log.info(f"解禁: {n} 批次（{start}~{end}）")
    return n


# ── 大宗交易 ────────────────────────────────────────────────────────
BLOCK_DDL = """
date DATE, code VARCHAR, name VARCHAR, price DOUBLE, close DOUBLE,
premium_pct DOUBLE, vol_wan DOUBLE, amount_wan DOUBLE,
buyer VARCHAR, seller VARCHAR, PRIMARY KEY(date, code, price, amount_wan)
"""


def update_block_trade(date=None) -> int:
    from .. import calendar
    if date is None:
        date = calendar.last_trading_day()
    date = pd.Timestamp(date).strftime("%Y-%m-%d")
    rows = datacenter(
        "RPT_DATA_BLOCKTRADE",
        filter_str=f"(TRADE_DATE>='{date}')(TRADE_DATE<='{date}')",
        page_size=500, sort_columns="DEAL_AMT", sort_types="-1", max_pages=3)
    if not rows:
        log.info(f"大宗交易 {date}: 无数据")
        return 0
    out = []
    for r in rows:
        close = r.get("CLOSE_PRICE") or 0
        price = r.get("DEAL_PRICE") or 0
        out.append({
            "date": str(r.get("TRADE_DATE", ""))[:10],
            "code": r.get("SECURITY_CODE", ""),
            "name": r.get("SECURITY_NAME_ABBR", ""),
            "price": price, "close": close,
            "premium_pct": round(float(r.get("PREMIUM_RATIO") or 0), 2),
            "vol_wan": float(r.get("DEAL_VOLUME") or 0) / 1e4,
            "amount_wan": float(r.get("DEAL_AMT") or 0) / 1e4,
            "buyer": r.get("BUYER_NAME", ""),
            "seller": r.get("SELLER_NAME", ""),
        })
    df = pd.DataFrame(out)
    store = Store()
    store.ensure_table("block_trade", BLOCK_DDL)
    store.register_dataset("block_trade", "clean", "eastmoney", "daily",
                           "大宗交易明细")
    df["date"] = pd.to_datetime(df["date"])
    # 主键 (date,code,price,amount_wan) 不含买卖方：同股同日同价同额的多笔
    # 大宗会在 upsert 时触发主键冲突，先按主键去重（保留首条）
    keys = ["date", "code", "price", "amount_wan"]
    df = df.drop_duplicates(subset=keys, keep="first")
    n = store.upsert(df, "block_trade", keys)
    store.set_watermark("block_trade", date)
    log.info(f"大宗交易 {date}: {n} 笔")
    return n


# ── 股东户数（季度，全市场分页） ──────────────────────────────────
HOLDER_DDL = """
code VARCHAR, name VARCHAR, date DATE, holder_num DOUBLE,
change_ratio DOUBLE, avg_shares DOUBLE, PRIMARY KEY(code, date)
"""


def update_holder_num(max_pages: int = 12) -> int:
    """股东户数最新一期（全市场分页拉取，季度更新）"""
    rows = datacenter("RPT_HOLDERNUMLATEST", page_size=500,
                      sort_columns="END_DATE", sort_types="-1",
                      max_pages=max_pages)
    if not rows:
        log.info("股东户数: 无数据")
        return 0
    df = pd.DataFrame([{
        "code": r.get("SECURITY_CODE", ""),
        "name": r.get("SECURITY_NAME_ABBR", ""),
        "date": str(r.get("END_DATE", ""))[:10],
        "holder_num": float(r.get("HOLDER_NUM") or 0),
        "change_ratio": round(float(r.get("HOLDER_NUM_RATIO") or 0), 2),
        "avg_shares": float(r.get("AVG_FREE_SHARES") or 0),
    } for r in rows if r.get("SECURITY_CODE")])
    if df.empty:
        return 0
    store = Store()
    store.ensure_table("holder_num", HOLDER_DDL)
    store.register_dataset("holder_num", "clean", "eastmoney", "quarterly",
                           "股东户数（最新一期）")
    df["date"] = pd.to_datetime(df["date"])
    n = store.upsert(df, "holder_num", ["code", "date"])
    store.set_watermark("holder_num", df["date"].max())
    log.info(f"股东户数: {n} 只")
    return n
