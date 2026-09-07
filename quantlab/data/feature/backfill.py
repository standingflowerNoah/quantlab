"""特色数据域历史回补（东财 datacenter 均支持日期范围过滤）
=====================================
覆盖域与口径：
- margin_total    两融余额市场汇总：RPTA_RZRQ_LSHJ，一次性拉全历史
- dragon_tiger    龙虎榜：RPT_DAILYBILLBOARD_DETAILSNEW，按月分段
- block_trade     大宗交易：RPT_DATA_BLOCKTRADE，按月分段
- lockup          解禁历史：RPT_LIFT_STAGE，按年分段
- northbound_daily 北向资金：RPT_MUTUAL_DEAL_HISTORY，按月分段；
  MUTUAL_TYPE 001=沪股通 / 002=深股通，NET_DEAL_AMT 单位百万元→亿元。
  注意：2024-08-16 起交易所停止披露北向每日净买入，此后无数据（机制性断供）。

不可回补域（上游仅提供当日/最新期）：daily_snapshot（腾讯当日接口）、
hot_topic（同花顺当日热点）、holder_num（上游仅最新一期，随季度积累）、
fund_flow_daily（push2his 受限，degraded）。
"""
from __future__ import annotations

import time

import pandas as pd

from ...config import get_logger
from ..store import Store
from ..sources.eastmoney_source import datacenter
from .eastmoney_features import BLOCK_DDL, LOCKUP_DDL, MARGIN_DDL
from .ths_features import NORTH_DDL

log = get_logger(__name__)


def _months(start: str, end: str) -> list[tuple[str, str]]:
    """按自然月切分 [start, end]"""
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    out = []
    cur = s.normalize()
    while cur <= e:
        seg_end = min(cur + pd.offsets.MonthEnd(0), e)
        out.append((cur.strftime("%Y-%m-%d"), seg_end.strftime("%Y-%m-%d")))
        cur = seg_end + pd.Timedelta(days=1)
    return out


def _years(start: str, end: str) -> list[tuple[str, str]]:
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    out = []
    cur = s
    while cur <= e:
        seg_end = min(pd.Timestamp(f"{cur.year}-12-31"), e)
        out.append((cur.strftime("%Y-%m-%d"), seg_end.strftime("%Y-%m-%d")))
        cur = pd.Timestamp(f"{cur.year + 1}-01-01")
    return out


# ── 两融余额（全历史一次性） ────────────────────────────────────────
def backfill_margin(start: str = "2022-01-01", end: str | None = None) -> int:
    end = end or pd.Timestamp.now().strftime("%Y-%m-%d")
    rows = datacenter("RPTA_RZRQ_LSHJ",
                      filter_str=f"(DIM_DATE>='{start}')(DIM_DATE<='{end}')",
                      page_size=500, sort_columns="DIM_DATE", sort_types="1",
                      max_pages=20)
    if not rows:
        log.info("两融回补: 无数据")
        return 0
    df = pd.DataFrame([{
        "date": str(r.get("DIM_DATE", ""))[:10],
        "rzye": float(r.get("RZYE") or 0) / 1e8,
        "rqye": float(r.get("RQYE") or 0) / 1e8,
        "rzrqye": float(r.get("RZRQYE") or 0) / 1e8,
        "rzmre": float(r.get("RZMRE") or 0) / 1e8,
        "rqye_chg": float(r.get("RZRQYECZ") or 0) / 1e8,
    } for r in rows if r.get("DIM_DATE")])
    store = Store()
    store.ensure_table("margin_total", MARGIN_DDL)
    df["date"] = pd.to_datetime(df["date"])
    n = store.upsert(df, "margin_total", ["date"])
    store.set_watermark("margin_total", df["date"].max())
    log.info(f"两融回补: {n} 日（{df['date'].min().date()} ~ {df['date'].max().date()}）")
    return n


# ── 龙虎榜（按月分段） ──────────────────────────────────────────────
def backfill_dragon_tiger(start: str = "2022-01-01",
                          end: str | None = None) -> int:
    end = end or pd.Timestamp.now().strftime("%Y-%m-%d")
    from .dragon_tiger import DDL
    store = Store()
    store.ensure_table("dragon_tiger", DDL)
    total = 0
    for s, e in _months(start, end):
        rows = datacenter(
            "RPT_DAILYBILLBOARD_DETAILSNEW",
            filter_str=f"(TRADE_DATE>='{s}')(TRADE_DATE<='{e}')",
            page_size=500, sort_columns="TRADE_DATE", sort_types="1",
            max_pages=30)
        if not rows:
            continue
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
        } for r in rows if r.get("SECURITY_CODE")])
        if df.empty:
            continue
        df["date"] = pd.to_datetime(df["date"])
        n = store.upsert(df, "dragon_tiger", ["date", "code", "reason"])
        total += n
        store.set_watermark("dragon_tiger", df["date"].max())
        log.info(f"龙虎榜回补 {s}~{e}: {n} 条（累计 {total}）")
    log.info(f"龙虎榜回补完成: 共 {total} 条")
    return total


