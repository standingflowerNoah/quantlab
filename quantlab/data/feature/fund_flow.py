"""个股资金流（东财 push2his，日级）
⚠️ 2026-09-02 沙箱网络状态：push2his.eastmoney.com 连接被断（RemoteDisconnected）
   采集器保持就绪，quality 模块将此域标记 degraded；网络恢复后自动可用。
   历史数据只能滚动积累（东财该接口仅返回最近120日）。
"""
from __future__ import annotations

import time

import pandas as pd

from ...config import get_logger
from ..store import Store
from ..sources import eastmoney_source as em

log = get_logger(__name__)

DDL = """
date DATE, code VARCHAR, main_net DOUBLE, super_net DOUBLE,
large_net DOUBLE, mid_net DOUBLE, small_net DOUBLE,
PRIMARY KEY(date, code)
"""


def probe_available() -> bool:
    """探测 push2his 是否恢复可用"""
    try:
        df = em.fund_flow_daily("600519")
        return not df.empty
    except Exception:
        return False


def update_fund_flow(codes: list[str] | None = None,
                     sample: int | None = None) -> int:
    """个股资金流日级（每只一次请求，1s限流；东财仅提供最近120日）

    sample: 抽样更新数量（如 500 只按流通市值前N），None=全量
    """
    if not probe_available():
        log.warning("资金流域不可用（push2his 网络断连），跳过。"
                    "待网络恢复后自动启用。")
        return -1
    store = Store()
    store.ensure_table("fund_flow_daily", DDL)
    store.register_dataset("fund_flow_daily", "clean", "eastmoney", "daily",
                           "个股资金流日级（元，滚动120日）")
    if codes is None:
        from ..instruments import all_codes
        codes = all_codes()
    if sample:
        codes = codes[:sample]
    total = 0
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        df = em.fund_flow_daily(code)
        if df is None or df.empty:
            continue
        total += store.upsert(df, "fund_flow_daily", ["date", "code"])
        if i % 100 == 0:
            log.info(f"资金流进度 {i}/{len(codes)} ({time.time()-t0:.0f}s)")
    if total:
        wm = store.q("SELECT MAX(date) FROM fund_flow_daily").iloc[0, 0]
        store.set_watermark("fund_flow_daily", wm)
    log.info(f"资金流完成: {total} 行")
    return total
