"""ETF / 基金 复权（分红）事件入湖（fsdb 源）
================================================================
用户裁决（2026-09-12）：补齐 ETF 复权。

⚠️ 形态（实测）
- 事件是**年度分红**：`div` 有值，`give=trans=0`（ETF 极少送转），
  `mult` 为单次乘数、`cum` 为累计乘数。
- **覆盖率仅 16.7%**（随机 120 只 ETF 中 20 只有事件）—— 不分红的 ETF
  本来就没有复权事件，**空 ≠ 缺数据**，勿当异常。
- 510300 实测 14 条（2012-12-18 ~ 2026-01-19），cum 累计 1.269。

湖结构：
  data/lake/clean/etf_adj/part-full.parquet   （单文件，全量仅千行级）

字段：code / ex_date / div / give / trans / mult / cum
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from .. import config
from ..config import get_logger
from .sources import fsdb_source as fs

log = get_logger(__name__)

ADJ_DIR = config.CLEAN_DIR / "etf_adj"
KEEP_COLS = ["code", "ex_date", "div", "give", "trans", "mult", "cum"]

# 已核实无复权字段，ETF 复权因子由 mult/cum 直接给出（无需 peigu）
KNOWN_EMPTY = True


def _fetch(code: str) -> pd.DataFrame | None:
    try:
        a = fs.adj_factors(code)
    except Exception as e:                                    # noqa: BLE001
        log.debug(f"ETF 复权 {code} 失败: {e}")
        return None
    if a is None or a.empty or (a["code"] != code).any():
        return None
    return a


def backfill(codes: list[str] | None = None, workers: int = 4) -> dict:
    """全量回补 ETF 复权事件（单文件落盘）"""
    from .etf import load_etf_daily
    if codes is None:
        codes = sorted(load_etf_daily()["code"].unique())
    fs.ensure_healthy()
    t0 = time.time()
    log.info(f"ETF 复权回补：候选 {len(codes)} 只（并发 {workers}）")

    frames, nz, empty = [], 0, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch, c): c for c in codes}
        for i, fut in enumerate(as_completed(list(futs)), 1):
            futs.pop(fut, None)
            d = fut.result()
            if d is None or d.empty:
                empty += 1
            else:
                frames.append(d)
                nz += 1
            if i % 500 == 0:
                log.info(f"  进度 {i}/{len(codes)}  有事件 {nz} 只")

    if not frames:
        log.warning("ETF 复权：无任何事件")
        return {"rows": 0, "codes": 0, "empty": empty, "elapsed": 0}

    df = pd.concat(frames, ignore_index=True)
    for c in KEEP_COLS:
        if c not in df.columns:
            df[c] = None
    df = df[KEEP_COLS].drop_duplicates(subset=["code", "ex_date"], keep="last")
    df = df.sort_values(["code", "ex_date"])

    ADJ_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(ADJ_DIR / "part-full.parquet", index=False,
                  compression="zstd")
    cov = nz / len(codes)
    log.info(f"ETF 复权回补完成：{len(df):,} 行 / {nz} 只有事件"
             f"（{cov:.1%}）/ {empty} 只无事件 / {time.time()-t0:.0f}s")
    return {"rows": int(len(df)), "codes": nz, "empty": empty,
            "coverage": round(cov, 4), "elapsed": round(time.time() - t0, 1)}


def load() -> pd.DataFrame:
    f = ADJ_DIR / "part-full.parquet"
    if not f.exists():
        return pd.DataFrame(columns=KEEP_COLS)
    df = pd.read_parquet(f)
    df["ex_date"] = pd.to_datetime(df["ex_date"], format="%Y%m%d",
                                   errors="coerce")
    return df


def adj_factor_map() -> pd.DataFrame:
    """ETF 复权乘数（code, ex_date, mult, cum），供行情复权使用"""
    df = load()
    return df[["code", "ex_date", "mult", "cum"]].copy()
