#!/usr/bin/env python3
"""历史季度财报回补（westock finance，腾讯源）→ data/lake/clean/fundamental/
=============================================================================
数据源：westock CLI（C:/Users/53497/.local/bin/westock.exe，已装 v0.0.4）
  - westock finance <codes> --type {income,balance,cashflow} --fields all
    --start 2015-01-01 --end 2026-12-31
  - 批量须同市场；输出 Markdown 管道表
  - --fields all 含 InfoPublDate（披露日，PIT 对齐核心字段）

产出：data/lake/clean/fundamental/finance_q/{income,balance,cashflow}/part-NNN.parquet
  - 列保留 westock 原字段名（驼峰），code 列为 6 位裸代码
  - 断点续传：按批号落 part 文件，重跑自动跳过已完成批次
工程约定：
  - 不写主库（单写者），直接落 parquet
  - "-" → NULL；空表跳过；失败批次重试 2 次后记录
用法：
  python scripts/fundamental_finance_backfill.py [--batch-size 60] [--market sh]
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT_BASE = ROOT / "data/lake/clean/fundamental/finance_q"
WESTOCK = Path(r"C:\Users\53497\.local\bin\westock.exe")
START, END = "2015-01-01", "2026-12-31"
TABLES = ("income", "balance", "cashflow")


def code_prefix(code: str) -> str:
    """6 位裸代码 → westock 市场前缀"""
    if code.startswith(("6",)):
        return "sh"
    if code.startswith(("00", "30")):
        return "sz"
    if code.startswith(("43", "83", "87", "88", "92")):
        return "bj"
    return "sz"


def load_codes() -> list[str]:
    """从 instruments parquet 取全量代码（含市场分布）"""
    import duckdb
    con = duckdb.connect()
    df = con.execute("""
        SELECT code FROM read_parquet(
            'C:/Users/53497/WorkBuddy/2026-09-02-23-42-28/quantlab/data/lake/clean/mirror/instruments.parquet')
    """).fetch_df()
    return sorted(df["code"].astype(str).str.zfill(6).unique())


def parse_md_table(text: str, batch_codes: dict[str, str]) -> pd.DataFrame | None:
    """解析 westock Markdown 管道表 → DataFrame；空/异常返回 None"""
    header = None
    rows = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue  # 分隔行
        if header is None:
            header = cells
            continue
        rows.append(cells)
    if header is None or not rows:
        return None
    df = pd.DataFrame(rows, columns=header)
    # 批量模式 code 列是 sh600519；单股模式无 code 列（本项目始终批量）
    if "code" in df.columns:
        df["code"] = df["code"].astype(str).str.replace(
            r"^(sh|sz|bj)", "", regex=True)
    else:
        return None
    # "-" → NA；数值列转 float
    df = df.replace("-", pd.NA).replace("", pd.NA)
    for c in df.columns:
        if c in ("code", "EndDate", "InfoPublDate", "_type", "EnterpriseType",
                 "SecuCode"):
            continue
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for dcol in ("EndDate", "InfoPublDate"):
        if dcol in df.columns:
            df[dcol] = pd.to_datetime(df[dcol], errors="coerce")
    return df


def call_westock(table: str, codes: list[str], retry: int = 2) -> str | None:
    """调用 westock finance，返回 stdout 文本；失败重试"""
    arg = ",".join(codes)
    for attempt in range(retry + 1):
        try:
            r = subprocess.run(
                [str(WESTOCK), "finance", arg, "--type", table,
                 "--fields", "all", "--start", START, "--end", END],
                capture_output=True, timeout=300, encoding="utf-8", errors="replace")
            if r.returncode == 0 and r.stdout:
                return r.stdout
            if "未找到" in (r.stdout or "") + (r.stderr or ""):
                return r.stdout or ""
        except subprocess.TimeoutExpired:
            pass
        time.sleep(2 * (attempt + 1))
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-size", type=int, default=60)
    ap.add_argument("--market", choices=["sh", "sz", "bj", "all"], default="all")
    args = ap.parse_args()

    for t in TABLES:
        (OUT_BASE / t).mkdir(parents=True, exist_ok=True)

    codes_all = load_codes()
    by_mkt: dict[str, list[str]] = {"sh": [], "sz": [], "bj": []}
    for c in codes_all:
        by_mkt[code_prefix(c)].append(c)
    mkts = [args.market] if args.market != "all" else ["sh", "sz", "bj"]
    codes = [c for m in mkts for c in by_mkt[m]]
    print(f"[codes] total={len(codes)}  "
          + "  ".join(f"{m}={len(by_mkt[m])}" for m in mkts), flush=True)

    # 组批：同市场内切 batch-size（westock 批量须同市场，代码带前缀）
    batches: list[tuple[int, list[str]]] = []
    i = 0
    for m in mkts:
        lst = by_mkt[m]
        for j in range(0, len(lst), args.batch_size):
            batches.append((i, [m + c for c in lst[j:j + args.batch_size]]))
            i += 1
    print(f"[batches] {len(batches)} 批 × {len(TABLES)} 表", flush=True)

    t0 = time.time()
    fails: list[tuple[int, str]] = []
    for bidx, bcodes in batches:
        done = all((OUT_BASE / t / f"part-{bidx:03d}.parquet").exists()
                   for t in TABLES)
        if done:
            continue
        for t in TABLES:
            p = OUT_BASE / t / f"part-{bidx:03d}.parquet"
            if p.exists():
                continue
            out = call_westock(t, bcodes)
            if out is None:
                fails.append((bidx, t))
                print(f"  [FAIL] batch={bidx} table={t}", flush=True)
                continue
            df = parse_md_table(out, None)
            if df is None or len(df) == 0:
                # 该批全部无数据也落空标记文件，避免死循环重试
                pd.DataFrame(columns=["code"]).to_parquet(p, index=False)
                continue
            df.to_parquet(p, index=False)
        if bidx % 5 == 0:
            el = time.time() - t0
            print(f"  {bidx}/{len(batches)}  elapsed={el:.0f}s  "
                  f"fails={len(fails)}", flush=True)

    print(f"[done] batches={len(batches)} fails={len(fails)} "
          f"elapsed={time.time()-t0:.0f}s", flush=True)
    if fails:
        print("  failed:", fails[:20], flush=True)


if __name__ == "__main__":
    main()
