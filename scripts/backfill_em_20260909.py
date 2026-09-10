#!/usr/bin/env python3
"""应急补 2026-09-09 数据（通达信 K 线故障 + 东财 push2his 限流）
================================================================
背景：2026-09-10 早盘通达信 K 线接口系统性返回空，09-09 收盘数据未入库。
东财 push2his 因短时高频探测被临时限流，改用两条本地/备用源兜底：
  - kline_daily / daily_snapshot  ← free-stockdb 日线（本地引擎，已同步到 09-09，
    含 amount/估值字段，volume=股、amount=元，与 kline_daily 口径直接对齐）
  - index_kline / trade_calendar  ← 新浪指数K线（volume 单位=股，需 ÷100 对齐
    库内 index_kline.volume=手 的通达信口径）

口径核验（2026-09-10 实测）：
  - kline_daily.vol=股、amount=元（amount/vol≈均价）；fsdb 日线同口径，无需换算
  - index_kline.volume=手（库 000300 09-08=173566688 ≈ 东财 173566694 手）
  - adj_factor 沿用 09-08（09-09 除权股极少，tdx 恢复后 update_kline 按新事件重算）
  - 日历只收 date <= 09-09（剔除今天盘中 bar，交易日历只收已完成日）
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

log = get_logger("em_backfill")

TARGET = "2026-09-09"

# 新浪指数 secid 映射（.SH→sh，.SZ→sz）
_SINA_INDEX = {
    "000001.SH": "sh000001", "399001.SZ": "sz399001",
    "399006.SZ": "sz399006", "000300.SH": "sh000300",
    "000905.SH": "sh000905", "000852.SH": "sh000852",
    "000688.SH": "sh000688",
}


def fetch_fsdb_daily(code: str) -> dict | None:
    """fsdb 日线 09-09 单股（含估值字段），失败/无数据返回 None"""
    try:
        d = fs.day_bars(code, "20260909", "20260909")
    except Exception:
        return None
    if d is None or d.empty:
        return None
    r = d.iloc[-1]
    return r.to_dict()


def backfill_kline_and_snapshot(store: Store) -> dict:
    codes = store.q("SELECT code FROM instruments").code.tolist()
    adj = store.q("SELECT code, adj_factor FROM kline_daily "
                  "WHERE date = (SELECT max(date) FROM kline_daily)")
    adj_map = dict(zip(adj["code"], adj["adj_factor"]))
    log.info(f"补日线/快照：instruments {len(codes)} 只，已有 adj_factor "
             f"{len(adj_map)} 只")

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

    log.info(f"fsdb 日线命中 09-09 {len(rows)} 只，缺 {miss} 只，"
             f"耗时 {time.time()-t0:.0f}s")

    n_kline = n_snap = 0
    if rows:
        dk = pd.DataFrame(rows)
        dk = dk[["date", "code", "open", "high", "low", "close",
                 "vol", "amount", "adj_factor"]]
        n_kline = store.upsert(dk, "kline_daily", ["code", "date"])
        wm = store.q("SELECT max(date) FROM kline_daily").iloc[0, 0]
        store.set_watermark("kline_daily", wm)
        log.info(f"kline_daily 补 09-09: {n_kline} 行，水位 {wm}")

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
        store.register_dataset("daily_snapshot", "clean", "fsdb", "daily",
                               "每日估值快照（价格/市值/PE/PB）")
        log.info(f"daily_snapshot 补 09-09: {n_snap} 行")

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
            log.warning(f"新浪指数 {code_suffix} 无 09-09")
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
        log.info(f"index_kline 补 09-09: {n_idx} 个指数，水位 {wm}")

    # 日历：新浪沪深300 全历史（收 <= 09-09 的已完成日）
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
