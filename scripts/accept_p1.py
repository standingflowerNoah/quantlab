#!/usr/bin/env python3
"""P1 数据层验收：首建完成后一键跑全链路验证

顺序：
1. 补漏重跑 K线（幂等，补拉 fail 的股票）
2. 导出 Parquet 镜像
3. 刷新指数成分（datacenter 接口）
4. 特色数据域更新（龙虎榜/两融/解禁/大宗/热点/北向）
5. 数据体检 quality
6. 股票池规模一览
用法: python3 scripts/accept_p1.py [--quick 跳过补漏]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from quantlab.config import get_logger
from quantlab.data.store import Store, query

log = get_logger("accept_p1")


def step(title: str):
    log.info(f"\n{'='*56}\n▶ {title}\n{'='*56}")


def main():
    t0 = time.time()
    quick = "--quick" in sys.argv
    store = Store()

    # 1. K线补漏（幂等：只拉缺的）
    if not quick:
        step("1/6 K线补漏（幂等重跑）")
        from quantlab.data.kline import init_kline
        from quantlab.data.instruments import all_codes
        done = store.q("""
            SELECT code, MAX(date) AS l FROM kline_daily
            GROUP BY code""")
        # 完整标准：末根K线 >= 库内全局最新交易日
        latest = store.q("SELECT MAX(date) AS d FROM kline_daily").iloc[0, 0]
        ok_codes = set(done[done["l"] >= latest]["code"])
        todo = [c for c in all_codes(st_only=False) if c not in ok_codes]
        log.info(f"覆盖 {len(ok_codes)} 只，补拉 {len(todo)} 只")
        if todo:
            init_kline(codes=todo)

    # 2. 镜像导出
    step("2/6 导出 Parquet 镜像")
    exported = store.export_mirror()
    log.info(f"镜像 {len(exported)} 表: {', '.join(exported)}")

    # 3. 指数成分 + 行业回填
    step("3/6 指数成分 + 行业回填（datacenter）")
    from quantlab.data.universe import refresh_index_members
    r = refresh_index_members()
    log.info(f"指数成分: {r}")
    from quantlab.data.instruments import backfill_industry
    n_ind = backfill_industry()
    log.info(f"行业回填: {n_ind} 只")

    # 4. 特色数据域
    step("4/6 特色数据域更新")
    from quantlab.data.update import update_all
    results = update_all(domains=["dragon_tiger", "margin_total", "lockup",
                                  "block_trade", "hot_topic",
                                  "northbound_daily"])
    for k, v in results.items():
        log.info(f"  {k:20s} {v}")

    # 5. 体检
    step("5/6 数据体检")
    from quantlab.data.quality import check_all
    issues = check_all(sample=30)
    print(issues.to_string(index=False))

    # 6. 股票池
    step("6/6 股票池规模")
    from quantlab.data.universe import describe_universes
    print(describe_universes().to_string(index=False))

    # 汇总
    step("P1 验收汇总")
    stats = query("""
        SELECT 'kline_daily' AS domain, COUNT(*) AS rows,
               COUNT(DISTINCT code) AS codes FROM kline_daily
        UNION ALL
        SELECT 'dividend_events', COUNT(*), COUNT(DISTINCT code)
          FROM dividend_events
        UNION ALL
        SELECT 'finance_snapshot', COUNT(*), COUNT(DISTINCT code)
          FROM finance_snapshot
        UNION ALL
        SELECT 'index_kline', COUNT(*), COUNT(DISTINCT code)
          FROM index_kline""")
    print(stats.to_string(index=False))
    log.info(f"验收总耗时 {(time.time()-t0)/60:.1f} 分钟")


if __name__ == "__main__":
    main()