# ── 大宗交易（按月分段） ────────────────────────────────────────────
def backfill_block_trade(start: str = "2022-01-01",
                         end: str | None = None) -> int:
    end = end or pd.Timestamp.now().strftime("%Y-%m-%d")
    store = Store()
    store.ensure_table("block_trade", BLOCK_DDL)
    total = 0
    for s, e in _months(start, end):
        rows = datacenter(
            "RPT_DATA_BLOCKTRADE",
            filter_str=f"(TRADE_DATE>='{s}')(TRADE_DATE<='{e}')",
            page_size=500, sort_columns="TRADE_DATE", sort_types="1",
            max_pages=30)
        if not rows:
            continue
        df = pd.DataFrame([{
            "date": str(r.get("TRADE_DATE", ""))[:10],
            "code": r.get("SECURITY_CODE", ""),
            "name": r.get("SECURITY_NAME_ABBR", ""),
            "price": r.get("DEAL_PRICE") or 0,
            "close": r.get("CLOSE_PRICE") or 0,
            "premium_pct": round(float(r.get("PREMIUM_RATIO") or 0), 2),
            "vol_wan": float(r.get("DEAL_VOLUME") or 0) / 1e4,
            "amount_wan": float(r.get("DEAL_AMT") or 0) / 1e4,
            "buyer": r.get("BUYER_NAME", ""),
            "seller": r.get("SELLER_NAME", ""),
        } for r in rows if r.get("SECURITY_CODE")])
        if df.empty:
            continue
        df["date"] = pd.to_datetime(df["date"])
        df = df.drop_duplicates(subset=["date", "code", "price", "amount_wan"],
                                keep="first")
        n = store.upsert(df, "block_trade",
                         ["date", "code", "price", "amount_wan"])
        total += n
        store.set_watermark("block_trade", df["date"].max())
        log.info(f"大宗回补 {s}~{e}: {n} 笔（累计 {total}）")
    log.info(f"大宗回补完成: 共 {total} 笔")
    return total


# ── 解禁历史（按年分段） ────────────────────────────────────────────
def backfill_lockup(start: str = "2022-01-01",
                    end: str | None = None) -> int:
    end = end or pd.Timestamp.now().strftime("%Y-%m-%d")
    store = Store()
    store.ensure_table("lockup", LOCKUP_DDL)
    total = 0
    for s, e in _years(start, end):
        rows = datacenter(
            "RPT_LIFT_STAGE",
            filter_str=f"(FREE_DATE>='{s}')(FREE_DATE<='{e}')",
            page_size=500, sort_columns="FREE_DATE", sort_types="1",
            max_pages=60)
        if not rows:
            continue
        df = pd.DataFrame([{
            "code": r.get("SECURITY_CODE", ""),
            "name": r.get("SECURITY_NAME_ABBR", ""),
            "date": str(r.get("FREE_DATE", ""))[:10],
            "shares": float(r.get("FREE_SHARES") or 0) / 1e4,
            "ratio": round(float(r.get("FREE_RATIO") or 0), 4),
            "type": r.get("FREE_SHARES_TYPE", ""),
        } for r in rows if r.get("SECURITY_CODE")])
        if df.empty:
            continue
        df["date"] = pd.to_datetime(df["date"])
        # 上游原始数据存在重复行（同股同日同类型多批），先去重
        df = df.drop_duplicates(subset=["code", "date", "type"], keep="first")
        n = store.upsert(df, "lockup", ["code", "date", "type"])
        total += n
        log.info(f"解禁回补 {s}~{e}: {n} 批次（累计 {total}）")
    log.info(f"解禁回补完成: 共 {total} 批次")
    return total


# ── 北向资金（按月分段；2024-08-16 起披露停止） ─────────────────────
NORTH_CUTOFF = "2024-08-15"   # 最后一个有官方净买入披露的交易日


def backfill_northbound(start: str = "2022-01-01",
                        end: str | None = None) -> int:
    end = end or NORTH_CUTOFF
    store = Store()
    store.ensure_table("northbound_daily", NORTH_DDL)
    # 全表重建：原同花顺口径（疑似成交额）与东财净买入口径不一致，统一为东财
    store.execute("DELETE FROM northbound_daily")
    total = 0
    for s, e in _months(start, end):
        rows = datacenter(
            "RPT_MUTUAL_DEAL_HISTORY",
            filter_str=f"(TRADE_DATE>='{s}')(TRADE_DATE<='{e}')",
            page_size=500, sort_columns="TRADE_DATE", sort_types="1",
            max_pages=10)
        if not rows:
            continue
        rec = {}
        for r in rows:
            d = str(r.get("TRADE_DATE", ""))[:10]
            t = str(r.get("MUTUAL_TYPE") or "")
            net = r.get("NET_DEAL_AMT")
            if net is None:
                continue
            rec.setdefault(d, {})[t] = float(net) / 100.0   # 百万→亿
        if not rec:
            continue
        df = pd.DataFrame([{
            "date": d,
            "hgt_yi": v.get("001"),
            "sgt_yi": v.get("002"),
        } for d, v in sorted(rec.items())
            if v.get("001") is not None or v.get("002") is not None])
        if df.empty:
            continue
        df["date"] = pd.to_datetime(df["date"])
        n = store.upsert(df, "northbound_daily", ["date"])
        total += n
        log.info(f"北向回补 {s}~{e}: {n} 日（累计 {total}）")
    store.set_watermark("northbound_daily", NORTH_CUTOFF)
    log.info(f"北向回补完成: 共 {total} 日（净买入披露止于 {NORTH_CUTOFF}）")
    return total


BACKFILLS = {
    "margin_total": backfill_margin,
    "dragon_tiger": backfill_dragon_tiger,
    "block_trade": backfill_block_trade,
    "lockup": backfill_lockup,
    "northbound_daily": backfill_northbound,
}
