"""PIT 逐日股本表 share_capital_daily
========================================
**解决的问题（as-of 缺口根治）**

`finance_snapshot` 只有「最新报告期」一行/股，其 `total_shares` / `float_shares`
是**当前值回填历史**。任何用股本做分母的历史因子（市值/换手率/BP…）都被
`audit._ASO_TABLES` 记 as-of 缺口（WARN）。截至 2026-09-11 受影响 8 个：
`bp / dragon_net_20 / op_margin / size / sp / total_mcap / turnover / turnover_std_20`。

而 `finance_snapshot` 走 `store.replace_table()`（DROP+CREATE+INSERT），
**不会**随时间积累历史版本——研究报告里「随财务快照积累缓解」的说法不成立。

**数据源**：free-stockdb 日线 21 字段中的 `total_share` / `float_share`。
2026-09-11 验证为**逐日真值**（非回填）：
  - 20/20 抽样股票股本呈阶梯式变化；
  - 茅台 2006 转增→9.438 亿、2014 送股→11.42 亿、**2025-09 回购注销→12.5227 亿**；
  - 建行 2010-01-04 为 2336.89 亿股（当前 2616 亿），平安银行 2015-01-05 为
    114.25 亿股（当前 194 亿）——均与公开历史一致。
  - 对照组：用送转事件反推平安银行 2015 年得 190 亿股，与真值差 40%
    （增发才是股本变动主因，事件反推不可行）。

**表**：`share_capital_daily(code, date, total_shares, float_shares, source)`
    PRIMARY KEY(code, date)

**用法（因子侧）**：必须用 ASOF JOIN 取「<= t 的最近一条」，这才是 PIT 语义
（停牌/无成交日不复权股本前向保持）：:

    SELECT k.code, k.date, k.close * sc.total_shares AS mcap
    FROM kline_daily k
    ASOF JOIN share_capital_daily sc
      ON k.code = sc.code AND k.date >= sc.date

**审计**：本表带 `date` 列，故可进 `audit._PIT_LE`（截断 <= t0 重算比对），
从「制度性豁免的 as-of」升级为「可检验的 PIT 表」。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from ..config import get_logger
from .sources import fsdb_source as fs
from .store import Store

log = get_logger(__name__)

TABLE = "share_capital_daily"
DDL = """
    code VARCHAR, date DATE,
    total_shares DOUBLE, float_shares DOUBLE,
    source VARCHAR,
    PRIMARY KEY(code, date)
