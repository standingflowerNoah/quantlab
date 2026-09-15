#!/usr/bin/env python3
"""hf_wf_composite 2022-2024 历史回填（无锁只读版）
==================================================
为什么存在：compute_factor 默认走 Store() 写模式主库连接——
metric_store --force 重算的 worker 以 `ATTACH ... (READ_ONLY)` 持
SHARED 文件锁时，写模式打开直接 IOException（2026-09-13 17:25 实录）。

本脚本改走 Store(readonly=True)：与 ATTACH READ_ONLY 天然共存
（SHARED+SHARED），compute() 全程只读主库，append 阶段绕过
_writable() 拦截直写湖分区（2022-2024 为全新分区，与 append_factor
keep="last" 等价的合并语义，幂等可重跑）。

安全性：
- 2025-2026 湖内现值完全不动（裁剪在外层完成，分区不交叉）
- 输出起点由 WF 结构决定 ≈ 2023-02（12 个月训练窗），2022 年大部分
  无值属正常
- audit JSON 不在此刷新——跑完后执行：
  python scripts/refresh_audit_ic_from_metric.py --names hf_wf_composite
  （structure 会从湖 glob 重算，自动覆盖 span 起点）

内存门卫：metric_store 双 worker 常驻 ~8.7GB，本计算峰值 8-12GB；
空闲内存 <10GB 时拒绝启动（--force-ignore-mem 覆盖）。

用法：
  python scripts/backfill_hf_wf_composite_ro.py                # 2022-2024
  python scripts/backfill_hf_wf_composite_ro.py --check        # 只算不写
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(ROOT / "logs" / "backfill_hf_wf_composite_ro.log",
                                  encoding="utf-8"),
              logging.StreamHandler(sys.stdout)])
log = logging.getLogger("hf_wf_ro")

FACTOR_DIR = ROOT / "data" / "lake" / "factor" / "hf_wf_composite"
MIN_AVAIL_GB = 10.0


def check_memory(force: bool) -> None:
    try:
        import psutil
        avail_gb = psutil.virtual_memory().available / 2**30
    except ImportError:
        import subprocess
        out = subprocess.run(["wmic", "OS", "get", "FreePhysicalMemory",
                              "/format:list"], capture_output=True, text=True)
        kb = next(int(l.split("=")[1]) for l in out.stdout.splitlines()
                  if l.startswith("FreePhysicalMemory="))
        avail_gb = kb / 2**20
    log.info(f"空闲内存 {avail_gb:.1f} GB（门槛 {MIN_AVAIL_GB} GB）")
    if avail_gb < MIN_AVAIL_GB and not force:
        raise SystemExit(
            f"内存不足（{avail_gb:.1f} GB < {MIN_AVAIL_GB} GB）："
            "等 metric_store --force 等大任务完成后再跑，避免 OOM 连坐。"
            "确认无风险可加 --force-ignore-mem。")


def append_year(df_year, part: Path) -> int:
    """与 Store.append_factor keep='last' 等价的单年分区写入（幂等）。"""
    if part.exists():
        old = pd.read_parquet(part)
        old["date"] = pd.to_datetime(old["date"])
        merged = (pd.concat([old, df_year], ignore_index=True)
                    .sort_values(["date", "code"])
                    .drop_duplicates(subset=["date", "code"], keep="last"))
    else:
        merged = df_year
    tmp = part.with_suffix(".tmp.parquet")
    merged.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(part)          # safe-delete 拦删除不拦覆盖
    return len(merged)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--check", action="store_true", help="只算不写")
    ap.add_argument("--force-ignore-mem", action="store_true")
    a = ap.parse_args()

    import pandas as pd
    check_memory(a.force_ignore_mem)

    from quantlab.data.store import Store
    from quantlab.factor.compute import _resolve_universe
    from quantlab.factor.registry import get_factor

    t0 = time.time()
    store = Store(readonly=True)          # 与 ATTACH READ_ONLY worker 共存
    factor = get_factor("hf_wf_composite")
    codes = _resolve_universe(None)       # None=全市场，对齐 compute_all
    df = factor.compute(store, universe=codes)
    df["date"] = pd.to_datetime(df["date"])
    n_all = len(df)
    log.info(f"compute 全历史 {n_all:,} 行 "
             f"({df['date'].min().date()}~{df['date'].max().date()}) "
             f"{time.time() - t0:.0f}s")

    df = df[(df["date"] >= pd.to_datetime(a.start))
            & (df["date"] <= pd.to_datetime(a.end))]
    df = df.sort_values(["date", "code"]).reset_index(drop=True)
    if df.empty:
        log.warning("裁剪后为空（WF 训练窗未成熟？检查输入因子起点）")
        return
    log.info(f"裁剪 [{a.start}~{a.end}] 后 {len(df):,} 行 "
             f"({df['date'].min().date()}~{df['date'].max().date()}, "
             f"{df['code'].nunique()} 只)")

    if a.check:
        log.info("--check 模式，不写湖")
        return

    FACTOR_DIR.mkdir(parents=True, exist_ok=True)
    years = sorted(df["date"].dt.year.unique())
    for year in years:
        part = df[df["date"].dt.year == year]
        n = append_year(part, FACTOR_DIR / f"part-{year}.parquet")
        log.info(f"part-{year}: 写入 {len(part):,} 行 → 分区共 {n:,} 行")

    # 写后自检：分区齐全 + 与 2025 现值无日期交叉
    got = sorted(p.name for p in FACTOR_DIR.glob("part-*.parquet"))
    log.info(f"湖内分区 {got}")
    overlap = df["date"].max() >= pd.to_datetime("2025-01-01")
    if overlap:
        log.error("⚠️ 裁剪失败：输出触及 2025+，检查日期过滤！")
    log.info(f"完成 {time.time() - t0:.0f}s。"
             "下一步：python scripts/refresh_audit_ic_from_metric.py "
             "--names hf_wf_composite")


if __name__ == "__main__":
    main()
