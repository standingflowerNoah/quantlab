"""ETF / 基金 日线入湖（fsdb 源）
================================================================
覆盖：fsdb `股票代码` 组 1（深市）+ 组 5（沪市），共约 2,053 只基金类证券。
用户裁决（2026-09-11）：**先做 ETF 日线，不做分钟**。

与 kline_daily 同构（不复权，volume 单位=股，amount=元），差异：
- 池来自 fsdb 基金代码组，而非 instruments（股票主表）
- 保留 fsdb 日k 的估值类字段（ETF 多为空，但 LOF/分级基金可能有）
- `is_etf` 由名称是否含 'ETF' 判定（数据驱动，不靠代码前缀猜）

湖结构：
  data/lake/clean/etf_daily/year=YYYY/part-dYYYYMMDD.parquet
（按日落文件、同名覆盖=幂等；写入前 anti-join 已有 (code, date)）
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from .. import config
from ..config import get_logger
from .sources import fsdb_source as fs


def _read_glob(pat: str, columns: list[str] | None = None) -> pd.DataFrame:
    """用 in-memory DuckDB 读 parquet glob（项目惯例；pandas 3.x 不支持 glob）

    hive_partitioning=false：分区目录名（snap=/year=）不注入为列，避免与
    真实列名冲突或误导 schema。
    """
    import duckdb
    sel = ", ".join(columns) if columns else "*"
    con = duckdb.connect()
    try:
        return con.execute(
            f"SELECT {sel} FROM read_parquet('{pat}', hive_partitioning=false)"
        ).df()
    finally:
        con.close()

log = get_logger(__name__)

ETF_DIR = config.CLEAN_DIR / "etf_daily"

KEEP_COLS = ["date", "code", "name", "open", "high", "low", "close",
             "pre_close", "volume", "amount", "turnover", "pct_chg",
             "amplitude", "total_share", "total_mv", "is_etf"]


def _fetch_one(code: str, start: str, end: str) -> pd.DataFrame | None:
    try:
        d = fs.day_bars(code, start, end)
    except Exception as e:                                   # noqa: BLE001
        log.debug(f"ETF 日线 {code} 失败: {e}")
        return None
    if d is None or d.empty:
        return None
    if (d["code"] != code).any():        # 防串位
        return None
    return d


def _existing_keys(day: pd.Timestamp) -> pd.DataFrame:
    """该日已有数据（用于 anti-join，避免重复写入）"""
    f = ETF_DIR / f"year={day.year}" / f"part-d{day.strftime('%Y%m%d')}.parquet"
    if not f.exists():
        return pd.DataFrame(columns=["code", "date"])
    try:
        return pd.read_parquet(f, columns=["code", "date"])
    except Exception:                                        # noqa: BLE001
        return pd.DataFrame(columns=["code", "date"])


def update_etf_daily(target: pd.Timestamp | str | None = None,
                     codes: list[str] | None = None) -> int:
    """增量更新当日 ETF/基金日线，返回写入行数"""
    day = pd.Timestamp(target) if target is not None else \
        pd.Timestamp.now().normalize()
    day_c = day.strftime("%Y%m%d")
    if codes is None:
        codes = fs.fund_codes()
    log.info(f"ETF 日线更新 {day.date()}：候选 {len(codes)} 只")

    fs.ensure_healthy()
    frames, miss, t0 = [], 0, time.time()
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_fetch_one, c, day_c, day_c): c for c in codes}
        for i, fut in enumerate(as_completed(futs), 1):
            d = fut.result()
            if d is None or d.empty:
                miss += 1
            else:
                frames.append(d)
            if i % 800 == 0:
                log.info(f"  进度 {i}/{len(codes)} ({time.time()-t0:.0f}s)")
    if not frames:
        log.warning(f"ETF 日线 {day.date()} 无数据（缺 {miss} 只）")
        return 0

    df = pd.concat(frames, ignore_index=True)
    df = df.rename(columns={"volume": "volume"})
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
    df = df[df["date"].notna()]
    df["is_etf"] = df["name"].fillna("").str.contains("ETF", case=False)
    for c in KEEP_COLS:
        if c not in df.columns:
            df[c] = None
    df = df[KEEP_COLS]

    # anti-join 已有 (code, date)：同一日重复运行不产生重复行
    ex = _existing_keys(day)
    if len(ex):
        ex = ex.assign(date=pd.to_datetime(ex["date"]))
        df = df.merge(ex[["code", "date"]].assign(_dup=1),
                      on=["code", "date"], how="left")
        df = df[df["_dup"].isna()].drop(columns="_dup")

    if df.empty:
        log.info(f"ETF 日线 {day.date()}：全部已存在，跳过写入")
        return 0

    f = ETF_DIR / f"year={day.year}" / f"part-d{day.strftime('%Y%m%d')}.parquet"
    f.parent.mkdir(parents=True, exist_ok=True)
    if f.exists():                       # 同日已有部分数据 → 合并覆盖
        old = pd.read_parquet(f)
        old = old[~old["code"].isin(set(df["code"]))]
        df = pd.concat([old, df], ignore_index=True)
    df.sort_values(["code", "date"]).to_parquet(f, index=False,
                                                compression="zstd")
    n_etf = int(df["is_etf"].sum())
    log.info(f"ETF 日线 {day.date()}: 写入 {len(df)} 行"
             f"（其中 name 含 ETF 的 {n_etf} 行），缺 {miss} 只 → {f}")
    return len(df)


def load_etf_daily(start: str | None = None, end: str | None = None,
                   etf_only: bool = True) -> pd.DataFrame:
    """读取 ETF 日线（glob 直读 parquet，不依赖 DuckDB）"""
    pat = str(ETF_DIR / "year=*" / "part-*.parquet").replace("\\", "/")
    try:
        df = _read_glob(pat)
    except Exception:                                        # noqa: BLE001
        return pd.DataFrame(columns=KEEP_COLS)
    if "year" in df.columns:             # hive 分区列回填，避免误导
        df = df.drop(columns=["year"])
    df["date"] = pd.to_datetime(df["date"])
    if etf_only and "is_etf" in df.columns:
        df = df[df["is_etf"]]
    if start:
        df = df[df["date"] >= pd.Timestamp(start)]
    if end:
        df = df[df["date"] <= pd.Timestamp(end)]
    return df.reset_index(drop=True)


def refresh(target: pd.Timestamp | str | None = None) -> int:
    """流水线入口（update.py 调用）"""
    return update_etf_daily(target)
