"""国泰君安 Alpha191 计算引擎
=====================================
从 kline_daily 构建宽表（date × code），逐因子调用公式计算，
经健全性检验后写入 Parquet 因子库（与 Store.append_factor 兼容，
可用 factor IC 工具/合成模型直接复用）。

设计要点：
- Alpha191 因子不注册进 factor registry：避免 daily_pipeline 的
  compute_all 每天重算 191 个因子；按需调用本模块批量计算。
- OHLC/VWAP 前复权，volume/amount 原始值。
- 存储起点默认 2023-01-01（控制磁盘；IC 评估 3.7 年窗口足够）。
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd

from ..config import get_logger
from .alpha191_formulas import ALPHAS

log = get_logger("alpha191")

WIDE_KEYS = ("open", "high", "low", "close", "vwap", "volume", "amount", "ret")


def build_wide(store, start: str = "2022-06-01") -> dict[str, pd.DataFrame]:
    """kline_daily → 宽表字典（OHLC/VWAP 前复权）"""
    df = store.q("""
        SELECT date, code, open, high, low, close, vol, amount, adj_factor
        FROM kline_daily WHERE close > 0 AND vol > 0
    """)
    df["date"] = pd.to_datetime(df["date"])
    if start:
        df = df[df["date"] >= pd.to_datetime(start)]
    df = df.dropna(subset=["adj_factor"])
    af = df.set_index(["date", "code"])["adj_factor"]

    wide = {}
    for col in ("open", "high", "low", "close"):
        w = df.set_index(["date", "code"])[col].unstack()
        wide[col] = (w * af.unstack()).sort_index()
    # 前复权 VWAP = amount/vol × adj_factor（与复权价同口径）
    vw = (df["amount"] / df["vol"]).values
    tmp = df.assign(vwap=vw).set_index(["date", "code"])["vwap"].unstack()
    wide["vwap"] = (tmp * af.unstack()).sort_index()
    wide["volume"] = df.set_index(["date", "code"])["vol"].unstack().sort_index()
    wide["amount"] = df.set_index(["date", "code"])["amount"].unstack().sort_index()
    wide["ret"] = wide["close"].pct_change()
    return wide


def _to_long(res: pd.DataFrame) -> pd.DataFrame:
    long = res.stack(future_stack=True).rename("value").reset_index()
    long.columns = ["date", "code", "value"]
    long["value"] = pd.to_numeric(long["value"], errors="coerce")
    long = long.replace([np.inf, -np.inf], np.nan).dropna(subset=["value"])
    return long


def _sanity(res: pd.DataFrame) -> tuple[bool, str]:
    """健全性检验：非全 NaN、非全常数、截面有效宽度足够"""
    v = res.values
    if not np.isfinite(v).any():
        return False, "全 NaN"
    valid_ratio = np.isfinite(v).mean()
    if valid_ratio < 0.05:
        return False, f"有效率过低 {valid_ratio:.1%}"
    # 常数列占比过高 → 公式退化
    col_std = pd.DataFrame(v).std(axis=0)
    const_cols = (col_std.fillna(0) == 0).mean()
    if const_cols > 0.5:
        return False, f"常数列占比 {const_cols:.0%}"
    return True, f"valid={valid_ratio:.0%}"


def compute_alpha191(names: list[str] | None = None,
                     start: str = "2022-06-01",
                     end: str | None = None,
                     save: bool = True,
                     store=None) -> pd.DataFrame:
    """批量计算 Alpha191 因子

    参数:
        names: 因子名列表（如 ['alpha001','alpha002']），None=全部
        start: 宽表数据起点（rolling 需要预热期，因子输出仍含全窗口）
        save: 写入 Parquet 因子库（data/lake/factor/alphaNNN/）
    返回: manifest DataFrame（name/status/rows/valid_ratio/elapsed）
    """
    from ..data.store import Store
    store = store or Store()
    wide = build_wide(store, start)
    if end:
        end = pd.to_datetime(end)
        for k in WIDE_KEYS:
            wide[k] = wide[k].loc[:end]

    todo = names or sorted(ALPHAS)
    rows = []
    for name in todo:
        if name not in ALPHAS:
            rows.append({"name": name, "status": "missing",
                         "rows": 0, "note": "公式未实现", "elapsed": 0})
            continue
        fn, conf = ALPHAS[name]
        t0 = time.time()
        try:
            res = fn(wide)
            ok, note = _sanity(res)
            elapsed = round(time.time() - t0, 1)
            if ok and save:
                long = _to_long(res)
                n = store.append_factor(name, long)
                rows.append({"name": name, "status": "ok", "rows": n,
                             "note": f"{note}|{conf}", "elapsed": elapsed})
                log.info(f"{name} ok ({elapsed}s) {n} 行 {note}")
            elif ok:
                rows.append({"name": name, "status": "ok_dry", "rows": 0,
                             "note": f"{note}|{conf}", "elapsed": elapsed})
            else:
                rows.append({"name": name, "status": "invalid", "rows": 0,
                             "note": f"{note}|{conf}", "elapsed": elapsed})
                log.warning(f"{name} invalid: {note}")
        except Exception as e:
            rows.append({"name": name, "status": "FAIL", "rows": 0,
                         "note": str(e)[:80], "elapsed": round(time.time() - t0, 1)})
            log.error(f"{name} FAIL: {e}")
    return pd.DataFrame(rows)
