"""龙虎榜席位明细（东财 RPT_BILLBOARD_DAILYDETAILSBUY，2026-09-07）
=====================================
事件类同家族新信息维度：现有 dragon_tiger 只有榜单汇总（净买入总额），
本表提供【席位维度】——谁在买（机构专用 vs 游资营业部）。

数据事实（2026-09-07 探针实测）：
- 当日 ~66 个榜单 / ~330 条席位记录（买 5 + 卖 5 席位）
- OPERATEDEPT_NAME 含"机构专用"即机构席位（沪深标准命名）
- BUY/SELL/NET 单位元，TOTAL_BUYRIO = 该席位买入占榜上总买入比
- 按交易日拉取，每交易日 1-2 页

存储：DuckDB 表 dragon_seats，键 (code, date, trade_id)。
"""
from __future__ import annotations

import time

import pandas as pd

from ..config import get_logger
from .store import Store
from .sources.eastmoney_source import em_get

log = get_logger(__name__)

URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

DS_DDL = """
    code VARCHAR,
    date DATE,
    dept_name VARCHAR,
    is_inst BOOLEAN,
    buy DOUBLE,
    sell DOUBLE,
    net DOUBLE,
    buy_ratio DOUBLE,
    PRIMARY KEY(code, date, dept_name)
"""

_KEEP = {"SECURITY_CODE": "code", "TRADE_DATE": "date",
         "OPERATEDEPT_NAME": "dept_name", "BUY": "buy", "SELL": "sell",
         "NET": "net", "TOTAL_BUYRIO": "buy_ratio"}


def _fetch_day(day: str, max_pages: int = 6) -> list[dict]:
    rows: list[dict] = []
    page = 1
    while page <= max_pages:
        for attempt in range(3):
            try:
                r = em_get(URL, params={
                    "reportName": "RPT_BILLBOARD_DAILYDETAILSBUY",
                    "columns": "ALL",
                    "filter": f"(TRADE_DATE='{day}')",
                    "pageSize": "500", "pageNumber": str(page),
                    "sortColumns": "SECURITY_CODE", "sortTypes": "1",
                })
                data = (r.json().get("result") or {})
                chunk = data.get("data") or []
                break
            except Exception as e:
                log.warning(f"dragon_seats {day} p{page} 第{attempt+1}次失败: {e}")
                time.sleep(2 * (attempt + 1))
                chunk = None
        if chunk is None:
            raise RuntimeError(f"dragon_seats {day} p{page} 连续失败")
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < 500 or len(rows) >= int(data.get("count") or 0):
            break
        page += 1
        time.sleep(0.2)
    return rows


def _to_frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame([{k: r.get(src) for src, k in _KEEP.items()}
                       for r in rows])
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    for col in ("buy", "sell", "net", "buy_ratio"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["code", "date", "dept_name"])
    df["is_inst"] = df["dept_name"].astype(str).str.contains("机构专用",
                                                             na=False)
    df = df.drop_duplicates(subset=["code", "date", "dept_name"])
    return df


def init_dragon_seats(start: str = "2022-01-01") -> int:
    """按交易日全量拉取席位明细（可断点：已有日期跳过）"""
    store = Store()
    store.ensure_table("dragon_seats", DS_DDL)
    store.register_dataset("dragon_seats", "clean", "eastmoney", "daily",
                           "龙虎榜席位明细（机构/营业部，事件类）")
    days = store.q(f"""
        SELECT DISTINCT CAST(date AS DATE) AS d FROM dragon_tiger
        WHERE date >= '{start}' ORDER BY d""")
    have = set(pd.to_datetime(
        store.q("SELECT DISTINCT date FROM dragon_seats")["date"]).dt.date)
    total, done = 0, 0
    for d in days["d"]:
        d_str = str(d)
        if d in have:
            continue
        rows = _fetch_day(d_str)
        done += 1
        if not rows:
            continue
        df = _to_frame(rows)
        if df.empty:
            continue
        store.upsert(df, "dragon_seats", keys=["code", "date", "dept_name"])
        total += len(df)
        if done % 50 == 0:
            print(f"  dragon_seats {done} 日：累计 {total} 行", flush=True)
    store.set_watermark("dragon_seats", pd.Timestamp.now())
    log.info(f"dragon_seats 全量完成 {total} 行（{done} 个新交易日）")
    return total


def update_dragon_seats(lookback_days: int = 6) -> int:
    """增量：回看最近 N 个自然日的榜单日重拉（幂等覆盖）"""
    store = Store()
    store.ensure_table("dragon_seats", DS_DDL)
    store.register_dataset("dragon_seats", "clean", "eastmoney", "daily",
                           "龙虎榜席位明细（机构/营业部，事件类）")
    start = (pd.Timestamp.now() - pd.Timedelta(days=lookback_days)
             ).strftime("%Y-%m-%d")
    days = store.q(f"""
        SELECT DISTINCT CAST(date AS DATE) AS d FROM dragon_tiger
        WHERE date >= '{start}' ORDER BY d""")
    total = 0
    for d in days["d"]:
        rows = _fetch_day(str(d))
        if not rows:
            continue
        df = _to_frame(rows)
        if df.empty:
            continue
        store.upsert(df, "dragon_seats", keys=["code", "date", "dept_name"])
        total += len(df)
    store.set_watermark("dragon_seats", pd.Timestamp.now())
    log.info(f"dragon_seats 增量 {total} 行")
    return total