"""

START_DATE = "20000101"          # fsdb 日线实际最早 ~2001（茅台 2001-08-27）
END_DATE = "20991231"
WORKERS = fs.MAX_CONCURRENCY     # 4（fsdb 护栏：并发 > 4 会诱发服务塌陷）
WRITE_BATCH = 300                # 每 N 只提交一次（兼顾内存与断点粒度）


def _fetch_one(code: str) -> pd.DataFrame | None:
    """拉单只全历史逐日股本。返回 [code, date, total_shares, float_shares]。"""
    try:
        d = fs.day_bars(code, START_DATE, END_DATE)
    except Exception as e:                                   # noqa: BLE001
        log.debug(f"[share] {code} 拉取失败: {e}")
        return None
    if d is None or d.empty:
        return None
    if "total_share" not in d.columns:
        return None
    out = pd.DataFrame({
        "code": d["code"].astype(str),
        "date": pd.to_datetime(d["date"].astype(str), format="%Y%m%d"),
        "total_shares": pd.to_numeric(d["total_share"], errors="coerce"),
        "float_shares": pd.to_numeric(d["float_share"], errors="coerce"),
    })
    out = out[(out["total_shares"] > 0) & (out["float_shares"] > 0)]
    if out.empty:
        return None
    # 防串位（工程惯例）：返回的 code 必须与请求一致
    out = out[out["code"] == code]
    return out if not out.empty else None


def _missing_codes(store: Store, codes: list[str]) -> list[str]:
    """断点续拉：跳过库里已有数据的 code"""
    have = store.q(f"SELECT DISTINCT code FROM {TABLE}")
    if have.empty:
        return codes
    s = set(have["code"].astype(str))
    return [c for c in codes if c not in s]


def build(codes: list[str] | None = None, workers: int = WORKERS,
          force: bool = False, limit: int | None = None) -> dict:
    """全量回填逐日股本（幂等，可中断续跑）。

    codes=None → fsdb 全部 A 股（含退市）。force=True 时不跳过已有 code。
    """
    store = Store()
    store.ensure_table(TABLE, DDL)
    if codes is None:
        codes = fs.all_codes()
    if limit:
        codes = codes[:limit]
    if not force:
        codes = _missing_codes(store, codes)

    total = len(codes)
    if not total:
        log.info(f"[share] {TABLE} 已是最新，无需回填")
        return {"codes": 0, "rows": 0, "failed": 0, "elapsed": 0.0}

    log.info(f"[share] 回填 {total} 只（workers={workers}）→ {TABLE}")
    fs.ensure_healthy()
    t0 = time.time()
    n_rows = 0
    n_fail = 0
    buf: list[pd.DataFrame] = []
    buf_n = 0
    done = 0

    def flush() -> None:
        nonlocal buf, n_rows, buf_n
        if not buf:
            return
        df = pd.concat(buf, ignore_index=True)
        df["source"] = "fsdb"
        n_rows += store.upsert(df, TABLE, ["code", "date"])
        buf, buf_n = [], 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_one, c): c for c in codes}
        for fut in as_completed(futs):
            done += 1
            try:
                df = fut.result()
            except Exception as e:                           # noqa: BLE001
                log.debug(f"[share] {futs[fut]} 异常: {e}")
                df = None
            if df is None:
                n_fail += 1
            else:
                buf.append(df)
                buf_n += 1
                if buf_n >= WRITE_BATCH:
                    flush()
            if done % 500 == 0:
                el = time.time() - t0
                log.info(f"[share] {done}/{total} 只，{n_rows} 行，"
                         f"{el:.0f}s（{done/el:.1f} 只/s）")
    flush()

    el = time.time() - t0
    store.set_watermark(TABLE, pd.Timestamp.now().date())
    log.info(f"[share] 完成：{total-n_fail}/{total} 只，{n_rows} 行，"
             f"失败 {n_fail}，用时 {el:.0f}s")
    return {"codes": total, "rows": n_rows, "failed": n_fail, "elapsed": el}


def update(target: pd.Timestamp | str | None = None) -> int:
    """每日增量：只补最近若干交易日（当日新数据 + 少量回溯以承接修正）。

    股本变动稀疏，故只拉近 10 个自然日的切片即可，避免每日全历史重拉。
    """
    from datetime import timedelta

    store = Store()
    store.ensure_table(TABLE, DDL)
    day = pd.Timestamp(target) if target is not None else pd.Timestamp.now()
    start = (day - timedelta(days=10)).strftime("%Y%m%d")
    end = day.strftime("%Y%m%d")

    codes = store.q("SELECT DISTINCT code FROM kline_daily").iloc[:, 0].astype(str).tolist()
    log.info(f"[share] 增量 {start}~{end}，{len(codes)} 只")
    rows = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fs.day_bars, c, start, end): c for c in codes}
        for fut in as_completed(futs):
            try:
                d = fut.result()
            except Exception:                                # noqa: BLE001
                continue
            if d is None or d.empty or "total_share" not in d.columns:
                continue
            d = d[d["code"].astype(str) == futs[fut]]
            if d.empty:
                continue
            rows.append(pd.DataFrame({
                "code": d["code"].astype(str),
                "date": pd.to_datetime(d["date"].astype(str), format="%Y%m%d"),
                "total_shares": pd.to_numeric(d["total_share"], errors="coerce"),
                "float_shares": pd.to_numeric(d["float_share"], errors="coerce"),
            }))
    if not rows:
        log.warning("[share] 增量无数据")
        return 0
    df = pd.concat(rows, ignore_index=True)
    df = df[(df["total_shares"] > 0) & (df["float_shares"] > 0)]
    df["source"] = "fsdb"
    n = store.upsert(df, TABLE, ["code", "date"])
    store.set_watermark(TABLE, day.date())
    log.info(f"[share] 增量写入 {n} 行")
    return n


def shares_asof(asof: pd.Timestamp | str | None = None) -> pd.DataFrame:
    """取 asof 时点的股本截面（每股取 <= asof 的最近一条）"""
    where = ""
    if asof is not None:
        where = f"WHERE date <= DATE '{pd.Timestamp(asof).date()}'"
    sql = f"""
        SELECT code, total_shares, float_shares FROM (
            SELECT code, total_shares, float_shares,
                   row_number() OVER (PARTITION BY code ORDER BY date DESC) rn
            FROM {TABLE} {where}
        ) WHERE rn = 1
    """
    return Store().q(sql)


def coverage() -> dict:
    """覆盖概况（用于质量看板 / 审计）"""
    return Store().q(f"""
        SELECT count(*) AS n_rows, count(DISTINCT code) AS n_codes,
               min(date) AS first_date, max(date) AS last_date
        FROM {TABLE}
    """).iloc[0].to_dict()
