"""交易日历：以沪深300 K 线日期序列为准（自维护，免依赖外部日历接口）"""
from __future__ import annotations

import pandas as pd

from ..config import get_logger, BENCHMARK, INDEX_CODES
from .store import Store
from .sources.tdx_source import TdxClient

log = get_logger(__name__)


def refresh_calendar(start: str = "2021-01-01") -> int:
    """刷新交易日历：主源通达信指数K线，失败降级新浪指数（2026-09-10 起）

    2026-09-09/10 事故：TDX 服务器池连续两日全部不可用 → 日历停止滚动，
    数据更新把交易日误判为非交易日静默跳过。故 TDX 失败时自动走新浪
    sh000300 日K兜底（实盘已验证），两源都失败才抛错。
    """
    code = BENCHMARK.split(".")[0]
    market = INDEX_CODES[BENCHMARK][1]
    df = pd.DataFrame()
    try:
        df = TdxClient.instance().index_bars_history(
            code, market, freq="day", start_date=start)
    except Exception as e:
        log.warning(f"[calendar] 通达信不可用，降级新浪指数: {e}")
    if df.empty:
        try:
            from .sources.sina_source import index_daily
            df = index_daily("sh000300", datalen=4000)
            if not df.empty:
                # 收盘前（<16:00）丢弃当日未完成 bar，防半根K线污染日历
                now = pd.Timestamp.now()
                if now.hour < 16:
                    df = df[df["date"] < now.normalize()]
                df = df[df["date"] >= pd.Timestamp(start)]
                log.info(f"[calendar] 新浪兜底成功: {len(df)} 个交易日 "
                         f"(至 {df['date'].max().date()})")
        except Exception as e:
            log.error(f"[calendar] 新浪兜底也失败: {e}")
    if df.empty:
        raise RuntimeError("交易日历刷新失败：通达信与新浪源均不可用")
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
