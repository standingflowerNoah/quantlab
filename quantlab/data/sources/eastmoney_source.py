"""东方财富数据源：特色数据（东财独有，必须限流）
=====================================
- 统一 em_get() 串行限流入口：最小间隔 + 随机抖动 + 会话复用
- datacenter API：龙虎榜/解禁/两融/大宗/股东户数/分红
- push2his：个股资金流（日级120日/分钟级）
- push2：板块行情、实时快照（备用）
"""
from __future__ import annotations

import random
import time

import pandas as pd
import requests

from ... import config
from ...config import get_logger

log = get_logger(__name__)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

_EM_SESSION = requests.Session()
_EM_SESSION.headers.update({"User-Agent": UA})
_em_last = [0.0]


def em_get(url: str, params: dict | None = None, headers: dict | None = None,
           timeout: int = config.EM_TIMEOUT, **kw) -> requests.Response:
    """东财统一请求入口：串行限流 + 会话复用（所有 eastmoney 请求必须走这里）"""
    wait = config.EM_MIN_INTERVAL - (time.time() - _em_last[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.05, 0.4))
    try:
        return _EM_SESSION.get(url, params=params, headers=headers,
                               timeout=timeout, **kw)
    finally:
        _em_last[0] = time.time()


def datacenter(report_name: str, filter_str: str = "", columns: str = "ALL",
               page_size: int = 500, sort_columns: str = "",
               sort_types: str = "-1", max_pages: int = 1) -> list[dict]:
    """东财数据中心统一查询（龙虎榜/解禁/两融/大宗/股东户数/分红 共用）"""
    out = []
    for page in range(1, max_pages + 1):
        params = {
            "reportName": report_name, "columns": columns,
            "filter": filter_str, "pageNumber": str(page),
            "pageSize": str(page_size),
            "sortColumns": sort_columns, "sortTypes": sort_types,
            "source": "WEB", "client": "WEB",
        }
        try:
            r = em_get(DATACENTER_URL, params=params, timeout=20)
            d = r.json()
        except Exception as e:
            log.warning(f"datacenter {report_name} page{page} 失败: {e}")
            time.sleep(2)
            continue
        data = (d.get("result") or {}).get("data") or []
        out.extend(data)
        pages = int((d.get("result") or {}).get("pages") or 1)
        if page >= pages or not data:
            break
    return out


def secid(code: str) -> str:
    """东财 secid：1.600519（沪） 0.000001（深） 0.830000（北交）"""
    return f"1.{code}" if code.startswith(("6", "9")) else f"0.{code}"


# ── 日 K 线（push2his，通达信 K 线故障时的兜底源） ────────────────
def _kline_get(secid_str: str, lmt: int = 800, fqt: int = 0) -> list[str]:
    """push2his 日K通用请求，返回 klines 字符串列表"""
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid_str, "klt": "101", "fqt": str(fqt),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "end": "20500101", "lmt": str(lmt),
    }
    headers = {"Referer": "https://quote.eastmoney.com/",
               "Origin": "https://quote.eastmoney.com"}
    r = em_get(url, params=params, headers=headers, timeout=20)
    return (r.json().get("data") or {}).get("klines") or []


def kline_daily(code: str, lmt: int = 800, start_date: str | None = None
                ) -> pd.DataFrame:
    """个股日K（东财 push2his，fqt=0 不复权），返回 tdx 兼容格式：
    date/open/high/low/close/vol(股)/amount(元)
    注：东财 klines 字段序 = 日期,开,收,高,低,量(手),额(元)，量需 ×100 转股。"""
    try:
        klines = _kline_get(secid(code), lmt=lmt, fqt=0)
    except Exception as e:
        log.warning(f"东财日K {code} 失败: {e}")
        return pd.DataFrame()
    rows = []
    for line in klines:
        p = line.split(",")
        if len(p) < 7:
            continue
        try:
            rows.append({
                "date": pd.to_datetime(p[0]),
                "open": float(p[1]), "close": float(p[2]),
                "high": float(p[3]), "low": float(p[4]),
                "vol": float(p[5]) * 100.0,      # 手 → 股
                "amount": float(p[6]),           # 元
            })
        except (ValueError, IndexError):
            continue
    df = pd.DataFrame(rows)
    if not df.empty and start_date:
        df = df[df["date"] >= pd.to_datetime(start_date)]
    return (df.sort_values("date").drop_duplicates(subset=["date"], keep="last")
            .reset_index(drop=True)
            if not df.empty else df)


