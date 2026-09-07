"""多期财务历史（东财 datacenter 按报告期业绩/资产负债/现金流报表）
=====================================
修复因子审查诊断出的根本数据限制：通达信 finance_info 仅提供最新报告期快照，
导致成长因子无数据、质量/估值因子无历史。本模块从东财拉取 2021Q1 起全部
报告期（22 期）的三张报表，构成 finance_history 表（code × report_period 主键）：

- 业绩报表 RPT_LICO_FN_CPD：营收/归母净利/自带同比（YSTZ/SJLTZ），含退市股
  （每期 ~11000+ 只，顺带修复纯通达信快照的幸存者偏差）
- 资产负债表 RPT_DMSK_FN_BALANCE：总资产/总负债/净资产
- 现金流量表 RPT_DMSK_FN_CASHFLOW：经营现金流净额
- 每期带 NOTICE_DATE（披露日）——因子记账的 point-in-time 键（无披露前视）

因子侧用法见 factor/library/growth.py（ASOF JOIN 前向填充）。
"""
from __future__ import annotations

import time

import pandas as pd

from ..config import get_logger
from .store import Store
from .sources.eastmoney_source import em_get

log = get_logger(__name__)

FH_DDL = """
code VARCHAR, report_period DATE, notice_date DATE,
revenue DOUBLE, revenue_yoy DOUBLE, net_profit DOUBLE, net_profit_yoy DOUBLE,
total_assets DOUBLE, total_liab DOUBLE, total_equity DOUBLE,
current_assets DOUBLE, current_liab DOUBLE, operate_cf DOUBLE,
current_ratio DOUBLE, inventory DOUBLE,
notice_eff DATE, fetched_at TIMESTAMP,
PRIMARY KEY(code, report_period)
"""

# 法定披露截止（用于修正 NOTICE_DATE 的"修订公告日"语义错位：东财该字段
# 为最近一次公告日，老报告期普遍滞后 1 年+。超过法定截止的回退到截止日——
# 截止日全市场必然已披露，无前视且时效损失最小）
_NOTICE_EFF_SQL = """
    LEAST(notice_date,
        CASE month(report_period)
          WHEN 3 THEN report_period + INTERVAL 30 DAY    -- 一季报 4-30
          WHEN 6 THEN report_period + INTERVAL 62 DAY    -- 半年报 8-31
          WHEN 9 THEN report_period + INTERVAL 31 DAY    -- 三季报 10-31
          ELSE report_period + INTERVAL 121 DAY          -- 年报 次年4-30
        END)::DATE
"""

URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORTS = [
    ("RPT_LICO_FN_CPD", "REPORTDATE"),
    ("RPT_DMSK_FN_BALANCE", "REPORT_DATE"),
    ("RPT_DMSK_FN_CASHFLOW", "REPORT_DATE"),
]


def _periods(start: str = "2021Q1", end: str | None = None) -> list[str]:
    """季度末日期列表（'2021-03-31' ... 最新已披露期）"""
    end = end or pd.Timestamp.now().to_period("Q").strftime("%YQ%q")
    pr = pd.period_range(start, end, freq="Q")
    return [p.end_time.strftime("%Y-%m-%d") for p in pr]


def _fetch_report(rep: str, date_col: str, period: str,
                  max_pages: int = 30) -> list[dict]:
    """拉取某报表某报告期全市场数据（翻页）"""
    rows: list[dict] = []
    page = 1
    while page <= max_pages:
        try:
            r = em_get(URL, params={
                "reportName": rep, "columns": "ALL",
                "filter": f"({date_col}='{period}')",
                "pageSize": "500", "pageNumber": str(page),
                "sortColumns": "SECURITY_CODE", "sortTypes": "1",
            })
            data = (r.json().get("result") or {})
            chunk = data.get("data") or []
        except Exception as e:
            log.warning(f"{rep} {period} p{page} 拉取失败: {e}")
            time.sleep(2)
            continue
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < 500 or len(rows) >= int(data.get("count") or 0):
            break
        page += 1
        time.sleep(0.25)               # 限流礼让
    return rows


def _f(v) -> float | None:
    try:
        x = float(v)
        return x if pd.notna(x) else None
    except (TypeError, ValueError):
        return None


def _d(v):
    if v is None:
        return None
    return str(v)[:10]


