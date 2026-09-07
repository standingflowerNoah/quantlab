"""同花顺特色数据：当日热点题材（人工归因标签）+ 北向资金（分钟级+日度自缓存）"""
from __future__ import annotations

import requests
import pandas as pd

from ...config import get_logger
from ..store import Store

log = get_logger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0 Safari/537.36")

# ── 同花顺热点：当日强势股 + 题材归因 ─────────────────────────────
HOT_DDL = """
date DATE, code VARCHAR, name VARCHAR, reason VARCHAR,
change_pct DOUBLE, turnover_pct DOUBLE, amount DOUBLE,
PRIMARY KEY(date, code)
"""


def update_hot_topic(date=None) -> int:
    from .. import calendar
    if date is None:
        date = calendar.last_trading_day()
    date = pd.Timestamp(date).strftime("%Y-%m-%d")
    url = (f"http://zx.10jqka.com.cn/event/api/getharden/date/{date}/"
           f"orderby/date/orderway/desc/charset/GBK/")
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
        data = r.json()
    except Exception as e:
        log.warning(f"同花顺热点失败: {e}")
        return 0
    rows = data.get("data") or []
    if not rows:
        log.info(f"热点 {date}: 无数据")
        return 0
    df = pd.DataFrame([{
        "date": date,
        "code": str(x.get("code", "")).zfill(6),
        "name": x.get("name", ""),
        "reason": x.get("reason", ""),
        "change_pct": float(x.get("zhangfu") or 0),
        "turnover_pct": float(x.get("huanshou") or 0),
        "amount": float(x.get("chengjiaoe") or 0),
    } for x in rows])
    store = Store()
    store.ensure_table("hot_topic", HOT_DDL)
    store.register_dataset("hot_topic", "clean", "ths", "daily",
                           "同花顺当日强势股题材归因")
    df["date"] = pd.to_datetime(df["date"])
    n = store.upsert(df, "hot_topic", ["date", "code"])
    store.set_watermark("hot_topic", date)
    log.info(f"热点 {date}: {n} 只强势股")
    return n


# ── 北向资金（同花顺 hsgtApi 分钟级，收盘快照自缓存为日度） ───────
NORTH_DDL = """
date DATE PRIMARY KEY, hgt_yi DOUBLE, sgt_yi DOUBLE
"""


def update_northbound(date=None) -> int:
    """北向当日累计净买入（收盘值落库，自缓存累积历史）"""
    from .. import calendar
    if date is None:
        date = calendar.last_trading_day()
    url = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
    try:
        r = requests.get(url, headers={"User-Agent": UA,
                                       "Referer": "https://data.hexin.cn/",
                                       "Host": "data.hexin.cn"}, timeout=10)
        d = r.json()
    except Exception as e:
        log.warning(f"北向数据失败: {e}")
        return 0
    hgt, sgt = d.get("hgt") or [], d.get("sgt") or []
    hgt = [x for x in hgt if x is not None]
    sgt = [x for x in sgt if x is not None]
    if not hgt and not sgt:
        log.info("北向: 无数据")
        return 0
    rec = {
        "date": pd.Timestamp(date).normalize(),
        "hgt_yi": float(hgt[-1]) if hgt else None,
        "sgt_yi": float(sgt[-1]) if sgt else None,
    }
    store = Store()
    store.ensure_table("northbound_daily", NORTH_DDL)
    store.register_dataset("northbound_daily", "clean", "ths", "daily",
                           "北向资金当日累计净买入（亿元，自缓存积累）")
    n = store.upsert(pd.DataFrame([rec]), "northbound_daily", ["date"])
    store.set_watermark("northbound_daily", date)
    log.info(f"北向 {date}: 沪股通 {rec['hgt_yi']}亿 / 深股通 {rec['sgt_yi']}亿")
    return n


# ── 题材热度聚合（供因子层使用：当日题材标签词频） ────────────────
def topic_heatmap(date=None) -> pd.DataFrame:
    """当日题材热度：reason 字段按 + 分割统计词频"""
    from .. import calendar
    from ..store import query
    if date is None:
        date = calendar.last_trading_day()
    df = query("SELECT reason FROM hot_topic WHERE date=?",
               [pd.Timestamp(date).date()])
    if df.empty:
        return pd.DataFrame()
    from collections import Counter
    cnt = Counter()
    for r in df["reason"].dropna():
        for tag in str(r).split("+"):
            tag = tag.strip()
            if tag:
                cnt[tag] += 1
    out = pd.DataFrame(cnt.most_common(), columns=["topic", "n_stocks"])
    return out
