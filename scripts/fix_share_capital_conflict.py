"""修复 share_capital_daily 的股本冲突（total_shares < float_shares）

背景
----
硬不变量：总股本 >= 流通股本（恒真）。2026-09-12 复核 1629 万行发现两类违反：

1. **浮点噪声**（占绝大多数）：如 601166 在 2026-01-05 报
   total=211.628519 亿 / float=211.628552 亿，相对差 1.5e-7。
2. **上游字段滞后**（少数）：如 600372 中航电子 2023-04-19 换股吸收合并后
   float 立即跳到 44.85 亿（新股上市），而 total 仍停在合并前的 19.18 亿，
   直到 2023-07-17 配套融资落地才更新为 48.39 亿。
   → 窗口期内 total 被低估 2.34 倍，EP/BP/SP/OCFP 的市值分母随之失真。
3. **北交所**（305 只，池外）：920009 等 total 仅 1008 万股、float 7754 万股，
   疑为两字段语义互换。

处理
----
统一 `total_shares = float_shares`（当 total 显著小于 float 时），因为
- 由 600372 案例可知 **float 字段是及时更新的那个**，total 会滞后；
- 该修改是保守方向：只会「提高」总股本、把不可能值拉回下界，不会高估。

写入前自动备份为 parquet，`source` 标记为 `fsdb+clamp` 便于审计与回滚。

用法：python scripts/fix_share_capital_conflict.py [--tol 1e-6]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from quantlab.config import CLEAN_DIR, get_logger
from quantlab.data.store import Store

log = get_logger(__name__)
TABLE = "share_capital_daily"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tol", type=float, default=1e-6,
                    help="相对容差：仅当 total < float*(1-tol) 才修（默认 1e-6）")
    args = ap.parse_args()

    store = Store()
    con = store.con

    n_all, n_bad = con.execute(
        f"SELECT count(*), sum(CASE WHEN total_shares < float_shares * (1-?) "
        f"THEN 1 ELSE 0 END) FROM {TABLE}", [args.tol]).fetchone()
    print(f"表规模 {n_all:,} 行；待修（total < float*(1-{args.tol:g})）={int(n_bad):,} 行")
    if not n_bad:
        print("无需修复")
        return

    # ① 备份
    bak_dir = CLEAN_DIR / "mirror"
    stamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M")
    bak = bak_dir / f"{TABLE}_backup_{stamp}.parquet"
    con.execute(f"COPY (SELECT * FROM {TABLE}) TO '{bak}' (FORMAT PARQUET)")
    print(f"备份已写入: {bak}")

    # ② 修复前分布
    print("\n修复前（按 ratio 分档）:")
    print(pd.read_sql(f"""
        SELECT CASE
                 WHEN float_shares/total_shares < 1.000001 THEN 'A <1.000001（浮点噪声）'
                 WHEN float_shares/total_shares < 1.001    THEN 'B <1.001（微小）'
                 WHEN float_shares/total_shares < 1.1      THEN 'C <1.1'
                 ELSE 'D >=1.1（实质错误）' END AS bucket,
               count(*) n, count(DISTINCT code) codes,
               sum(CASE WHEN code LIKE '92%' OR code LIKE '83%'
                         OR code LIKE '87%' OR code LIKE '43%' THEN 1 ELSE 0 END) bj
        FROM {TABLE}
        WHERE total_shares < float_shares * (1-{args.tol})
        GROUP BY 1 ORDER BY 1
    """, con).to_string(index=False))

    # ③ 修复
    con.execute(f"""
        UPDATE {TABLE}
        SET total_shares = float_shares,
            source = source || '+clamp'
        WHERE total_shares < float_shares * (1-?)
    """, [args.tol])
    con.commit()

    # ④ 复核
    left = con.execute(
        f"SELECT count(*) FROM {TABLE} WHERE total_shares < float_shares").fetchone()[0]
    print(f"\n修复后残余违反（严格 <）: {left}")
    store.set_watermark(TABLE, pd.Timestamp.now().date())
    print("完成。可回滚：从备份 parquet 重建该表。")


if __name__ == "__main__":
    main()