def init_finance_history(periods: list[str] | None = None,
                         force: bool = False) -> int:
    """拉取多期财务历史并写入 finance_history（upsert 幂等）"""
    store = Store()
    store.ensure_table("finance_history", FH_DDL)
    # 既有表补列（ensure_table 不做 schema 迁移）
    for col in ("current_ratio", "inventory"):
        store.con.execute(
            f"ALTER TABLE finance_history ADD COLUMN IF NOT EXISTS {col} DOUBLE")
    store.register_dataset("finance_history", "clean", "eastmoney", "weekly",
                           "多期财务历史（东财业绩/资产负债/现金流报表）")
    periods = periods or _periods()
    all_rows: dict[tuple, dict] = {}
    t0 = time.time()
    for i, period in enumerate(periods, 1):
        # 1) 业绩报表：营收/净利/同比/披露日
        income = _fetch_report("RPT_LICO_FN_CPD", "REPORTDATE", period)
        for r in income:
            code = r.get("SECURITY_CODE")
            if not code:
                continue
            key = (code, period)
            rec = all_rows.setdefault(key, {"code": code, "report_period": period})
            rec["notice_date"] = _d(r.get("NOTICE_DATE")) or rec.get("notice_date")
            rec["revenue"] = _f(r.get("TOTAL_OPERATE_INCOME"))
            rec["revenue_yoy"] = _f(r.get("YSTZ"))
            rec["net_profit"] = _f(r.get("PARENT_NETPROFIT"))
            rec["net_profit_yoy"] = _f(r.get("SJLTZ"))
        # 2) 资产负债表：总资产/负债/净资产
        for r in _fetch_report("RPT_DMSK_FN_BALANCE", "REPORT_DATE", period):
            code = r.get("SECURITY_CODE")
            if not code:
                continue
            rec = all_rows.setdefault((code, period),
                                      {"code": code, "report_period": period})
            rec["notice_date"] = _d(r.get("NOTICE_DATE")) or rec.get("notice_date")
            rec["total_assets"] = _f(r.get("TOTAL_ASSETS"))
            rec["total_liab"] = _f(r.get("TOTAL_LIABILITIES"))
            rec["total_equity"] = _f(r.get("TOTAL_EQUITY"))
            # 流动比率（东财直接给出，百分数口径 ÷100 存比率）与存货绝对值。
            # 2026-09-07 修复：旧代码读 TOTAL_CURRENT_ASSETS/TOTAL_CURRENT_LIABILITIES
            # ——该报表无此字段（57 字段实测），导致 current_assets/current_liab 全空。
            # 金融股（银行等）无流动资产概念，CURRENT_RATIO=None 属预期。
            cr = _f(r.get("CURRENT_RATIO"))
            rec["current_ratio"] = cr / 100.0 if cr is not None else None
            rec["inventory"] = _f(r.get("INVENTORY"))
        # 3) 现金流量表：经营现金流
        for r in _fetch_report("RPT_DMSK_FN_CASHFLOW", "REPORT_DATE", period):
            code = r.get("SECURITY_CODE")
            if not code:
                continue
            rec = all_rows.setdefault((code, period),
                                      {"code": code, "report_period": period})
            rec["notice_date"] = _d(r.get("NOTICE_DATE")) or rec.get("notice_date")
            rec["operate_cf"] = _f(r.get("NETCASH_OPERATE"))
        log.info(f"finance_history {period}: 累计 {len(all_rows)} 行 "
                 f"({time.time()-t0:.0f}s)")

    if not all_rows:
        log.warning("finance_history 为空")
        return 0

    df = pd.DataFrame(list(all_rows.values()))
    df["report_period"] = pd.to_datetime(df["report_period"])   # 表主键为 DATE
    # 披露日缺失 → 用该期全市场最晚披露日保守化（宁晚勿早，防前视）
    max_notice = pd.to_datetime(df["notice_date"], errors="coerce").max()
    df["notice_date"] = (pd.to_datetime(df["notice_date"], errors="coerce")
                         .fillna(max_notice))
    # notice_eff：超出法定披露截止的修订公告日回退到截止日（见 _NOTICE_EFF_SQL）
    df["fetched_at"] = pd.Timestamp.now()
    df = df.sort_values(["code", "report_period"])
    n = store.upsert(df, "finance_history",
                     ["code", "report_period"])
    store.con.execute(
        f"UPDATE finance_history SET notice_eff = {_NOTICE_EFF_SQL}")
    store.set_watermark("finance_history", pd.Timestamp.now().date())
    log.info(f"finance_history 完成: {len(df)} 行（{len(periods)} 期）→ {n} 行写入")
    return n


def update_finance_history() -> int:
    """周度增量：仅刷新最近 2 个报告期（新期披露 + 上期修正公告）。

    历史期数据基本稳定，且 notice_eff 有法定截止兜底，无需每周全量重拉
    （全量 22 期约 20 分钟，增量 2 期约 2 分钟）。upsert 幂等。
    """
    periods = _periods()
    return init_finance_history(periods=periods[-2:])
