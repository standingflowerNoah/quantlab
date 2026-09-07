"""股东户数（东财 datacenter RPT_HOLDERNUM_DET，第四批 B1 数据源）
=====================================
筹码集中度代理：股东户数下降 = 户均持股上升 = 筹码向主力集中（吸筹）。

数据事实（2026-09-07 探针实测）：
- 全市场每期 ~5300 只，全历史 43.9 万行，最早 2013-01（平安银行 2013-03 起 68 期）
- 披露频率不规则：季报节点为主 + 部分月度/不定期披露 → 稀疏事件表
- HOLD_NOTICE_DATE = 披露日（PIT 记账键），END_DATE = 户数截止日
- 同 (code, end_date) 可能因重述多行 → 因子侧按 notice_date 生效取最新 end_date

存储：DuckDB 表 holder_num（数据量小，与 finance_history 同模式）；
主键 (code, end_date, notice_date)，upsert 幂等。
"""
from __future__ import annotations

import time

import pandas as pd

from ..config import get_logger
from .store import Store
from .sources.eastmoney_source import em_get

log = get_logger(__name__)

URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

HN_DDL = """
    code VARCHAR,
    end_date DATE,
    notice_date DATE,
    holder_num BIGINT,
    PRIMARY KEY (code, end_date, notice_date)
"""

# 仅保留因子需要的列（原始响应 25 列，其余为冗余标注）
_KEEP = {"SECURITY_CODE": "code", "END_DATE": "end_date",
         "HOLD_NOTICE_DATE": "notice_date", "HOLDER_NUM": "holder_num"}


def _fetch_all(max_pages: int = 2000) -> list[dict]:
    """全量翻页拉取（RPT_HOLDERNUM_DET 无 filter，按 END_DATE 倒序）"""
    rows: list[dict] = []
    page = 1
    while page <= max_pages:
        for attempt in range(3):
            try:
                r = em_get(URL, params={
                    "reportName": "RPT_HOLDERNUM_DET", "columns": "ALL",
                    "pageSize": "500", "pageNumber": str(page),
                    "sortColumns": "END_DATE,SECURITY_CODE", "sortTypes": "0,1",
                })
                data = (r.json().get("result") or {})
                chunk = data.get("data") or []
                break
            except Exception as e:
                log.warning(f"holder_num p{page} 第{attempt+1}次拉取失败: {e}")
                time.sleep(2 * (attempt + 1))
                chunk = None
        if chunk is None:
            raise RuntimeError(f"holder_num p{page} 连续失败，中止")
        if not chunk:
            break
        rows.extend(chunk)
        if page % 100 == 0:
            print(f"  holder_num p{page}: 累计 {len(rows)} 行", flush=True)
        if len(chunk) < 500 or len(rows) >= int(data.get("count") or 0):
            break
        page += 1
        time.sleep(0.25)
    return rows


def _to_frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame([{k: r.get(src) for src, k in _KEEP.items()}
                       for r in rows])
    for col in ("end_date", "notice_date"):
        df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
    df["holder_num"] = pd.to_numeric(df["holder_num"], errors="coerce")
    df = df.dropna(subset=["code", "end_date", "holder_num"])
    df["notice_date"] = df["notice_date"].fillna(df["end_date"])
    df = df.drop_duplicates(subset=["code", "end_date", "notice_date"])
    return df


def init_holder_num() -> int:
    """全量拉取股东户数并写入 holder_num（标准 DELETE+INSERT upsert 幂等）"""
    store = Store()
    store.ensure_table("holder_num", HN_DDL)
    rows = _fetch_all()
    if not rows:
        log.warning("holder_num 拉取 0 行，不写入")
        return 0
    df = _to_frame(rows)
    n = len(df)
    for i in range(0, n, 20000):
        store.upsert(df.iloc[i:i + 20000], "holder_num",
                     keys=["code", "end_date", "notice_date"])
    log.info(f"holder_num 写入 {n} 行")
    return n


def update_holder_num(lookback_days: int = 10) -> int:
    """增量：按披露日回看拉取（重述/补披的行 DELETE+INSERT 幂等覆盖）"""
    store = Store()
    store.ensure_table("holder_num", HN_DDL)
    store.register_dataset("holder_num", "clean", "eastmoney",
                           "irregular",
                           "股东户数全历史（RPT_HOLDERNUM_DET，披露日 PIT）")
    row = store.q("SELECT MAX(notice_date) AS d FROM holder_num")
    last = row["d"].iloc[0] if not row.empty else None
    if last is None:
        n = init_holder_num()
        store.set_watermark("holder_num", pd.Timestamp.now())
        return n
    since = (pd.Timestamp(last) - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    rows: list[dict] = []
    page = 1
    while True:
        r = em_get(URL, params={
            "reportName": "RPT_HOLDERNUM_DET", "columns": "ALL",
            "filter": f"(HOLD_NOTICE_DATE>='{since}')",
            "pageSize": "500", "pageNumber": str(page),
            "sortColumns": "SECURITY_CODE", "sortTypes": "1",
        })
        data = (r.json().get("result") or {})
        chunk = data.get("data") or []
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < 500 or len(rows) >= int(data.get("count") or 0):
            break
        page += 1
        time.sleep(0.25)
    if not rows:
        log.info(f"holder_num 增量 0 行（since={since}）")
        store.set_watermark("holder_num", pd.Timestamp.now())
        return 0
    df = _to_frame(rows)
    store.upsert(df, "holder_num",
                 keys=["code", "end_date", "notice_date"])
    store.set_watermark("holder_num", pd.Timestamp.now())
    log.info(f"holder_num 增量写入 {len(df)} 行（since={since}）")
    return len(df)
