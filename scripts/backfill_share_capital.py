"""全量回填 PIT 逐日股本表 share_capital_daily（幂等，可中断续跑）。

用法：
    python scripts/backfill_share_capital.py            # 续拉缺失
    python scripts/backfill_share_capital.py --force    # 全量重拉
    python scripts/backfill_share_capital.py --limit 200
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quantlab.data import share_capital as sc      # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true", help="不跳过已有 code")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--workers", type=int, default=sc.WORKERS)
    a = p.parse_args()

    r = sc.build(force=a.force, limit=a.limit, workers=a.workers)
    print("回填结果:", r)
    print("覆盖概况:", sc.coverage())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
