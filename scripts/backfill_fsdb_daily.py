#!/usr/bin/env python3
"""应急补日线数据（通达信故障时，fsdb+新浪兜底），通用日期版
================================================================
用法: python scripts/backfill_fsdb_daily.py <YYYY-MM-DD>
示例: python scripts/backfill_fsdb_daily.py 2026-09-10

背景：2026-09-11 早盘流水线启动时通达信服务器池全部不可用，
kline_daily/index_kline/finance_snapshot 主源失败，湖内日线停在 09-09。
fsdb（free-stockdb）已同步至 20260910，日线口径与 kline_daily 对齐
（vol=股、amount=元），改走 fsdb 兜底：
  - kline_daily / daily_snapshot  ← fsdb day_bars（含估值字段）
  - index_kline                   ← 新浪指数K线（volume 股→手 ÷100 对齐）
  - trade_calendar                ← 新浪沪深300（只收已完成日）
adj_factor 沿用前一交易日（除权股极少，通达信恢复后 update_kline 按新事件重算）
"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quantlab import config
from quantlab.config import get_logger
from quantlab.data.store import Store
from quantlab.data.sources import fsdb_source as fs

log = get_logger("fsdb_backfill")

TARGET = sys.argv[1] if len(sys.argv) > 1 else None
if not TARGET:
    log.error("用法: python scripts/backfill_fsdb_daily.py <YYYY-MM-DD>")
    sys.exit(1)
TARGET_COMPACT = TARGET.replace("-", "")

# 新浪指数 secid 映射（.SH→sh，.SZ→sz）
_SINA_INDEX = {
    "000001.SH": "sh000001", "399001.SZ": "sz399001",
    "399006.SZ": "sz399006", "000300.SH": "sh000300",
    "000905.SH": "sh000905", "000852.SH": "sh000852",
    "000688.SH": "sh000688",
}


def fetch_fsdb_daily(code: str) -> dict | None:
    """fsdb 日线单股（含估值字段），失败/无数据返回 None"""
    try:
        d = fs.day_bars(code, TARGET_COMPACT, TARGET_COMPACT)
    except Exception:
        return None
    if d is None or d.empty:
        return None
    r = d.iloc[-1]
    # 防串行：每行 code 校验（工程惯例）
    if r.get("code") != code:
        return None
    return r.to_dict()


def backfill_kline_and_snapshot(store: Store) -> dict:
    codes = store.q("SELECT code FROM instruments").code.tolist()
    adj = store.q("SELECT code, adj_factor FROM kline_daily "
                  "WHERE date = (SELECT max(date) FROM kline_daily)")
    adj_map = dict(zip(adj["code"], adj["adj_factor"]))
    log.info(f"补日线/快照 {TARGET}：instruments {len(codes)} 只，"
             f"已有 adj_factor {len(adj_map)} 只")

    rows, snap_rows, miss = [], [], 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_fsdb_daily, c): c for c in codes}
        for i, fut in enumerate(as_completed(futs), 1):
            code = futs[fut]
            r = fut.result()
            if r is None:
                miss += 1
                continue
            rows.append({
                "date": pd.Timestamp(TARGET), "code": code,
                "open": r["open"], "high": r["high"], "low": r["low"],
                "close": r["close"], "vol": r["volume"], "amount": r["amount"],
                "adj_factor": adj_map.get(code, 1.0),
            })
            snap_rows.append({
                "date": pd.Timestamp(TARGET), "code": code,
                "name": r.get("name"),
                "price": r["close"], "last_close": r.get("pre_close"),
                "change_pct": r.get("pct_chg"),
                "turnover_pct": r.get("turnover"),
                "pe_ttm": r.get("pe_ttm"), "pb": r.get("pb"),
                "mcap_yi": (r.get("total_mv") or 0) / 1e8,
                "float_mcap_yi": (r.get("float_mv") or 0) / 1e8,
                "vol_ratio": r.get("vol_ratio"),
            })
            if i % 1000 == 0:
                log.info(f"  进度 {i}/{len(codes)} ({time.time()-t0:.0f}s)")

    log.info(f"fsdb 日线命中 {TARGET} {len(rows)} 只，缺 {miss} 只，"
             f"耗时 {time.time()-t0:.0f}s")

    n_kline = n_snap = 0
    if rows:
        dk = pd.DataFrame(rows)
        dk = dk[["date", "code", "open", "high", "low", "close",
                 "vol", "amount", "adj_factor"]]
        n_kline = store.upsert(dk, "kline_daily", ["code", "date"])
        wm = store.q("SELECT max(date) FROM kline_daily").iloc[0, 0]
        store.set_watermark("kline_daily", wm)
        log.info(f"kline_daily 补 {TARGET}: {n_kline} 行，水位 {wm}")

    if snap_rows:
        ds = pd.DataFrame(snap_rows)
        ds = ds[["date", "code", "name", "price", "last_close", "change_pct",
                 "turnover_pct", "pe_ttm", "pb", "mcap_yi", "float_mcap_yi",
                 "vol_ratio"]]
        store.ensure_table("daily_snapshot", """
            date DATE, code VARCHAR, name VARCHAR, price DOUBLE, last_close DOUBLE,
            change_pct DOUBLE, turnover_pct DOUBLE, pe_ttm DOUBLE, pb DOUBLE,
            mcap_yi DOUBLE, float_mcap_yi DOUBLE, vol_ratio DOUBLE,
            PRIMARY KEY(date, code)""")
        n_snap = store.upsert(ds, "daily_snapshot", ["date", "code"])
        store.set_watermark("daily_snapshot", pd.Timestamp(TARGET))
        log.info(f"daily_snapshot 补 {TARGET}: {n_snap} 行")

    return {"kline": n_kline, "snapshot": n_snap, "miss": miss}


def fetch_sina_index(symbol: str, datalen: int = 800) -> pd.DataFrame:
    """新浪指数日K，返回 date/open/high/low/close/volume(股)"""
    url = ("http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData")
    r = requests.get(url, params={"symbol": symbol, "scale": "240",
                                  "ma": "no", "datalen": str(datalen)},
                     timeout=20, proxies={"http": None, "https": None})
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return pd.DataFrame()
    return pd.DataFrame([{
        "date": pd.to_datetime(x["day"]),
        "open": float(x["open"]), "high": float(x["high"]),
        "low": float(x["low"]), "close": float(x["close"]),
        "volume": float(x["volume"]) / 100.0,   # 股 → 手（对齐通达信口径）
    } for x in data])


def backfill_index_and_calendar(store: Store) -> dict:
    idx_rows = []
    for code_suffix, symbol in _SINA_INDEX.items():
        try:
            df = fetch_sina_index(symbol, datalen=10)
        except Exception as e:
            log.warning(f"新浪指数 {code_suffix} 失败: {e}")
            continue
        t = df[df["date"] == pd.Timestamp(TARGET)]
        if t.empty:
            log.warning(f"新浪指数 {code_suffix} 无 {TARGET}")
            continue
        r = t.iloc[0]
        idx_rows.append({"date": pd.Timestamp(TARGET), "code": code_suffix,
                         "open": r["open"], "high": r["high"], "low": r["low"],
                         "close": r["close"], "volume": r["volume"]})
    n_idx = 0
    if idx_rows:
        dfi = pd.DataFrame(idx_rows)
        store.ensure_table("index_kline", """
            date DATE, code VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE,
            close DOUBLE, volume DOUBLE, PRIMARY KEY(code, date)""")
        store.upsert(dfi, "index_kline", ["code", "date"])
        wm = store.q("SELECT max(date) FROM index_kline").iloc[0, 0]
        store.set_watermark("index_kline", wm)
        n_idx = len(dfi)
        log.info(f"index_kline 补 {TARGET}: {n_idx} 个指数，水位 {wm}")

    # 日历：新浪沪深300 全历史（收 <= TARGET 的已完成日）
    try:
        df = fetch_sina_index("sh000300", datalen=4000)
    except Exception as e:
        log.error(f"沪深300 拉取失败，日历跳过: {e}")
        return {"index": n_idx, "calendar": 0}
    cal = pd.DataFrame({"trade_date": df["date"][df["date"] <= pd.Timestamp(TARGET)]})
    cal["year"] = cal["trade_date"].dt.year
    cal["month"] = cal["trade_date"].dt.month
    cal["week"] = cal["trade_date"].dt.isocalendar().week.astype(int)
    store.ensure_table("trade_calendar",
                       "trade_date DATE PRIMARY KEY, year INT, month INT, week INT")
    store.upsert(cal, "trade_calendar", ["trade_date"])
    log.info(f"trade_calendar 刷新: {len(cal)} 个交易日（至 "
             f"{cal['trade_date'].max().date()}）")
    return {"index": n_idx, "calendar": len(cal)}


def main():
    store = Store()
    store.ensure_table("kline_daily", """
        date DATE, code VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE,
        close DOUBLE, vol DOUBLE, amount DOUBLE, adj_factor DOUBLE,
        PRIMARY KEY(code, date)""")
    r1 = backfill_kline_and_snapshot(store)
    r2 = backfill_index_and_calendar(store)

    kline_max = store.q("SELECT max(date) FROM kline_daily").iloc[0, 0]
    cal_max = store.q("SELECT max(trade_date) FROM trade_calendar").iloc[0, 0]
    snap_max = store.q("SELECT max(date) FROM daily_snapshot").iloc[0, 0]
    log.info("=" * 56)
    log.info(f"补数据结果: 日线 {r1['kline']} 行，快照 {r1['snapshot']} 行，"
             f"指数 {r2['index']} 个，日历 {r2['calendar']} 天")
    log.info(f"校验: kline_daily→{kline_max}，daily_snapshot→{snap_max}，"
             f"trade_calendar→{cal_max}")


if __name__ == "__main__":
    main()
