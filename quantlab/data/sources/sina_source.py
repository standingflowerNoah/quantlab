"""新浪财经数据源：全 A 股列表（官方口径，含北交所）
=====================================
- Market_Center.getHQNodeData: node=hs_a 全A股，单页上限100条
- 字段: code/name/trade(价)/per(PE)/pb/mktcap(总市值万)/nmc(流通市值万)/turnoverratio
"""
from __future__ import annotations

import time

import pandas as pd
import requests

from ...config import get_logger

log = get_logger(__name__)

BASE = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
        "json_v2.php/Market_Center.")
HEADERS = {"User-Agent": "Mozilla/5.0",
           "Referer": "https://finance.sina.com.cn"}


def _get(params: dict, timeout: int = 10):
    r = requests.get(BASE + params.pop("_api"), params=params,
                     headers=HEADERS, timeout=timeout)
    return r.json()


def all_a_shares(max_pages: int = 80) -> pd.DataFrame:
    """全 A 股列表（含北交所），单页100条自动翻页"""
    try:
        total = int(_get({"_api": "getHQNodeStockCount", "node": "hs_a"}))
    except Exception as e:
        log.warning(f"新浪股票总数获取失败: {e}")
        total = 5600
    pages = min(max_pages, total // 100 + 2)
    rows = []
    for page in range(1, pages + 1):
        try:
            data = _get({"_api": "getHQNodeData", "page": str(page),
                         "num": "100", "sort": "symbol", "asc": "1",
                         "node": "hs_a", "symbol": "", "_s_r_a": "page"})
        except Exception as e:
            log.warning(f"新浪列表第{page}页失败: {e}")
            time.sleep(1)
            continue
        if not data:
            break
        rows.extend(data)
        if len(data) < 100:
            break
        time.sleep(0.3)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    out = pd.DataFrame({
        "code": df["code"].astype(str).str.zfill(6),
        "name": df["name"].astype(str),
        "price": pd.to_numeric(df["trade"], errors="coerce"),
        "pe_ttm": pd.to_numeric(df["per"], errors="coerce"),
        "pb": pd.to_numeric(df["pb"], errors="coerce"),
        "mcap_yi": pd.to_numeric(df["mktcap"], errors="coerce") / 1e4,
        "float_mcap_yi": pd.to_numeric(df["nmc"], errors="coerce") / 1e4,
        "turnover_pct": pd.to_numeric(df["turnoverratio"], errors="coerce"),
    })
    return out.drop_duplicates(subset=["code"]).reset_index(drop=True)


_KLINE_URL = ("http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "CN_MarketData.getKLineData")


def index_daily(symbol: str, datalen: int = 800) -> pd.DataFrame:
    """新浪指数日K，返回 date/open/high/low/close/volume(手)

    指数日历/K线的 TDX 兜底源（2026-09-09/10 TDX 服务器池连续宕机实证可用）。
    symbol 形如 sh000300 / sz399006。volume 新浪口径=股，÷100 对齐通达信手。
    """
    r = requests.get(_KLINE_URL, params={"symbol": symbol, "scale": "240",
                                         "ma": "no", "datalen": str(datalen)},
                     headers=HEADERS, timeout=20,
                     proxies={"http": None, "https": None})
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
