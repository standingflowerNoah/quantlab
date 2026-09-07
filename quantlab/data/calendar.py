"""交易日历：以沪深300 K 线日期序列为准（自维护，免依赖外部日历接口）"""
from __future__ import annotations

import pandas as pd

from ..config import get_logger, BENCHMARK, INDEX_CODES
from .store import Store
from .sources.tdx_source import TdxClient

log = get_logger(__name__)


def refresh_calendar(start: str = "2021-01-01") -> int:
    """从通达信指数K线刷新交易日历（沪深300）"""
    code = BENCHMARK.split(".")[0]
    market = INDEX_CODES[BENCHMARK][1]
    df = TdxClient.instance().index_bars_history(
        code, market, freq="day", start_date=start)
    if df.empty:
        raise RuntimeError("交易日历刷新失败：指数K线为空（检查 tdx 服务器）")
    store = Store()
    store.ensure_table("trade_calendar",
                       "trade_date DATE PRIMARY KEY, year INT, month INT, week INT")
    cal = pd.DataFrame({
        "trade_date": df["date"],
        "year": df["date"].dt.year,
        "month": df["date"].dt.month,
        "week": df["date"].dt.isocalendar().week.astype(int),
    })
    n = store.upsert(cal, "trade_calendar", ["trade_date"])
    log.info(f"交易日历刷新: {len(df)} 个交易日 (至 {cal['trade_date'].max()})")
    return n


def trading_dates(start=None, end=None) -> pd.DatetimeIndex:
    """区间交易日序列"""
    store = Store()
    sql = "SELECT trade_date FROM trade_calendar WHERE 1=1"
    params: list = []
    if start is not None:
        sql += " AND trade_date >= ?"
        params.append(pd.to_datetime(start).date())
    if end is not None:
        sql += " AND trade_date <= ?"
        params.append(pd.to_datetime(end).date())
    df = store.q(sql + " ORDER BY trade_date", params)
    if df.empty:
        return pd.DatetimeIndex([])
    return pd.DatetimeIndex(pd.to_datetime(df["trade_date"]))


def is_trading_day(date=None) -> bool:
    date = pd.Timestamp(date) if date is not None else pd.Timestamp.now()
    store = Store()
    df = store.q("SELECT 1 FROM trade_calendar WHERE trade_date = ?",
                 [date.normalize().date()])
    return not df.empty


def last_trading_day(date=None, offset: int = 0) -> pd.Timestamp:
    """<= date 的第 offset+1 个最近交易日（offset=0 即当日或之前最近）"""
    date = pd.Timestamp(date) if date is not None else pd.Timestamp.now()
    dates = trading_dates(end=date)
    if len(dates) == 0:
        raise RuntimeError("交易日历为空，请先 refresh_calendar()")
    idx = len(dates) - 1 - offset
    return dates[max(0, idx)].normalize()


def next_trading_day(date=None) -> pd.Timestamp:
    date = pd.Timestamp(date) if date is not None else pd.Timestamp.now()
    dates = trading_dates(start=date + pd.Timedelta(days=1))
    if len(dates) == 0:
        raise RuntimeError("找不到下一交易日（日历是否需要刷新？）")
    return dates[0].normalize()
