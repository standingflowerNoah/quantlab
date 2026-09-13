#!/usr/bin/env python3
"""hf_* 因子历史回填 —— 无锁路径（绕开 quant.duckdb 写锁）
=====================================================================
quant.duckdb 被另一会话的 backfill_metric_daily 长跑占用，Store() 打不开。
hf 因子是 SqlFactor：SQL 只引用 minute_feat 与 kline_daily 两张表，
**两者都是 Parquet**——自建内存 DuckDB 注册视图即可完整复算，无需主库。

写入：直接按年写 data/lake/factor/<name>/part-<year>.parquet
      （2022-2024 此前不存在任何行，append 语义等价于新建，无旧值残留风险）

用法：
    python scripts/backfill_hf_factors_lockfree.py                 # 全部 22 个
    python scripts/backfill_hf_factors_lockfree.py --names hf_rv_20
"""
from __future__ import annotations

import argparse
import gc
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402
import pandas as pd  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(ROOT / "logs" / "backfill_hf_lockfree.log",
                                                  encoding="utf-8"),
                              logging.StreamHandler(sys.stdout)])
log = logging.getLogger("hf_lockfree")

FACTOR_DIR = ROOT / "data" / "lake" / "factor"
MF_DIR = ROOT / "data" / "lake" / "clean" / "minute_feat"
DAILY = ROOT / "data" / "lake" / "clean" / "mirror" / "kline_daily.parquet"

START, END = "2022-01-01", "2024-12-31"


def make_con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET memory_limit='2GB'")
    con.execute(f"SET temp_directory='{(ROOT / 'data' / 'lake' / 'clean' / '.duckdb_tmp').as_posix()}'")
    con.execute("SET preserve_insertion_order=false")
    glob_mf = (MF_DIR / "part-*.parquet").as_posix()
    con.execute(f"""
        CREATE OR REPLACE VIEW minute_feat AS
        SELECT * FROM read_parquet('{glob_mf}')
    """)
    con.execute(f"""
        CREATE OR REPLACE VIEW kline_daily AS
        SELECT * FROM read_parquet('{DAILY.as_posix()}')
    """)
    return con


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--names", default="", help="逗分隔子集（缺省全部 hf_*）")
    p.add_argument("--mem", default="2GB")
    a = p.parse_args()

    from quantlab.factor.registry import get_factor
    names = ([x.strip() for x in a.names.split(",") if x.strip()]
             or sorted(d.name for d in FACTOR_DIR.glob("hf_*") if d.is_dir()))
    log.info("待回填 hf 因子 %d 个: %s", len(names), names)

    con = make_con()
    done = 0
    for i, name in enumerate(names, 1):
        t0 = time.time()
        try:
            factor = get_factor(name)
            sql, params = factor._sql(start=None, end=None, universe=None)
            df = con.execute(sql, params).df()
            if df.empty:
                log.warning(f"[{i}/{len(names)}] {name}: 计算结果为空")
                continue
            df["date"] = pd.to_datetime(df["date"])
            df["code"] = df["code"].astype(str).str.zfill(6)
            df["value"] = pd.to_numeric(df["value"], errors="coerce")
            df = df.dropna(subset=["value"])
            df = df[(df["date"] >= pd.Timestamp(START)) & (df["date"] <= pd.Timestamp(END))]
            df = df[["date", "code", "value"]].sort_values(["date", "code"]).reset_index(drop=True)

            if df.empty:
                log.warning(f"[{i}/{len(names)}] {name}: 区间内无行")
                continue

            d = FACTOR_DIR / name
            d.mkdir(parents=True, exist_ok=True)
            n_written = 0
            for y, part in df.groupby(df["date"].dt.year):
                f = d / f"part-{int(y)}.parquet"
                if f.exists():          # 幂等：同名覆盖（该年全部重写）
                    old = pd.read_parquet(f)
                    old = old[~old["date"].isin(part["date"].unique())]
                    part = pd.concat([old, part], ignore_index=True)
                    part = part.sort_values(["date", "code"])
                part.to_parquet(f, index=False, compression="zstd")
                n_written += len(part)
            log.info(f"[{i}/{len(names)}] {name}: 追加 {n_written:,} 行 "
                     f"({df['date'].min().date()}~{df['date'].max().date()}) "
                     f"{time.time() - t0:.0f}s")
            done += 1
        except Exception as e:  # noqa: BLE001
            log.error(f"[{i}/{len(names)}] {name}: 失败 {type(e).__name__}: {str(e)[:250]}")
        df = None
        gc.collect()

    log.info("完成 %d/%d 个因子", done, len(names))


if __name__ == "__main__":
    main()
