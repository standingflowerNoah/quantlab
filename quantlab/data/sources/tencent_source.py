"""腾讯财经数据源：实时行情快照（不封IP）/ 指数与个股日K
=====================================
- qt.gtimg.cn 批量实时行情：PE/PB/市值/换手/涨跌停，60 只/次
- web.ifzq.gtimg.cn 日K（含前复权 qfq）：指数 K 线主源 + K 线交叉校验
"""
from __future__ import annotations

import time
import urllib.request

import pandas as pd

from ... import config
from ...config import get_logger

log = get_logger(__name__)


def _prefix(code: str) -> str:
    if code.startswith(("6", "9")):
        return "sh"
    if code.startswith(("8", "4")):
        return "bj"
    if code.startswith(("0", "3")):
        return "sz"
    # 指数：000001.SH vs 399001.SZ 由调用方传带后缀代码
    return "sh"


def full_code(code: str) -> str:
    """600519 / 600519.SH / 000300.SH → sh600519 / sh000300"""
    code = str(code)
    if code.endswith((".SH", ".SZ", ".BJ")):
        c, suf = code.split(".")
        return suf.lower() + c
    return _prefix(code) + code


def batch_quotes(codes: list[str]) -> pd.DataFrame:
    """批量实时行情快照（60只/次，不封IP）

    返回列: code name price last_close open change_pct high low amount_wan
            turnover_pct pe_ttm pb mcap_yi float_mcap_yi limit_up limit_down
    """
    rows = []
    codes = [c for c in codes if c]
    for i in range(0, len(codes), config.TENCENT_BATCH):
        batch = codes[i:i + config.TENCENT_BATCH]
        url = "https://qt.gtimg.cn/q=" + ",".join(full_code(c) for c in batch)
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0")
            data = urllib.request.urlopen(req, timeout=10).read().decode("gbk")
        except Exception as e:
            log.warning(f"腾讯行情批次失败({batch[0]}..): {e}")
            time.sleep(1)
            continue
        for line in data.strip().split(";"):
            line = line.strip()
            if "=" not in line or '"' not in line:
                continue
            key = line.split("=")[0].split("_")[-1]
            vals = line.split('"')[1].split("~")
            if len(vals) < 53 or not vals[1]:
                continue

            def _f(idx, default=0.0):
                try:
                    v = vals[idx]
                    return float(v) if v not in ("", "-") else default
                except (ValueError, IndexError):
                    return default

            rows.append({
                "code": key[2:],
                "name": vals[1],
                "price": _f(3),
                "last_close": _f(4),
                "open": _f(5),
                "change_pct": _f(32),
                "high": _f(33),
                "low": _f(34),
                "amount_wan": _f(37),
                "turnover_pct": _f(38),
                "pe_ttm": _f(39),
                "amplitude_pct": _f(43),
                "mcap_yi": _f(44),
                "float_mcap_yi": _f(45),
                "pb": _f(46),
                "limit_up": _f(47),
                "limit_down": _f(48),
                "vol_ratio": _f(49),
                "pe_static": _f(52),
            })
        time.sleep(0.15)
    return pd.DataFrame(rows)


def daily_kline(code: str, start: str | None = None,
                end: str | None = None, fq: str = "") -> pd.DataFrame:
    """腾讯日K（fq: ''=不复权 'qfq'=前复权 'hfq'=后复权），指数/个股通用

    腾讯语义：返回 [start, end] 区间内最近 640 根（start 常被弱化），
    全量翻页请用 daily_kline_full。

    返回列: date open close high low volume(手)
    """
    import json
    fc = full_code(code)
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={fc},day,,{end or ''},640,{fq}")
    for attempt in range(3):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0")
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            break
        except Exception as e:
            if attempt == 2:
                log.warning(f"腾讯日K {code} 失败: {e}")
                return pd.DataFrame()
            time.sleep(1)
    node = data.get("data", {}).get(fc, {})
    k = node.get(f"{fq}day") or node.get("day") or []
    if not k:
        return pd.DataFrame()
    # 每行可能带附加字段，仅取前6列
    rows = [r[:6] for r in k]
    df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    for c in ("open", "close", "high", "low", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    if start is not None:
        df = df[df["date"] >= pd.to_datetime(start)]
    return df


def daily_kline_full(code: str, start: str | None = None,
                     end: str | None = None, fq: str = "") -> pd.DataFrame:
    """腾讯日K 全量翻页：以 end 为游标从最新往回取，每轮 640 根"""
    chunks = []
    end_cursor = end
    for _ in range(15):
        df = daily_kline(code, end=end_cursor, fq=fq)
        if df.empty:
            break
        chunks.append(df)
        earliest = df["date"].min()
        if len(df) < 640:
            break                    # 已到数据起点
        if start is not None and earliest <= pd.to_datetime(start):
            break
        end_cursor = (earliest - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    if not chunks:
        return pd.DataFrame()
    out = pd.concat(chunks, ignore_index=True).drop_duplicates(
        subset=["date"], keep="first").sort_values("date").reset_index(drop=True)
    if start is not None:
        out = out[out["date"] >= pd.to_datetime(start)]
    return out
