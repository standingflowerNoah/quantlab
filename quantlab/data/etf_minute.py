"""ETF / 基金 分钟K 入湖（fsdb 源）
================================================================
用户裁决（2026-09-12）：补齐 ETF 分钟。
覆盖：fsdb `股票代码` 组 1（深市）+ 组 5（沪市），约 2,053 只基金代码。

⚠️ 边界与形态（2026-09-12 实测）
- 深度：**2025-01-02 起**（2024 全年 0 根）—— 与股票分钟同一边界，
  由镜像清单驱动，不可向前扩展。
- 根数：2025-01-02 与 2026-01-05 均为 **240 根**，2026-09-10 为 **242 根**
  （2026 年网格切换后多出 14:59 零成交 bar 与 15:00 收盘 bar）→ **勿硬编码根数**。
- 列名：fsdb 分钟表的时间列叫 `date`（14 位 int，YYYYMMDDHHMMSS），
  入湖统一改名为 `datetime`（与股票分钟 kline_1min 对齐）。

湖结构：
  data/lake/clean/kline_1min_etf/year=YYYY/day=YYYY-MM-DD/part-0.parquet
（hive 风格按日分区；与股票分钟 kline_1min 分目录隔离，避免污染 5,471 只的股票池）

工程约束（沿用 P0 护栏）：
- **有界查询**：每次都是 `表 + key:{code} + fwz:{start},{end}`，绝不发无界查询
- **并发 ≤4**：fsdb 的安全并发上限
- **worker 内落地**：拉取结果直接写临时 parquet，future 只回传 int
  （避免 DataFrame 滞留 future 导致内存线性增长）
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

from .. import config
from ..config import get_logger
from .sources import fsdb_source as fs

log = get_logger(__name__)

ETF_MIN_DIR = config.CLEAN_DIR / "kline_1min_etf"
TMP_DIR = config.DATA_DIR / "tmp" / "etf_min"

KEEP_COLS = ["code", "datetime", "open", "high", "low", "close",
             "volume", "amount"]

# fsdb ETF 分钟实测边界
MIN_START = "20250102"


def _norm(d: pd.DataFrame) -> pd.DataFrame:
    """规范化列名与类型：date(14位) → datetime"""
    d = d.copy()
    d["datetime"] = pd.to_datetime(d["date"].astype("int64").astype(str),
                                   format="%Y%m%d%H%M%S", errors="coerce")
    d = d[d["datetime"].notna()]
    for c in KEEP_COLS:
        if c not in d.columns:
            d[c] = None
    return d[KEEP_COLS]


def _fetch_to_tmp(code: str, start: str, end: str) -> int:
    """worker：拉一只 ETF 的分钟区间，直接落临时 parquet，只回传行数"""
    try:
        raw = fs.minute_bars(code, start, end)
    except Exception as e:                                    # noqa: BLE001
        log.debug(f"ETF 分钟 {code} 失败: {e}")
        return 0
    if raw is None or raw.empty or (raw["code"] != code).any():
        return 0
    d = _norm(raw)
    if d.empty:
        return 0
    d.to_parquet(TMP_DIR / f"{code}.parquet", index=False, compression="zstd")
    return int(len(d))


def _write_partitioned(codes: list[str]) -> None:
    """把临时目录里的按-code 文件流式重分区成 year=/day= 结构

    ⚠️ 本机装有 safe-delete shim（批量删除 >50 个文件会被 fail-closed 拒绝），
    因此**不做 rmtree**：`overwrite_or_ignore` 会覆盖同名分区文件（每天一个
    part-{i}.parquet，i 恒为 0）→ 幂等且不残留。
    ⚠️ 只读本次 `codes` 对应的临时文件，避免上次回补的残留污染结果。
    """
    import pyarrow as pa
    import pyarrow.dataset as ds

    files = [str(TMP_DIR / f"{c}.parquet") for c in codes
             if (TMP_DIR / f"{c}.parquet").exists()]
    if not files:
        raise RuntimeError("ETF 分钟重分区：临时目录无可用文件")

    dataset = ds.dataset(files, format="parquet")
    tbl = dataset.to_table(columns=KEEP_COLS)
    tbl = tbl.append_column("_day", pa.compute.strftime(
        tbl["datetime"], format="%Y-%m-%d"))
    tbl = tbl.append_column("_year", pa.compute.strftime(
        tbl["datetime"], format="%Y"))
    dataset = ds.dataset(tbl)
    file_opts = ds.ParquetFileFormat().make_write_options(compression="zstd")
    ds.write_dataset(
        dataset, str(ETF_MIN_DIR), format="parquet",
        partitioning=ds.partitioning(
            pa.schema([("_year", pa.string()), ("_day", pa.string())]),
            flavor="hive"),
        basename_template="part-{i}.parquet",
        existing_data_behavior="overwrite_or_ignore",
        file_options=file_opts,
    )


def backfill(start: str = MIN_START, end: str | None = None,
             codes: list[str] | None = None, workers: int = 4) -> dict:
    """全量回补 ETF 分钟（默认 2025-01-02 起）"""
    if codes is None:
        codes = fs.fund_codes()
    end = end or pd.Timestamp.now().strftime("%Y%m%d")
    if start < MIN_START:
        log.warning(f"start={start} 早于 fsdb 边界 {MIN_START}，按边界执行")
        start = MIN_START

    fs.ensure_healthy()
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    # ⚠️ 不预删临时文件：本机 safe-delete 会拦截批量删除（>50 个文件）。
    #    改为"读取时按本次 codes 过滤"，残留文件不参与本次结果。

    log.info(f"ETF 分钟回补 {start}~{end}：候选 {len(codes)} 只（并发 {workers}）")
    rows, ok, t0 = 0, 0, time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_to_tmp, c, start, end): c for c in codes}
        for i, fut in enumerate(as_completed(list(futs)), 1):
            futs.pop(fut, None)          # 立即释放 future（防结果滞留）
            n = fut.result()
            rows += n
            ok += 1 if n else 0
            if i % 400 == 0:
                log.info(f"  进度 {i}/{len(codes)}  已拉 {rows:,} 行"
                         f"  ({time.time()-t0:.0f}s)")

    log.info(f"拉取完成：{ok}/{len(codes)} 只有数据，共 {rows:,} 行，"
             f"{time.time()-t0:.0f}s → 开始重分区")
    _write_partitioned(codes)
    _cleanup_tmp()

    files = list(ETF_MIN_DIR.rglob("*.parquet"))
    log.info(f"ETF 分钟回补完成：{rows:,} 行 / {len(files)} 个日分区 / "
             f"耗时 {time.time()-t0:.0f}s → {ETF_MIN_DIR}")
    return {"rows": rows, "codes": ok, "files": len(files),
            "elapsed": round(time.time() - t0, 1)}


def _cleanup_tmp() -> None:
    """尽力清理临时文件；被 safe-delete 拦截则跳过（不影响结果正确性）"""
    try:
        n = 0
        for p in TMP_DIR.glob("*.parquet"):
            p.unlink()
            n += 1
        log.info(f"  临时文件已清理 {n} 个")
    except Exception as e:                                    # noqa: BLE001
        log.warning(f"  临时文件清理跳过（本机 safe-delete 拦截批量删除）: "
                    f"{str(e)[:100]}")


def update(target: pd.Timestamp | str | None = None,
           codes: list[str] | None = None) -> int:
    """增量：拉当日 ETF 分钟并合并进对应日分区（幂等覆盖）"""
    day = pd.Timestamp(target) if target is not None else \
        pd.Timestamp.now().normalize()
    ymd = day.strftime("%Y%m%d")
    if ymd < MIN_START:
        log.warning(f"{ymd} 早于 fsdb 分钟边界 {MIN_START}，跳过")
        return 0
    if codes is None:
        codes = fs.fund_codes()

    fs.ensure_healthy()
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_fetch_to_tmp, c, ymd, ymd): c for c in codes}
        frames, miss = [], 0
        for fut in as_completed(list(futs)):
            code = futs.pop(fut, None)
            if fut.result() == 0:
                miss += 1
                continue
            try:
                frames.append(pd.read_parquet(TMP_DIR / f"{code}.parquet"))
            except Exception:                                 # noqa: BLE001
                continue
    for p in TMP_DIR.glob("*.parquet"):
        try:
            p.unlink()
        except Exception:                                     # noqa: BLE001
            break                # safe-delete 拦截批量删除 → 交给 _cleanup_tmp
    if not frames:
        log.warning(f"ETF 分钟 {day.date()} 无数据（缺 {miss} 只）")
        return 0

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["code", "datetime"], keep="last")
    out = ETF_MIN_DIR / f"year={day.year}" / f"day={day:%Y-%m-%d}"
    out.mkdir(parents=True, exist_ok=True)
    # 同名 part-0.parquet 直接覆盖 = 幂等（不做删除：本机 safe-delete 会拦截）
    df.sort_values(["code", "datetime"]).to_parquet(
        out / "part-0.parquet", index=False, compression="zstd")
    log.info(f"ETF 分钟 {day.date()}: 写入 {len(df):,} 行 / "
             f"{df['code'].nunique()} 只（缺 {miss} 只）→ {out}")
    return len(df)


def load(start: str | None = None, end: str | None = None,
         codes: list[str] | None = None) -> pd.DataFrame:
    """读取 ETF 分钟（in-memory DuckDB glob 直读，不依赖主库）"""
    import duckdb
    pat = str(ETF_MIN_DIR / "**" / "*.parquet").replace("\\", "/")
    con = duckdb.connect()
    try:
        where = []
        if start:
            where.append(f"datetime >= TIMESTAMP '{pd.Timestamp(start)}'")
        if end:
            where.append(f"datetime <= TIMESTAMP '{pd.Timestamp(end)} 23:59:59'")
        if codes:
            lst = ",".join(f"'{c}'" for c in codes)
            where.append(f"code IN ({lst})")
        sql = (f"SELECT {', '.join(KEEP_COLS)} FROM "
               f"read_parquet('{pat}', hive_partitioning=false)")
        if where:
            sql += " WHERE " + " AND ".join(where)
        return con.execute(sql).df()
    finally:
        con.close()


def coverage() -> dict:
    """覆盖概览：行数 / 标的数 / 日期范围 / 日分区数"""
    import duckdb
    pat = str(ETF_MIN_DIR / "**" / "*.parquet").replace("\\", "/")
    con = duckdb.connect()
    try:
        r = con.execute(f"""
            SELECT count(*) n, count(DISTINCT code) codes,
                   min(datetime) d0, max(datetime) d1,
                   count(DISTINCT CAST(datetime AS DATE)) n_days
            FROM read_parquet('{pat}', hive_partitioning=false)
        """).df()
        return r.to_dict("records")[0]
    finally:
        con.close()


def refresh(target: pd.Timestamp | str | None = None) -> int:
    """流水线入口"""
    return update(target)
