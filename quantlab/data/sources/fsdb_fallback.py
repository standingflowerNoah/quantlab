"""日线/快照/指数的自动兜底（主源失败时切换 fsdb + 新浪）
================================================================
背景：2026-09-11 早盘流水线启动时通达信服务器池全部不可用，
kline_daily / index_kline / finance_snapshot 三域 FAIL，湖内日线停在 09-09，
需人工跑 `scripts/backfill_fsdb_daily.py` 才补齐。本模块把那次手工脚本
升级为流水线内的自动降级。

降级映射：
  - kline_daily / daily_snapshot  ← fsdb 日k（21 字段，含估值）
  - index_kline                   ← 新浪指数K线（volume 股→手 ÷100 对齐）
  - trade_calendar                ← 新浪沪深300（只收已完成日）

fsdb 日线口径与 kline_daily 对齐（不复权；vol 单位见下方换算说明）。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from ...config import get_logger
from . import fsdb_source as fs
from . import sina_source as sina

log = get_logger("fsdb_fallback")

KLINE_DDL = """
    date DATE, code VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE,
    close DOUBLE, vol DOUBLE, amount DOUBLE, adj_factor DOUBLE,
    PRIMARY KEY(code, date)"""

SNAPSHOT_DDL = """
    date DATE, code VARCHAR, name VARCHAR, price DOUBLE, last_close DOUBLE,
    change_pct DOUBLE, turnover_pct DOUBLE, pe_ttm DOUBLE, pb DOUBLE,
    mcap_yi DOUBLE, float_mcap_yi DOUBLE, vol_ratio DOUBLE,
    PRIMARY KEY(date, code)"""

INDEX_DDL = """
    date DATE, code VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE,
    close DOUBLE, volume DOUBLE, PRIMARY KEY(code, date)"""

CALENDAR_DDL = "trade_date DATE PRIMARY KEY, year INT, month INT, week INT"

# 新浪指数 secid 映射（.SH→sh，.SZ→sz）
SINA_INDEX = {
    "000001.SH": "sh000001", "399001.SZ": "sz399001",
    "399006.SZ": "sz399006", "000300.SH": "sh000300",
    "000905.SH": "sh000905", "000852.SH": "sh000852",
    "000688.SH": "sh000688",
}


# ── fsdb 日线 → kline_daily / daily_snapshot ──────────────────────
def _fetch_one(code: str, target_c: str) -> dict | None:
    try:
        d = fs.day_bars(code, target_c, target_c)
    except Exception as e:                                   # noqa: BLE001
        log.debug(f"fsdb 日线 {code} 失败: {e}")
        return None
    if d is None or d.empty:
        return None
    r = d.iloc[-1]
    if r.get("code") != code:          # 防串位（工程惯例）
        return None
    return r.to_dict()


def backfill_kline_and_snapshot(store, target: pd.Timestamp,
                                codes: list[str] | None = None,
                                progress_every: int = 1500) -> dict:
    """用 fsdb 日线补齐 kline_daily 与 daily_snapshot（当日）"""
    target_c = target.strftime("%Y%m%d")
    if codes is None:
        codes = store.q("SELECT code FROM instruments").code.tolist()

    adj = store.q("SELECT code, adj_factor FROM kline_daily "
                  "WHERE date = (SELECT max(date) FROM kline_daily)")
    adj_map = dict(zip(adj["code"], adj["adj_factor"]))
    log.info(f"[fallback] fsdb 补日线/快照 {target.date()}："
             f"{len(codes)} 只，已有 adj_factor {len(adj_map)} 只")

    # 引擎健康前置（劣化则自动重启，MTTR≈5s）
    fs.ensure_healthy()
    rows, snap_rows, miss = [], [], 0
    t0 = time.time()
    # 并发线程数可大于引擎上限：fsdb_source 内部信号量把实际并发压到 ≤4
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_fetch_one, c, target_c): c for c in codes}
        for i, fut in enumerate(as_completed(futs), 1):
            code = futs[fut]
            r = fut.result()
            if r is None:
                miss += 1
                continue
            rows.append({
                "date": target, "code": code,
                "open": r["open"], "high": r["high"], "low": r["low"],
                "close": r["close"], "vol": r["volume"], "amount": r["amount"],
                "adj_factor": adj_map.get(code, 1.0),
            })
            snap_rows.append({
                "date": target, "code": code,
                "name": r.get("name"),
                "price": r["close"], "last_close": r.get("pre_close"),
                "change_pct": r.get("pct_chg"),
                "turnover_pct": r.get("turnover"),
                "pe_ttm": r.get("pe_ttm"), "pb": r.get("pb"),
                "mcap_yi": (r.get("total_mv") or 0) / 1e8,
                "float_mcap_yi": (r.get("float_mv") or 0) / 1e8,
                "vol_ratio": r.get("vol_ratio"),
            })
            if i % progress_every == 0:
                log.info(f"[fallback]   进度 {i}/{len(codes)} ({time.time()-t0:.0f}s)")
    log.info(f"[fallback] fsdb 日线命中 {target.date()} {len(rows)} 只，"
             f"缺 {miss} 只，耗时 {time.time()-t0:.0f}s")

    n_kline = n_snap = 0
    store.ensure_table("kline_daily", KLINE_DDL)
    if rows:
        dk = pd.DataFrame(rows)[["date", "code", "open", "high", "low",
                                 "close", "vol", "amount", "adj_factor"]]
        n_kline = store.upsert(dk, "kline_daily", ["code", "date"])
        wm = store.q("SELECT max(date) FROM kline_daily").iloc[0, 0]
        store.set_watermark("kline_daily", wm)
        log.info(f"[fallback] kline_daily 补 {target.date()}: {n_kline} 行，水位 {wm}")

    if snap_rows:
        ds = pd.DataFrame(snap_rows)
        store.ensure_table("daily_snapshot", SNAPSHOT_DDL)
        n_snap = store.upsert(ds, "daily_snapshot", ["date", "code"])
        store.set_watermark("daily_snapshot", target)
        log.info(f"[fallback] daily_snapshot 补 {target.date()}: {n_snap} 行")

    return {"kline": n_kline, "snapshot": n_snap, "miss": miss,
            "codes": len(codes)}


# ── 新浪指数 → index_kline / trade_calendar ───────────────────────
def _sina_index(symbol: str, datalen: int = 800) -> pd.DataFrame:
    df = sina.index_daily(symbol, datalen=datalen)
    if df is None or df.empty:
        return pd.DataFrame()
    return df


def backfill_index_and_calendar(store, target: pd.Timestamp) -> dict:
    """用新浪补齐 index_kline（当日）与 trade_calendar（全历史已完成日）"""
    idx_rows = []
    for code_suffix, symbol in SINA_INDEX.items():
        try:
            df = _sina_index(symbol, datalen=10)
        except Exception as e:                               # noqa: BLE001
            log.warning(f"[fallback] 新浪指数 {code_suffix} 失败: {e}")
            continue
        if df.empty:
            continue
        t = df[df["date"] == target]
        if t.empty:
            log.warning(f"[fallback] 新浪指数 {code_suffix} 无 {target.date()}")
            continue
        r = t.iloc[0]
        idx_rows.append({"date": target, "code": code_suffix,
                         "open": r["open"], "high": r["high"],
                         "low": r["low"], "close": r["close"],
                         "volume": r["volume"]})
    n_idx = 0
    if idx_rows:
        store.ensure_table("index_kline", INDEX_DDL)
        n_idx = store.upsert(pd.DataFrame(idx_rows), "index_kline",
                             ["code", "date"])
        wm = store.q("SELECT max(date) FROM index_kline").iloc[0, 0]
        store.set_watermark("index_kline", wm)
        log.info(f"[fallback] index_kline 补 {target.date()}: {n_idx} 个指数，"
                 f"水位 {wm}")

    n_cal = 0
    try:
        df = _sina_index("sh000300", datalen=4000)
    except Exception as e:                                   # noqa: BLE001
        log.error(f"[fallback] 沪深300 拉取失败，日历跳过: {e}")
        return {"index": n_idx, "calendar": 0}
    if not df.empty:
        cal = pd.DataFrame({"trade_date": df["date"][df["date"] <= target]})
        cal["year"] = cal["trade_date"].dt.year
        cal["month"] = cal["trade_date"].dt.month
        cal["week"] = cal["trade_date"].dt.isocalendar().week.astype(int)
        store.ensure_table("trade_calendar", CALENDAR_DDL)
        store.upsert(cal, "trade_calendar", ["trade_date"])
        n_cal = len(cal)
        log.info(f"[fallback] trade_calendar 刷新: {n_cal} 个交易日"
                 f"（至 {cal['trade_date'].max().date()}）")
    return {"index": n_idx, "calendar": n_cal}


# ── 编排 ──────────────────────────────────────────────────────────
def run(store, target: pd.Timestamp, parts: tuple[str, ...] = (
        "kline", "snapshot", "index", "calendar")) -> dict:
    """执行 fallback。parts 可选 kline/snapshot/index/calendar"""
    out: dict = {}
    if "kline" in parts or "snapshot" in parts:
        r = backfill_kline_and_snapshot(store, target)
        if "kline" in parts:
            out["kline_daily"] = r["kline"]
        if "snapshot" in parts:
            out["daily_snapshot"] = r["snapshot"]
        out["miss"] = r["miss"]
    if "index" in parts or "calendar" in parts:
        r2 = backfill_index_and_calendar(store, target)
        if "index" in parts:
            out["index_kline"] = r2["index"]
        if "calendar" in parts:
            out["trade_calendar"] = r2["calendar"]
    return out
