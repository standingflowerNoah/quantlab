"""数据更新调度器：按域编排增量更新，域间独立失败不阻塞"""
from __future__ import annotations

import time
from datetime import timedelta

import pandas as pd

from ..config import get_logger
from .store import Store

log = get_logger(__name__)


def _days_since(d) -> int:
    if d is None:
        return 999
    return (pd.Timestamp.now().normalize() - pd.Timestamp(d).normalize()).days


def update_all(date=None, domains: list[str] | None = None,
               full_kline: bool = False) -> dict:
    """执行日度增量更新流水线（盘后运行）

    域顺序：日历 → 股票主表/快照 → K线 → 指数 → 特色数据(龙虎榜/两融/解禁/
    大宗/热点/北向) → 财务(周) → 股东户数/业绩预告(周) → 资金流(可用时)
    """
    from . import calendar
    results: dict[str, str] = {}

    def run(domain: str, fn):
        if domains and domain not in domains:
            return
        t0 = time.time()
        try:
            n = fn()
            results[domain] = f"ok ({n}) {time.time()-t0:.0f}s"
        except Exception as e:
            results[domain] = f"FAIL: {str(e)[:80]}"
            log.error(f"[{domain}] {e}")

    store = Store()
    # 交易日判断（重要）：trade_calendar 只含"已完成"的交易日（来自指数K线），
    # 盘前运行（如 07:00 定时任务）时"今天"必然不在日历中，仅用 is_trading_day
    # 判断会导致定时任务永远跳过数据更新。因此补充"待补数据"判断：
    # 日历最大交易日 > K线最大日期 ⇒ 上个交易日收盘数据尚未拉取，需完整更新。
    is_td = calendar.is_trading_day(pd.Timestamp.now())
    cal_max = store.q("SELECT max(trade_date) AS d FROM trade_calendar")["d"][0]
    kline_max = store.q("SELECT max(date) AS d FROM kline_daily")["d"][0]
    pending = (cal_max is not None and
               (kline_max is None
                or pd.Timestamp(cal_max) > pd.Timestamp(kline_max)))
    if (not domains and not is_td and not pending and date is None):
        log.info(f"今日非交易日且无待补数据（日历至 {cal_max}，K线至 {kline_max}），"
                 f"执行轻量检查（日历/水位）")
        run("calendar", lambda: calendar.refresh_calendar())
        # 轻量路径复查：盘后运行时 refresh_calendar 会把"今天"补进日历，
        # 此时 cal_max > kline_max ⇒ 上一交易日数据待补，继续完整更新
        cal_max2 = store.q("SELECT max(trade_date) AS d FROM trade_calendar")["d"][0]
        kline_max2 = store.q("SELECT max(date) AS d FROM kline_daily")["d"][0]
        if (cal_max2 is not None and
                (kline_max2 is None
                 or pd.Timestamp(cal_max2) > pd.Timestamp(kline_max2))):
            log.info(f"日历刷新后出现待补数据（日历至 {cal_max2}，"
                     f"K线至 {kline_max2}），继续完整更新")
        else:
            return results

    # 1. 日历（顺延刷新未来）
    run("calendar", lambda: calendar.refresh_calendar())
    # 2. 股票主表 + 当日估值快照
    from .instruments import refresh_instruments, update_daily_snapshot
    run("instruments", refresh_instruments)
    run("daily_snapshot", lambda: update_daily_snapshot(date))
    # 3. K线增量
    from .kline import update_kline, update_index_kline, init_kline
    if full_kline:
        run("kline_daily", lambda: init_kline())
    else:
        run("kline_daily", lambda: update_kline())
    run("index_kline", update_index_kline)
    # 3.5 分钟K线（free-stockdb 本地引擎：镜像增量同步 → 入湖）
    #     首次使用需先单独执行 cli.py data minute-init 全量回补
    from .minute import update_kline_1min
    from .sources.fsdb_source import sync as fsdb_sync
    run("fsdb_sync", fsdb_sync)
    run("kline_1min", update_kline_1min)
    # 4. 特色数据
    from .feature.dragon_tiger import update_dragon_tiger
    from .feature.eastmoney_features import (update_margin, update_lockup,
                                             update_block_trade)
    from .feature.ths_features import update_hot_topic, update_northbound
    from .feature.fund_flow import update_fund_flow
    run("dragon_tiger", lambda: update_dragon_tiger(date))
    run("margin_total", lambda: update_margin(date))
    run("lockup", lambda: update_lockup(date))
    run("block_trade", lambda: update_block_trade(date))
    run("hot_topic", lambda: update_hot_topic(date))
    run("northbound_daily", lambda: update_northbound(date))
    # 5. 低频域（按水位间隔触发）
    wm = store.get_watermark("finance_snapshot")
    if _days_since(wm) >= 7:
        from .financial import init_financial
        run("finance_snapshot", init_financial)
    wm = store.get_watermark("finance_history")
    if _days_since(wm) >= 7:
        from .finance_history import update_finance_history
        run("finance_history", update_finance_history)
    wm = store.get_watermark("holder_num")
    if _days_since(wm) >= 7:
        # 2026-09-07 切换至全历史版（RPT_HOLDERNUM_DET，披露日 PIT；
        # 旧"最新一期快照"版已废弃，旧表留档 holder_num_legacy_20260907）
        from .holder_num import update_holder_num
        run("holder_num", update_holder_num)
    wm = store.get_watermark("perf_forecast")
    if _days_since(wm) >= 7:
        from .perf_forecast import update_perf_forecast
        run("perf_forecast", update_perf_forecast)
    wm = store.get_watermark("index_members")
    if _days_since(wm) >= 7:
        from .universe import refresh_index_members
        run("index_members", refresh_index_members)
    wm = store.get_watermark("industry_map")
    if _days_since(wm) >= 7:
        from .instruments import backfill_industry
        run("industry_map", backfill_industry)
    # 6. 资金流（探测可用才跑，抽市值前500）
    from .feature.fund_flow import probe_available
    if probe_available():
        def _ff():
            codes = store.q("""
                SELECT s.code FROM daily_snapshot s
                JOIN instruments i ON i.code = s.code
                WHERE s.date = (SELECT MAX(date) FROM daily_snapshot)
                  AND NOT i.is_st AND i.board != 'BJ'
                ORDER BY s.float_mcap_yi DESC NULLS LAST LIMIT 500""")
            from .feature.fund_flow import update_fund_flow
            return update_fund_flow(codes=codes["code"].tolist())
        run("fund_flow_daily", _ff)
    else:
        results["fund_flow_daily"] = "degraded (push2his 不可达)"

    log.info("更新完成: " + " | ".join(f"{k}:{v.split(' ')[0]}" for k, v in results.items()))

    # 导出 Parquet 镜像（供写锁被占时的无锁查询兜底）
    try:
        exported = store.export_mirror()
        log.info(f"镜像导出 {len(exported)} 表")
    except Exception as e:
        log.warning(f"镜像导出失败（不影响数据更新）: {e}")
    return results


def update_plan() -> pd.DataFrame:
    """数据域状态总览：水位/新鲜度/行数（只读，可镜像降级）"""
    from .store import query
    wm = query("""
        SELECT w.domain, w.partition, w.watermark, w.updated_at,
               d.frequency, d.source
        FROM watermarks w LEFT JOIN datasets d USING(domain)
        ORDER BY w.domain""")
    import duckdb as _ddb

    def _q(sql):
        try:
            return query(sql)
        except Exception:
            return None

    rows = []
    for _, w in wm.iterrows():
        table = w["domain"]
        cnt_df = _q(f"SELECT COUNT(*) AS n, MAX(date) AS d FROM {table}")
        if cnt_df is None or cnt_df.empty:
            cnt, latest = "-", "-"
        else:
            cnt = int(cnt_df.iloc[0, 0])
            latest = cnt_df.iloc[0, 1]
        rows.append({
            "domain": w["domain"], "source": w.get("source"),
            "frequency": w.get("frequency"),
            "watermark": str(w["watermark"]),
            "latest_row": str(latest)[:10] if latest is not None and str(latest) != "None" else "-",
            "rows": cnt,
            "days_ago": _days_since(w["watermark"]) if w["watermark"] else None,
        })
    return pd.DataFrame(rows)
