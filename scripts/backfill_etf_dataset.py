"""ETF 数据集回补入口（2026-09-12 新增四类数据）
================================================================
用法：
  python scripts/backfill_etf_dataset.py --what all
  python scripts/backfill_etf_dataset.py --what minute --start 20250102
  python scripts/backfill_etf_dataset.py --what nav --start 20120101
  python scripts/backfill_etf_dataset.py --what adj
  python scripts/backfill_etf_dataset.py --what holding

四类数据（源与形态）：
| 数据 | 源 | 深度 | 形态 |
|---|---|---|---|
| minute  | fsdb     | 2025-01-02 起（硬边界） | 区间查询，可全量回补 |
| nav     | westock  | 上市首日（实测 2012 起） | 区间查询，**单次≤1211行须切段** |
| adj     | fsdb     | 全史（仅 18.5% 标的有分红事件） | 事件表，一次性 |
| holding | westock  | 仅当日（`--date` 不生效） | **快照型，只能前向积累** |
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    ap = argparse.ArgumentParser(description="ETF 数据集回补")
    ap.add_argument("--what", default="all",
                    choices=["minute", "nav", "adj", "holding", "all"])
    ap.add_argument("--start", default=None, help="YYYYMMDD")
    ap.add_argument("--end", default=None, help="YYYYMMDD")
    a = ap.parse_args()

    if a.what in ("minute", "all"):
        from quantlab.data import etf_minute as m
        print("▶ ETF 分钟:", m.backfill(a.start or m.MIN_START, a.end))
        print("  覆盖:", m.coverage())
    if a.what in ("nav", "all"):
        from quantlab.data import etf_nav as n
        print("▶ ETF 净值:", n.backfill(a.start or "20040101", a.end))
        print("  覆盖:", n.coverage())
    if a.what in ("adj", "all"):
        from quantlab.data import etf_adj as j
        print("▶ ETF 复权:", j.backfill())
    if a.what in ("holding", "all"):
        from quantlab.data import etf_holding as h
        print("▶ ETF 持仓快照:", h.snapshot())
        print("  覆盖:", h.coverage())


if __name__ == "__main__":
    main()
