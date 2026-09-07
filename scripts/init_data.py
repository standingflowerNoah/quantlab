#!/usr/bin/env python3
"""P1 全量首建脚本：日K线(含复权因子) → 财务快照(含行业回填)
用法: python3 scripts/init_data.py [--kline-only|--fin-only]
支持断点续跑（upsert 幂等，重复执行自动跳过已有数据）
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quantlab.config import get_logger
from quantlab.data.store import Store
from quantlab.data.instruments import all_codes
from quantlab.data import kline, financial

log = get_logger("init_data")


def main():
    t0 = time.time()
    store = Store()
    codes = all_codes(st_only=False)
    log.info(f"全量首建开始: {len(codes)} 只 A 股")

    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("--fin-only",):
        # K线（幂等：已入库的股票跳过可加速——检查已有覆盖）
        done = store.q("""
            SELECT code, MIN(date) AS f, MAX(date) AS l, COUNT(*) AS n
            FROM kline_daily GROUP BY code""")
        done_codes = set(done["code"]) if not done.empty else set()
        todo = [c for c in codes if c not in done_codes]
        log.info(f"K线: 已有 {len(done_codes)} 只, 待拉 {len(todo)} 只")
        if todo:
            kline.init_kline(codes=todo)
        else:
            log.info("K线已全部完成，跳过")

    if mode not in ("--kline-only",):
        try:
            fin_done = store.q("SELECT COUNT(*) AS n FROM finance_snapshot")
            n = int(fin_done.iloc[0, 0]) if not fin_done.empty else 0
        except Exception:
            n = 0                    # 表不存在（首次运行）
        if n > 5000:
            log.info(f"财务快照已有 {n} 条，跳过")
        else:
            financial.init_financial()

    # 导出 Parquet 镜像（供写锁被占时的无锁查询兜底）
    exported = store.export_mirror()
    log.info(f"首建完成，镜像导出 {len(exported)} 表")

    log.info(f"全量首建总耗时 {(time.time()-t0)/60:.1f} 分钟")


if __name__ == "__main__":
    main()
