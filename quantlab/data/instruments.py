"""股票主表 instruments + 每日估值快照 daily_snapshot
=====================================
- instruments：静态/慢变属性（代码/名称/板块/ST/上市日期/行业/股本）
- daily_snapshot：每日估值截面（价格/市值/PE/PB/换手），腾讯批量行情，60只/次
"""
from __future__ import annotations

import time

import pandas as pd

from ..config import get_logger
from .store import Store
from .sources.tdx_source import TdxClient, is_a_share
from .sources import sina_source as sina
from .sources import tencent_source as tx

log = get_logger(__name__)


def _board_of(code: str) -> str:
    if code.startswith(("688", "689")):
        return "STAR"       # 科创板
    if code.startswith(("300", "301")):
        return "GEM"        # 创业板
    if code.startswith(("43", "83", "87", "88", "92")):
        return "BJ"         # 北交所
    return "MAIN"           # 主板


def _market_of(code: str) -> str:
    if code.startswith(("6", "9")):
        return "SH"
    if code.startswith(("43", "83", "87", "88", "92")):
        return "BJ"
    return "SZ"


def refresh_instruments() -> int:
    """刷新股票主表（新浪全A列表：5552只含北交所；行业/上市日期由财务快照回填）"""
    df = sina.all_a_shares()
    if df.empty:
        raise RuntimeError("新浪全A列表为空")
    df = df[df["code"].map(is_a_share)].copy()
    df["name"] = df["name"].astype(str)
    df["is_st"] = df["name"].str.contains("ST|退", regex=True)
    df["market"] = df["code"].map(_market_of)
    df["board"] = df["code"].map(_board_of)
    df["industry"] = None          # 由财务快照回填
    df["list_date"] = None
    df["total_shares"] = None
    df["float_shares"] = None
    df["updated_at"] = pd.Timestamp.now()
    out = df[["code", "name", "market", "board", "is_st", "industry",
              "list_date", "total_shares", "float_shares", "updated_at"]]
    store = Store()
    store.ensure_table(
        "instruments", """
        code VARCHAR PRIMARY KEY, name VARCHAR, market VARCHAR, board VARCHAR,
        is_st BOOLEAN, industry VARCHAR, list_date DATE,
        total_shares DOUBLE, float_shares DOUBLE, updated_at TIMESTAMP""")
    store.replace_table("instruments", out, """
        code VARCHAR, name VARCHAR, market VARCHAR, board VARCHAR,
        is_st BOOLEAN, industry VARCHAR, list_date DATE,
        total_shares DOUBLE, float_shares DOUBLE, updated_at TIMESTAMP""")
    store.register_dataset("instruments", "clean", "sina", "daily",
                           "A 股股票主表（静态属性）")
    # 顺带把新浪快照的估值字段写入当日 daily_snapshot
    snap = df[["code", "name", "price", "pe_ttm", "pb", "mcap_yi",
               "float_mcap_yi", "turnover_pct"]].copy()
    from . import calendar
    try:
        trade_date = calendar.last_trading_day()
        snap.insert(0, "date", pd.to_datetime(trade_date).normalize())
        store.ensure_table("daily_snapshot", """
            date DATE, code VARCHAR, name VARCHAR, price DOUBLE, last_close DOUBLE,
            change_pct DOUBLE, turnover_pct DOUBLE, pe_ttm DOUBLE, pb DOUBLE,
            mcap_yi DOUBLE, float_mcap_yi DOUBLE, vol_ratio DOUBLE,
            PRIMARY KEY(date, code)""")
        snap = snap.rename(columns={"turnover_pct": "turnover_pct"})
        snap["last_close"] = None
        snap["change_pct"] = None
        snap["vol_ratio"] = None
        snap = snap[["date", "code", "name", "price", "last_close",
                     "change_pct", "turnover_pct", "pe_ttm", "pb",
                     "mcap_yi", "float_mcap_yi", "vol_ratio"]]
        store.upsert(snap, "daily_snapshot", ["date", "code"])
        store.register_dataset("daily_snapshot", "clean", "sina", "daily",
                               "每日估值快照（价格/市值/PE/PB）")
        store.set_watermark("daily_snapshot", trade_date)
    except Exception as e:
        log.warning(f"daily_snapshot 写入跳过: {e}")
    log.info(f"股票主表刷新: {len(out)} 只 A 股")
    return len(out)