def kline_index(secid_str: str, lmt: int = 800,
                start_date: str | None = None) -> pd.DataFrame:
    """指数日K（东财 push2his），返回 date/open/high/low/close/volume。
    指数无成交额，volume 单位=手（与通达信指数口径一致）。"""
    try:
        klines = _kline_get(secid_str, lmt=lmt, fqt=0)
    except Exception as e:
        log.warning(f"东财指数K {secid_str} 失败: {e}")
        return pd.DataFrame()
    rows = []
    for line in klines:
        p = line.split(",")
        if len(p) < 6:
            continue
        try:
            rows.append({
                "date": pd.to_datetime(p[0]),
                "open": float(p[1]), "close": float(p[2]),
                "high": float(p[3]), "low": float(p[4]),
                "volume": float(p[5]),
            })
        except (ValueError, IndexError):
            continue
    df = pd.DataFrame(rows)
    if not df.empty and start_date:
        df = df[df["date"] >= pd.to_datetime(start_date)]
    return (df.sort_values("date").drop_duplicates(subset=["date"], keep="last")
            .reset_index(drop=True)
            if not df.empty else df)


# ── 个股资金流（日级，最近120个交易日） ───────────────────────────
def fund_flow_daily(code: str) -> pd.DataFrame:
    """主力/超大单/大单/中单/小单 日级净流入（元），最近120日"""
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    params = {
        "secid": secid(code), "lmt": "120",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "klt": "101",
    }
    headers = {"Referer": "https://quote.eastmoney.com/",
               "Origin": "https://quote.eastmoney.com"}
    try:
        r = em_get(url, params=params, headers=headers)
        klines = (r.json().get("data") or {}).get("klines") or []
    except Exception as e:
        log.warning(f"fund_flow {code} 失败: {e}")
        return pd.DataFrame()
    rows = []
    for line in klines:
        p = line.split(",")
        if len(p) >= 6:
            rows.append({
                "date": p[0], "code": code,
                "main_net": float(p[1]) if p[1] != "-" else 0,
                "super_net": float(p[2]) if p[2] != "-" else 0,
                "large_net": float(p[3]) if p[3] != "-" else 0,
                "mid_net": float(p[4]) if p[4] != "-" else 0,
                "small_net": float(p[5]) if p[5] != "-" else 0,
            })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


# ── 全市场资金流排名快照（一次请求拿全市场当日主力净流入） ────────
def fund_flow_market_snapshot() -> pd.DataFrame:
    """全市场个股当日资金流排名（单次请求，适合日频增量）
    返回: code name price change_pct main_net(元) main_net_pct super_net large_net
    """
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "6000", "po": "1", "np": "1",
        "fltt": "2", "invt": "2",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
        "fields": "f12,f14,f2,f3,f62,f184,f66,f72,f78,f84,f124",
    }
    headers = {"Referer": "https://quote.eastmoney.com/",
               "Origin": "https://quote.eastmoney.com"}
    try:
        r = em_get(url, params=params, headers=headers, timeout=20)
        items = (r.json().get("data") or {}).get("diff") or []
    except Exception as e:
        log.warning(f"fund_flow_market 快照失败: {e}")
        return pd.DataFrame()

    def _sf(v):
        try:
            return float(v) if v not in (None, "", "-") else None
        except (TypeError, ValueError):
            return None

    rows = []
    for it in items:
        rows.append({
            "code": it.get("f12", ""),
            "name": it.get("f14", ""),
            "price": _sf(it.get("f2")),
            "change_pct": _sf(it.get("f3")),
            "main_net": _sf(it.get("f62")),          # 主力净流入 元
            "main_net_pct": _sf(it.get("f184")),     # 主力净占比 %
            "super_net": _sf(it.get("f66")),
            "large_net": _sf(it.get("f72")),
            "mid_net": _sf(it.get("f78")),
            "small_net": _sf(it.get("f84")),
        })
    return pd.DataFrame(rows)
