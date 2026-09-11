"""重建后校验：因子 parquet 行数/覆盖 + 缺失股本股残留 + 与旧备份的差异

2026-09-12 用于验证 as-of 根治后的 11 个因子重建结果。
"""
from __future__ import annotations

import duckdb

FACTORS = ["size", "total_mcap", "turnover", "turnover_std_20", "sp", "ocfp",
           "chip_vwap_bias_250", "dragon_net_20", "lockup_pressure_60", "ep", "bp"]
BACKUP = "data/backup_factors_20260911"


def main() -> None:
    con = duckdb.connect(":memory:")
    print("=" * 78)
    print("① 重建后因子概览")
    print("=" * 78)
    print(f"{'factor':22s} {'rows':>10s} {'codes':>6s}  {'range':23s} {'688808':>7s}")
    for f in FACTORS:
        row = con.execute(
            f"SELECT count(*), count(DISTINCT code), min(date), max(date) "
            f"FROM read_parquet('data/lake/factor/{f}/part-*.parquet')").fetchone()
        has = con.execute(
            f"SELECT count(*) FROM read_parquet('data/lake/factor/{f}/part-*.parquet') "
            f"WHERE code='688808'").fetchone()[0]
        print(f"{f:22s} {row[0]:>10,} {row[1]:>6d}  {str(row[2])}~{str(row[3])} {has:>7d}")

    print()
    print("=" * 78)
    print("② 缺失股本的新上市股：是否已被清出因子文件")
    print("=" * 78)
    for f in ["size", "turnover", "dragon_net_20"]:
        n = con.execute(
            f"SELECT count(*) FROM read_parquet('data/lake/factor/{f}/part-*.parquet') t "
            f"WHERE NOT EXISTS (SELECT 1 FROM "
            f"read_parquet('data/lake/clean/mirror/share_capital_daily/*.parquet') s "
            f"WHERE s.code=t.code)").fetchone()[0]
        print(f"  {f:20s} 无股本源的行数 = {n}")

    print()
    print("=" * 78)
    print("③ 与旧值备份的差异（rank 位移）")
    print("=" * 78)
    import os
    have = set(os.listdir(BACKUP)) if os.path.isdir(BACKUP) else set()
    print(f"  备份目录文件：{sorted(have)}")


if __name__ == "__main__":
    main()
