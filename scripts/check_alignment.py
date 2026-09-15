# -*- coding: utf-8 -*-
"""主库(DuckDB) vs 湖镜像(lake/clean/mirror) 对齐终检。

用法：
    $PY scripts/check_alignment.py            # 主库 vs mirror 逐表对比
    $PY scripts/check_alignment.py --verbose  # 附带每表行数明细

设计：
- 只读连接主库（read_only=True），写锁被占时退出码 2 并提示等待；
- mirror 侧用同一 DuckDB 连接 read_parquet 保证同口径；
- 对比维度：表存在性 / 行数 / 日期水位（date|trade_date|ann_date|pub_date）；
- 输出：stdout 汇总表 + reports/alignment_check_YYYYMMDD_HHMM.md 归档。
"""
import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402

DB = ROOT / "data" / "quant.duckdb"
MIRROR = ROOT / "data" / "lake" / "clean" / "mirror"
DATE_COLS = ["date", "trade_date", "ann_date", "pub_date", "report_date"]


def probe_date_col(con, ref: str) -> str | None:
    """ref 可以是表名或 parquet 路径；返回第一个命中的日期列名。"""
    if ref.endswith(".parquet"):
        cols = [r[0] for r in con.execute(
            f"SELECT name FROM parquet_schema('{ref}')").fetchall()]
    else:
        cols = [r[1] for r in con.execute(
            f"PRAGMA table_info('{ref}')").fetchall()]
    for c in DATE_COLS:
        if c in cols:
            return c
    return None


def stats(con, ref: str) -> tuple[int, str | None]:
    dcol = probe_date_col(con, ref)
    src = f"read_parquet('{ref}')" if ref.endswith(".parquet") else ref
    rows = con.execute(f"SELECT COUNT(*) FROM {src}").fetchone()[0]
    mx = None
    if dcol:
        mx = con.execute(f"SELECT MAX({dcol}) FROM {src}").fetchone()[0]
        mx = str(mx)[:10] if mx is not None else "-"
    return rows, mx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not DB.exists():
        print(f"主库不存在: {DB}")
        return 1
    try:
        con = duckdb.connect(str(DB), read_only=True)
    except duckdb.IOException as e:
        print(f"主库写锁被占，无法对齐终检，请等流水线/更新任务结束后重跑。\n{e}")
        return 2

    db_tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    pq_files = sorted(MIRROR.glob("*.parquet"))
    pq_tables = {p.stem for p in pq_files if "backup" not in p.stem}

    rows = []  # (table, db_rows, pq_rows, db_max, pq_max)
    for t in sorted(db_tables & pq_tables):
        f = MIRROR / f"{t}.parquet"
        r1, m1 = stats(con, t)
        r2, m2 = stats(con, str(f))
        rows.append((t, r1, r2, m1, m2))

    aligned = mis_rows = mis_date = 0
    lines = ["| 表 | 主库行数 | 镜像行数 | 主库水位 | 镜像水位 | 结论 |",
             "|---|---|---|---|---|---|"]
    for t, r1, r2, m1, m2 in rows:
        ok_r = r1 == r2
        ok_d = (m1 == m2) or (m1 is None and m2 is None)
        if ok_r and ok_d:
            verdict, aligned = "✅ 对齐", aligned + 1
        elif not ok_r:
            verdict, mis_rows = f"❌ 行数差 {r2 - r1:+,}", mis_rows + 1
        else:
            verdict, mis_date = "❌ 水位差", mis_date + 1
        lines.append(f"| {t} | {r1:,} | {r2:,} | {m1 or '-'} | {m2 or '-'} | {verdict} |")

    only_db = sorted(db_tables - pq_tables)
    only_pq = sorted(pq_tables - db_tables)

    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    head = [f"# 主库 vs 湖镜像对齐检查（{stamp}）", ""]
    if only_db:
        head.append(f"主库有、镜像未覆盖的表（{len(only_db)}）：{', '.join(only_db)}")
        head.append("")
    if only_pq:
        head.append(f"镜像有、主库缺失（{len(only_pq)}）：{', '.join(only_pq)}")
        head.append("")

    summary = (f"对比 {len(rows)} 表：对齐 {aligned} | 行数差 {mis_rows} | "
               f"水位差 {mis_date}；镜像未含主库表 {len(only_db)}")
    report = "\n".join(head + lines) + f"\n\n{summary}\n"

    out = ROOT / "reports" / f"alignment_check_{dt.datetime.now():%Y%m%d_%H%M}.md"
    out.write_text(report, encoding="utf-8")
    print(summary)
    if args.verbose or mis_rows or mis_date:
        print("\n".join(lines))
    print(f"报告: {out}")
    return 0 if (mis_rows == 0 and mis_date == 0) else 3


if __name__ == "__main__":
    sys.exit(main())
