#!/usr/bin/env python3
"""特色数据域历史回补 CLI
=====================================
用法（与 data update 串行，DuckDB 单写者）：
  $PY scripts/backfill_data.py                    # 全部域，2022-01-01 起
  $PY scripts/backfill_data.py --domains margin_total,dragon_tiger
  $PY scripts/backfill_data.py --start 2023-01-01
"""
import argparse
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

from quantlab.config import get_logger
from quantlab.data.feature.backfill import BACKFILLS

log = get_logger("backfill")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domains", default="",
                    help="逗号分隔域列表，默认全部可回补域")
    ap.add_argument("--start", default="2022-01-01")
    args = ap.parse_args()

    domains = ([d.strip() for d in args.domains.split(",") if d.strip()]
               or list(BACKFILLS))
    for d in domains:
        if d not in BACKFILLS:
            log.error(f"未知域 {d}（可用: {', '.join(BACKFILLS)}）")
            return
    log.info(f"回补开始: {domains}（自 {args.start}）")
    t0 = time.time()
    for d in domains:
        try:
            n = BACKFILLS[d](args.start)
            log.info(f"[{d}] 回补完成: {n}")
        except Exception as e:
            log.error(f"[{d}] 回补失败: {e}")
    log.info(f"全部回补完成，总耗时 {(time.time()-t0)/60:.1f} 分钟")


if __name__ == "__main__":
    main()
