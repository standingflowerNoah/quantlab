"""业绩预告（东财 datacenter RPT_PUBLIC_OP_NEWPREDICT，第五批 SUE_I 数据源）
=====================================
预告事件流：比正式财报早 1-3 个月的盈利预披露，SUE_I 时效改进的核心信号。

数据事实（2026-09-07 探针实测）：
- 2025-12-31 期全市场 7818 行；茅台 2003-2026 全历史 33 条
- PREDICT_FINANCE_CODE='004' = 归母净利润（同股同期可能多科目行，必须过滤）
- NOTICE_DATE = 披露日（PIT 键）；PREDICT_AMT_LOWER/UPPER = 净利预告区间（元）
- ADD_AMP_LOWER/UPPER = 同比增幅区间（%）；PREYEAR_SAME_PERIOD = 去年同期净利
- 同股同期可多次披露（修正预告）→ 全保留，因子侧按 notice_date 生效取最新

存储：DuckDB 表 perf_forecast，键 (code, report_date, notice_date)。
"""
from __future__ import annotations

import time

import pandas as pd

from ..config import get_logger
from .store import Store
from .sources.eastmoney_source import em_get

log = get_logger(__name__)

URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

PF_DDL = """
    code VARCHAR,
    report_date DATE,
    notice_date DATE,
    amt_lower DOUBLE,
    amt_upper DOUBLE,
    add_amp_lower DOUBLE,
    add_amp_upper DOUBLE,
    prev_same_period DOUBLE,
    predict_type VARCHAR,
    PRIMARY KEY (code, report_date, notice_date)
"""

_KEEP = {"SECURITY_CODE": "code", "REPORT_DATE": "report_date",
         "NOTICE_DATE": "notice_date", "PREDICT_AMT_LOWER": "amt_lower",
         "PREDICT_AMT_UPPER": "amt_upper", "ADD_AMP_LOWER": "add_amp_lower",
         "ADD_AMP_UPPER": "add_amp_upper",
         "PREYEAR_SAME_PERIOD": "prev_same_period",
         "PREDICT_TYPE": "predict_type"}


def _fetch_period(period: str, max_pages: int = 60) -> list[dict]:
    """拉取某报告期全市场预告（过滤归母净利科目）"""
    rows: list[dict] = []
    page = 1
    while page <= max_pages:
        for attempt in range(3):
            try:
                r = em_get(URL, params={
                    "reportName": "RPT_PUBLIC_OP_NEWPREDICT", "columns": "ALL",
                    "filter": (f"(REPORT_DATE='{period}')"
                               "(PREDICT_FINANCE_CODE='004')"),
                    "pageSize": "500", "pageNumber": str(page),
                    "sortColumns": "SECURITY_CODE", "sortTypes": "1",
                })
                data = (r.json().get("result") or {})
                chunk = data.get("data") or []
                break
            except Exception as e:
                log.warning(f"perf_forecast {period} p{page} 第{attempt+1}次失败: {e}")
                time.sleep(2 * (attempt + 1))
                chunk = None
        if chunk is None:
            raise RuntimeError(f"perf_forecast {period} p{page} 连续失败")
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < 500 or len(rows) >= int(data.get("count") or 0):
            break
        page += 1
        time.sleep(0.25)
    return rows


def _to_frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame([{k: r.get(src) for src, k in _KEEP.items()}
                       for r in rows])
    for col in ("report_date", "notice_date"):
        df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
    for col in ("amt_lower", "amt_upper", "add_amp_lower", "add_amp_upper",
                "prev_same_period"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["code", "report_date", "notice_date"])
    # 同键多行（同股同期同日的多版本/多段落 004 行）按键保留一条
    df = df.drop_duplicates(subset=["code", "report_date", "notice_date"])
    return df


def init_perf_forecast(periods: list[str] | None = None) -> int:
    """按报告期全量拉取预告并写入 perf_forecast（DELETE+INSERT upsert 幂等）"""
    store = Store()
    store.ensure_table("perf_forecast", PF_DDL)
    store.register_dataset("perf_forecast", "clean", "eastmoney",
                           "quarterly",
                           "业绩预告全历史（RPT_PUBLIC_OP_NEWPREDICT，披露日 PIT）")
    if periods is None:
        # 2020Q1 起（预留 Q_{t-4} 基数与 8 季窗口）
        pr = pd.period_range("2020Q1", pd.Timestamp.now().to_period("Q"),
                             freq="Q")
        periods = [p.end_time.strftime("%Y-%m-%d") for p in pr]
    total = 0
    for i, period in enumerate(periods, 1):
        rows = _fetch_period(period)
        df = _to_frame(rows)
        if df.empty:
            print(f"  [{i}/{len(periods)}] {period}: 0 行", flush=True)
            continue
        store.upsert(df, "perf_forecast",
                     keys=["code", "report_date", "notice_date"])
        total += len(df)
        print(f"  [{i}/{len(periods)}] {period}: {len(df)} 行"
              f"（累计 {total}）", flush=True)
    log.info(f"perf_forecast 全量完成 {total} 行")
    return total


def update_perf_forecast(lookback_quarters: int = 2) -> int:
    """增量：回看最近 N 个季度报告期重拉（修正预告幂等覆盖）"""
    store = Store()
    store.ensure_table("perf_forecast", PF_DDL)
    store.register_dataset("perf_forecast", "clean", "eastmoney",
                           "quarterly",
                           "业绩预告全历史（RPT_PUBLIC_OP_NEWPREDICT，披露日 PIT）")
    pr = pd.period_range(
        (pd.Timestamp.now() - pd.DateOffset(months=3 * lookback_quarters)
         ).to_period("Q"), pd.Timestamp.now().to_period("Q"), freq="Q")
    periods = [p.end_time.strftime("%Y-%m-%d") for p in pr]
    n = init_perf_forecast(periods)
    store.set_watermark("perf_forecast", pd.Timestamp.now())
    return n
