"""ETF/基金 历史日线回补入口
================================================================
背景：etf.py::update_etf_daily 只查当日（start=end=当天）→ 湖里只有快照。
fsdb `日k` 对全部 2,053 只基金代码**都有历史**（最早 2004-03-22，
合计 2,303,259 行），可用大范围查询一次性回补。

用法：
  python scripts/backfill_etf_history.py                # 全历史（1990 起）
  python scripts/backfill_etf_history.py --start 20150101
  python scripts/backfill_etf_history.py --codes 510300,159915

产出：data/lake/clean/etf_daily/year=YYYY/part-full.parquet（按年一个文件）
重复运行幂等（同年整体覆盖重写，并吸收清理该年 part-d*.parquet）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quantlab.data.etf import backfill_etf_daily       # noqa: E402
from quantlab.data.etf import load_etf_daily           # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="ETF 历史日线回补")
    ap.add_argument("--start", default="19900101")
    ap.add_argument("--end", default=None)
    ap.add_argument("--codes", default=None, help="逗号分隔，默认全量基金代码")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    codes = a.codes.split(",") if a.codes else None
    r = backfill_etf_daily(a.start, a.end, codes, a.workers)
    print("\n回补结果:", r)

    df = load_etf_daily(etf_only=False)
    print(f"湖内合计 {len(df):,} 行 / {df['code'].nunique()} 只 / "
          f"{df['date'].min().date()} -> {df['date'].max().date()}")


if __name__ == "__main__":
    main()
