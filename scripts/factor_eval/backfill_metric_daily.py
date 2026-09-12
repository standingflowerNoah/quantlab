"""因子评估指标 L0/L2 全历史回填
==============================
用法：
  python scripts/factor_eval/backfill_metric_daily.py --pool ashare_ex \
      [--start 2022-01-01] [--end 2026-09-12] [--workers 4]
  python scripts/factor_eval/backfill_metric_daily.py --pool zz1000
  python scripts/factor_eval/backfill_metric_daily.py --names size,amihud_20

设计（方案 §4.2）：
  - 每因子 = compute_factor_l0（IC/分组/分布/自相关/换手）
    + compute_factor_core_corr（暴露 exp_* 列 + L2 核心对，同查询产出）
  - 按信号日记账，尾部未成熟 horizon 列 NULL（每日流水线逐步补格）
  - L2 核心对只写全市场口径（方案 §3.6：相关层不复制池维度）
  - 多进程分片，每 worker 独立 DuckDB 只读连接（不碰主库写锁）
  - 断点续跑：L0 中已有且 max(date) >= end 的因子默认跳过（--force 重算）
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from quantlab.factor.metric_store import (
    FACTOR_DIR, POOL_DIR, STYLE_REPS, backfill_worker as _job,
    compute_factor_core_corr, compute_factor_l0, pool_registry,
    worker_init as _worker_init_proxy,
    write_corr_daily, write_l0)


def factor_names(args) -> list[str]:
    if args.names:
        return [s.strip() for s in args.names.split(",") if s.strip()]
    return sorted(d.name for d in FACTOR_DIR.iterdir() if d.is_dir())


def _done_factors(pool: str, end: str) -> set[str]:
    """批量断点扫描（单查询，勿逐因子扫 parquet——312×2 次全文件
    读取会把主进程卡 10 分钟+）。

    L0 与（全市场池的）L2 核心对都齐才算完成；corr 攒批在主进程
    内存，kill 会丢——丢 corr 的因子必须重跑补齐。
    """
    import duckdb
    d = POOL_DIR / pool
    if not d.exists():
        return set()
    con = duckdb.connect()
    try:
        l0 = con.execute(
            f"SELECT factor, max(date) AS d FROM read_parquet("
            f"'{d.as_posix()}/part-*.parquet', hive_partitioning=false) "
            f"GROUP BY factor").df()
        done = set(l0[l0["d"].astype(str).str[:10] >= end[:10]]["factor"])
        if done and pool == "ashare_ex":
            corr_f = con.execute(
                "SELECT DISTINCT factor FROM read_parquet("
                "'data/lake/factor_corr_daily/part-*.parquet')"
            ).df()["factor"]
            done &= set(corr_f)
        return done
    except Exception:
        return set()
    finally:
        con.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--pool", default="ashare_ex")
    p.add_argument("--start", default="2022-01-01")
    p.add_argument("--end", default=None, help="缺省=因子湖最大日期")
    p.add_argument("--names", help="逗号分隔子集（缺省全部）")
    p.add_argument("--workers", type=int, default=2,
                   help="并发 worker 数（资源约束下默认 2；worker 内"
                        "threads=2+memory_limit=2GB）")
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 个（调试）")
    p.add_argument("--force", action="store_true", help="跳过断点续跑检查")
    p.add_argument("--no-corr", action="store_true",
                   help="只回填 L0（不算 L2 核心对）")
    args = p.parse_args()

    reg = pool_registry().get(args.pool)
    if reg is None:
        sys.exit(f"池 {args.pool} 未注册")
    if not reg.get("active") or not reg.get("membership_source"):
        sys.exit(f"池 {args.pool} 无 PIT 成员历史（红线），禁止回填")

    import duckdb
    con = duckdb.connect()
    from quantlab.factor.metric_store import _factor_paths
    if args.end is None:
        args.end = str(con.execute(
            f"SELECT max(date) FROM read_parquet("
            f"{_factor_paths('size')})").fetchone()[0])
    con.close()

    names = factor_names(args)
    if args.limit:
        names = names[:args.limit]
    if not args.force:
        done = _done_factors(args.pool, args.end)
        names = [n for n in names if n not in done]
        if done:
            print(f"断点续跑：{len(done)} 因子已完成跳过，剩 {len(names)}",
                  flush=True)

    print(f"回填 pool={args.pool} {args.start}~{args.end} "
          f"{len(names)} 因子 workers={args.workers}", flush=True)
    jobs = [(n, args.pool, args.start, args.end) for n in names]

    import multiprocessing as mp
    t0 = time.time()
    n_ok, n_err = 0, 0
    corr_buf: list[pd.DataFrame] = []
    errors: list[str] = []
    FLUSH_EVERY = 20  # corr 分批落盘：kill 最多丢 20 因子的 L2

    def _flush_corr() -> int:
        if not corr_buf:
            return 0
        allc = pd.concat(corr_buf, ignore_index=True)
        corr_buf.clear()
        write_corr_daily(allc)
        return len(allc)

    ctx = mp.get_context("spawn")
    with ctx.Pool(
            args.workers,
            initializer=_worker_init_proxy,
            initargs=(args.start, str(args.end)[:10])) as pool:
        for i, (name, l0, cc) in enumerate(
                pool.imap_unordered(_job, jobs), 1):
            try:
                if not l0.empty:
                    write_l0(l0, args.pool)
                    n_ok += 1
                else:
                    n_err += 1
                    errors.append(f"{name}: L0 空")
                if (not args.no_corr and not cc.empty
                        and args.pool == "ashare_ex"):
                    corr_buf.append(cc)
                    if len(corr_buf) >= FLUSH_EVERY:
                        _flush_corr()
            except Exception as e:
                n_err += 1
                errors.append(f"{name}: {e}")
            if i % 20 == 0 or i == len(jobs):
                el = time.time() - t0
                print(f"  [{i}/{len(jobs)}] ok={n_ok} err={n_err} "
                      f"{el:.0f}s ({el/i:.1f}s/因子)", flush=True)

    flush_rows = _flush_corr()
    print(f"L2 核心对入库：{flush_rows:,} 行（本批）")

    print(f"完成：ok={n_ok} err={n_err} 总耗时 {(time.time()-t0)/60:.1f} 分钟")
    if errors:
        print("错误明细：")
        for e in errors:
            print(" ", e)


if __name__ == "__main__":
    main()