def all_codes(st_only: bool = True) -> list[str]:
    """当前全部 A 股代码（st_only=False 含 ST/退市标记股）"""
    store = Store()
    sql = "SELECT code FROM instruments"
    if st_only:
        sql += " WHERE NOT is_st"
    df = store.q(sql)
    return sorted(df["code"].tolist())


# ── 行业回填（东财三级行业 + 证监会行业，批量报表） ─────────────────
def backfill_industry() -> int:
    """从东财 RPT_F10_BASIC_ORGINFO 批量拉取行业分类回填 instruments

    BOARD_NAME_LEVEL = "一级-二级-三级"（东财/申万体系）
    INDUSTRYCSRC1 = 证监会行业
    """
    from .sources.eastmoney_source import datacenter
    rows = datacenter(
        "RPT_F10_BASIC_ORGINFO",
        columns="SECUCODE,SECURITY_CODE,BOARD_NAME_LEVEL,INDUSTRYCSRC1",
        page_size=500, sort_columns="SECURITY_CODE", sort_types="1",
        max_pages=60)
    if not rows:
        raise RuntimeError("东财行业报表为空")
    recs = []
    for r in rows:
        code = r.get("SECURITY_CODE", "")
        if not code or not is_a_share(code):
            continue
        lv = (r.get("BOARD_NAME_LEVEL") or "").split("-")
        recs.append({
            "code": code,
            "industry": lv[0] if lv and lv[0] else None,
            "industry_l2": lv[1] if len(lv) > 1 else None,
            "industry_l3": lv[2] if len(lv) > 2 else None,
            "industry_csrc": r.get("INDUSTRYCSRC1") or None,
        })
    df = pd.DataFrame(recs).drop_duplicates(subset=["code"])
    if df.empty:
        return 0
    store = Store()
    # 扩展列（幂等）
    for col in ("industry_l2", "industry_l3", "industry_csrc"):
        try:
            store.con.execute(
                f"ALTER TABLE instruments ADD COLUMN {col} VARCHAR")
        except Exception:
            pass          # 已存在
    store.con.register("_ind", df)
    store.con.execute("""
        UPDATE instruments SET
            industry = (SELECT _ind.industry FROM _ind WHERE _ind.code = instruments.code),
            industry_l2 = (SELECT _ind.industry_l2 FROM _ind WHERE _ind.code = instruments.code),
            industry_l3 = (SELECT _ind.industry_l3 FROM _ind WHERE _ind.code = instruments.code),
            industry_csrc = (SELECT _ind.industry_csrc FROM _ind WHERE _ind.code = instruments.code)
        WHERE code IN (SELECT code FROM _ind)""")
    store.con.unregister("_ind")
    n = int(store.q("""
        SELECT COUNT(*) FROM instruments
        WHERE industry IS NOT NULL AND industry_l2 IS NOT NULL""").iloc[0, 0])
    log.info(f"行业回填完成: 报表 {len(rows)} 行 → A股 {len(df)} 只, "
             f"instruments 已有行业 {n} 只")
    return n


def update_daily_snapshot(trade_date=None) -> int:
    """每日估值快照：腾讯批量行情全市场（价格/市值/PE/PB）"""
    from . import calendar
    if trade_date is None:
        trade_date = calendar.last_trading_day()
    codes = all_codes(st_only=False)
    total = 0
    store = Store()
    store.ensure_table("daily_snapshot", """
        date DATE, code VARCHAR, name VARCHAR, price DOUBLE, last_close DOUBLE,
        change_pct DOUBLE, turnover_pct DOUBLE, pe_ttm DOUBLE, pb DOUBLE,
        mcap_yi DOUBLE, float_mcap_yi DOUBLE, vol_ratio DOUBLE,
        PRIMARY KEY(date, code)""")
    for i in range(0, len(codes), 500):
        batch = codes[i:i + 500]
        snap = tx.batch_quotes(batch)
        if snap.empty:
            continue
        snap["date"] = pd.to_datetime(trade_date).normalize()
        cols = ["date", "code", "name", "price", "last_close", "change_pct",
                "turnover_pct", "pe_ttm", "pb", "mcap_yi", "float_mcap_yi",
                "vol_ratio"]
        snap = snap[cols]
        total += store.upsert(snap, "daily_snapshot", ["date", "code"])
        time.sleep(0.3)
    store.register_dataset("daily_snapshot", "clean", "tencent", "daily",
                           "每日估值快照（价格/市值/PE/PB）")
    store.set_watermark("daily_snapshot", trade_date)
    log.info(f"每日快照完成: {total} 行 @ {trade_date}")
    return total
