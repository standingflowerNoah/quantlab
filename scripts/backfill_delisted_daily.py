"""退市股日线回补（全景规划批次 C）——修复幸存者偏差。

数据通道
--------
- 主源 fsdb day_bars（本地 HTTP，全史，含 OHLCV+换手+市值+is_st 附加列）
- adj_factor：tushare `stk_factor`（按 ts_code 区间，含 adj_factor 列；
  主库 dividend_events 不含退市股 → 无法本地自算）
- 缺段兜底：tushare `bak_daily`（退市整理期 fsdb 缺失段）

清单
----
data/lake/clean/instruments_delisted/part-all.parquet
（delist_date >= 2021-12-01 的 193 只）

落湖
----
data/lake/clean/kline_daily_delisted/part-{code}.parquet
列对齐主库 kline_daily + 研究加成列：
  date, code, open, high, low, close, vol, amount, adj_factor,
  pre_close, turnover, float_mv, total_mv, is_st, name

用法
----
  python scripts/backfill_delisted_daily.py            # 全量（断点续跑）
  python scripts/backfill_delisted_daily.py --codes 300799,000046
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backfill_tushare_datasets import LAKE, api, to_df  # 复用代理客户端/限流
OUT_DIR = LAKE / "kline_daily_delisted"
PROGRESS = OUT_DIR / "_delisted_progress.json"
LOG_FILE = ROOT / "logs" / "backfill_delisted_daily.log"
START = "20211201"


def load_delisted() -> pd.DataFrame:
    df = pd.read_parquet(LAKE / "instruments_delisted" / "part-all.parquet")
    df = df[(df["delist_date"].notna()) & (df["delist_date"] >= pd.Timestamp("2021-12-01"))]
    return df.sort_values("delist_date").reset_index(drop=True)


def fsdb_bars(code: str, start: str, end: str) -> pd.DataFrame:
    from quantlab.data.sources import fsdb_source as fs
    df = fs.day_bars(code, start, end)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
    df = df.rename(columns={"volume": "vol"})
    keep = ["date", "code", "open", "high", "low", "close", "vol", "amount",
            "pre_close", "turnover", "float_mv", "total_mv", "is_st", "name"]
    return df[[c for c in keep if c in df.columns]]


def tushare_bars(ts_code: str, start: str, end: str) -> pd.DataFrame:
    """stk_factor 区间拉取（含 adj_factor + OHLCV）。~500 行/年 → 2 年一段。"""
    frames = []
    s = date.fromisoformat(f"{start[:4]}-{start[4:6]}-{start[6:]}")
    e = date.fromisoformat(f"{end[:4]}-{end[4:6]}-{end[6:]}")
    cur = s
    while cur <= e:
        seg_end = min(date.fromordinal(
            min(cur.toordinal() + 500, e.toordinal() + 1) - 1), e)
        f, it = api("stk_factor", {"ts_code": ts_code,
                                   "start_date": cur.strftime("%Y%m%d"),
                                   "end_date": seg_end.strftime("%Y%m%d")})
        if it:
            frames.append(to_df(f, it))
        cur = date.fromordinal(seg_end.toordinal() + 1)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
    df["code"] = ts_code.split(".")[0]
    df = df.rename(columns={"vol": "vol_t"})
    # tushare stk_factor vol 单位与 fsdb 不同（万股/手级差异）→ 量能统一以 fsdb 为准，
    # stk_factor 只取 adj_factor + 价格兜底
    keep = ["date", "code", "open", "high", "low", "close", "amount", "adj_factor"]
    return df[[c for c in keep if c in df.columns]]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default=None, help="逗号分隔，仅跑指定代码")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
                  logging.StreamHandler()])

    dl = load_delisted()
    if args.codes:
        want = set(args.codes.split(","))
        dl = dl[dl["code"].isin(want)]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    done: set = set()
    if PROGRESS.exists():
        done = set(json.loads(PROGRESS.read_text(encoding="utf-8")))
    todo = dl[~dl["code"].isin(done)]
    logging.info("退市股 %d 只，已完成 %d，待回补 %d", len(dl), len(done), len(todo))

    for i, (_, r) in enumerate(todo.iterrows()):
        code, ts_code = r["code"], r["ts_code"]
        delist = r["delist_date"]
        end_str = (delist + timedelta(days=10)).strftime("%Y%m%d")
        t0 = time.time()
        try:
            # 1) 主源 fsdb
            df = fsdb_bars(code, START, end_str)
            # 2) stk_factor 拿 adj_factor（+价格兜底）
            tf = tushare_bars(ts_code, START, end_str)
            if df.empty:
                df = tf.drop(columns=["adj_factor"], errors="ignore")
                logging.warning("%s fsdb 空，用 stk_factor %d 行兜底", code, len(df))
            if not df.empty and not tf.empty:
                adj = tf[["date", "adj_factor"]].drop_duplicates("date", keep="last")
                df = df.merge(adj, on="date", how="left")
            # 3) 缺段兜底 bak_daily（fsdb 与 stk_factor 都没盖到的尾部，
            #    典型=退市整理期）
            if not df.empty:
                have_max = df["date"].max()
                delist_ts = pd.Timestamp(delist)
                if have_max < delist_ts - timedelta(days=7):
                    gap_s = (have_max + timedelta(days=1)).strftime("%Y%m%d")
                    f, it = api("bak_daily", {"ts_code": ts_code,
                                              "start_date": gap_s, "end_date": end_str})
                    if it:
                        bd = to_df(f, it)
                        bd["date"] = pd.to_datetime(bd["trade_date"], format="%Y%m%d",
                                                    errors="coerce")
                        bd = bd[(bd["close"] > 0) | (bd["open"] > 0)]  # 过滤全零占位行
                        if len(bd):
                            bd2 = bd.rename(columns={"vol": "vol_b"})[
                                ["date", "open", "high", "low", "close", "amount"]]
                            bd2.insert(1, "code", code)
                            df = pd.concat([df, bd2], ignore_index=True)
                            df = df.drop_duplicates("date", keep="first").sort_values("date")
                            logging.info("%s bak_daily 补尾段 %d 行 (%s~%s)", code, len(bd2),
                                         bd2["date"].min().date(), bd2["date"].max().date())
            if df.empty:
                logging.warning("%s 无任何数据源可用，跳过", code)
                done.add(code)   # 标记完成避免死循环重试
                continue
            df = df.sort_values("date").reset_index(drop=True)
            df.to_parquet(OUT_DIR / f"part-{code}.parquet", index=False)
            done.add(code)
            logging.info("[%d/%d] %s(%s): %d 行 %s~%s adj缺%.0f%% (%.0fs)",
                         len(done), len(dl), code, r["name"], len(df),
                         df["date"].min().date(), df["date"].max().date(),
                         (df["adj_factor"].isna().mean() * 100
                          if "adj_factor" in df else 100), time.time() - t0)
        except Exception as e:  # noqa: BLE001
            logging.error("%s 失败: %s", code, e)
        if (i + 1) % 10 == 0:
            PROGRESS.write_text(json.dumps(sorted(done)), encoding="utf-8")
    PROGRESS.write_text(json.dumps(sorted(done)), encoding="utf-8")

    # 汇总
    import duckdb
    files = sorted(OUT_DIR.glob("part-*.parquet"))
    if files:
        con = duckdb.connect()
        fs = ", ".join(f"'{f.as_posix()}'" for f in files)
        print(con.execute(f"""
            select count(*) as n_files, sum(n) as n_rows,
                   count(*) filter (where adjna > 0.5) as adj_half_missing
            from (
                select code, count(*) as n,
                       avg(case when adj_factor is null then 1.0 else 0.0 end) as adjna
                from read_parquet([{fs}], hive_partitioning=false)
                group by code
            ) t
        """).fetchdf().to_string())
        con.close()


if __name__ == "__main__":
    main()
