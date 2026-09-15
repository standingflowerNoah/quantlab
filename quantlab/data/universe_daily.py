"""逐日股票池快照 universe_daily（残留 as-of 的根治路径，2026-09-12 立项）

**问题**
`universe.py::get_universe()` 用 `instruments.is_st/list_date/board` +
`daily_snapshot` 最新市值建池，全部是**当前状态**。用它做历史回测等于
「今天的 ST 名单 / 今天的市值排名」套在历史上：

- `ashare_ex` 剔 ST 用今天的名单 → 当时处于 ST 状态的股票被误纳入（或反之），
  已退市股被整体剔除 → **幸存者偏差**；
- `top500/top2000/top3000` 按最新 `float_mcap_yi` 排序 → 市值排名错位。

**方案（三层）**
1. **前向积累**（本模块 `record()`）：每个交易日把当日各池成员快照落库。
   这是唯一严格 PIT 的来源，从上线日起逐日变厚。
2. **历史重建**（`reconstruct_ashare_ex()`）：`valuation_daily.is_st` 经验证
   为**逐日真值**（489 只代码状态随时间变化、497 只曾 ST、163,957 行 ST），
   配合 `instruments.list_date/board`（静态事实）可对 2021-01-04 起的
   `ashare_ex` 做 PIT 重建。重建行标 `source='recon'`。
3. **缺口透明化**：重建覆盖不到的区间（2021 以前）如实缺省，不伪造。

**表**：`universe_daily(universe_name, date, code, source)`，
PRIMARY KEY(universe_name, date, code)

**用法**
    from .universe_daily import record, reconstruct_ashare_ex, load
    record()                      # 每日流水线调用（当日各池快照）
    reconstruct_ashare_ex()       # 一次性历史重建（幂等）
    load("ashare_ex", "2024-05-14")   # 取某日池成员（PIT）
"""
from __future__ import annotations

import time
from datetime import timedelta

import pandas as pd

from ..config import get_logger
from .store import Store

log = get_logger(__name__)

TABLE = "universe_daily"
DDL = """
    universe_name VARCHAR, date DATE, code VARCHAR, source VARCHAR,
    PRIMARY KEY(universe_name, date, code)
"""

# 前向积累的标准池集合（与 universe.py 的池名一致）
STANDARD_POOLS = ("all", "ashare_ex", "ashare_main",
                  "top500", "top2000", "top3000")

MIN_LIST_DAYS = 120          # 与 universe.py 保持一致
NEW_STOCK_BUFFER = 20
SOURCE_SNAP = "snap"
SOURCE_RECON = "recon"


def ensure_table(store: Store) -> None:
    store.ensure_table(TABLE, DDL)


def record(date=None, pools: tuple[str, ...] = STANDARD_POOLS) -> int:
    """记录当日各池成员快照（每日流水线调用；同日重跑幂等覆盖）

    严格 PIT：当天记录的就是当天可知的池，事后不回改。
    """
    from .universe import get_universe
    store = Store()
    ensure_table(store)
    day = pd.Timestamp(date).normalize() if date is not None else pd.Timestamp.now().normalize()
    rows = []
    for name in pools:
        try:
            codes = get_universe(name)
        except Exception as e:                               # noqa: BLE001
            log.warning(f"[universe_daily] {name} 取池失败: {e}")
            continue
        if not codes:
            continue
        rows.append(pd.DataFrame({
            "universe_name": name,
            "date": day.date(),
            "code": sorted(codes),
            "source": SOURCE_SNAP,
        }))
    if not rows:
        log.warning("[universe_daily] 无池可记录")
        return 0
    df = pd.concat(rows, ignore_index=True)
    n = store.upsert(df, TABLE, ["universe_name", "date", "code"])
    store.set_watermark(TABLE, day.date())
    log.info(f"[universe_daily] 记录 {day.date()} 快照 "
             f"{len(pools)} 池 / {n} 行")
    return n


def reconstruct_ashare_ex(start: str = "2021-01-04",
                          end: str | None = None,
                          batch_days: int = 60) -> int:
    """PIT 重建 ashare_ex 历史池（2021 起，幂等）

    口径（与 universe.py::_drop_st_new 对齐，改为逐日真值）：
      - 非 ST：`valuation_daily.is_st = false`（逐日真值，已验证）
      - 非北交所：`instruments.board != 'BJ'`（静态事实）
      - 非次新：`list_date <= d - (MIN_LIST_DAYS + NEW_STOCK_BUFFER) 天`
    """
    store = Store()
    ensure_table(store)
    con = store.con

    end_d = pd.Timestamp(end) if end else pd.Timestamp.now().normalize()
    start_d = pd.Timestamp(start)
    if end_d < start_d:
        return 0

    # 交易日序列取自 share_capital_daily（逐日真值表，天然对齐行情日历）
    dates = [r[0] for r in con.execute(
        f"SELECT DISTINCT date FROM share_capital_daily "
        f"WHERE date >= DATE '{start_d.date()}' AND date <= DATE '{end_d.date()}' "
        f"ORDER BY date").fetchall()]
    if not dates:
        log.warning("[universe_daily] 重建区间无交易日")
        return 0

    # 静态事实：板块与上市日（当前态即可——board/list_date 不随时间变化）
    static = con.execute(
        "SELECT code, board, list_date FROM instruments "
        "WHERE board IS NOT NULL").df()
    static["code"] = static["code"].astype(str)
    # 北交所在 instruments 里可能标 BJ 也可能没有，双保险按代码段排除
    bj_mask = static["board"].eq("BJ") | static["code"].str.match(
        r"^(83|87|43|92)")
    ok = static[~bj_mask][["code", "list_date"]].copy()
    con.register("ok_codes", ok)
    log.info(f"[universe_daily] 重建候选（非北交所）{len(ok)} 只")

    n_total = 0
    t0 = time.time()
    for i in range(0, len(dates), batch_days):
        chunk = dates[i:i + batch_days]
        d0, d1 = chunk[0], chunk[-1]
        buf = []
        for d in chunk:
            cutoff = (pd.Timestamp(d) - timedelta(
                days=MIN_LIST_DAYS + NEW_STOCK_BUFFER)).date()
            df = con.execute(f"""
                SELECT DATE '{d}' AS date, v.code
                FROM read_parquet('data/lake/clean/fundamental/valuation_daily/part-*.parquet') v
                WHERE v.date = DATE '{d}' AND NOT v.is_st
                  AND v.code IN (SELECT code FROM ok_codes)
                  AND COALESCE((SELECT list_date FROM ok_codes o
                                WHERE o.code = v.code),
                               DATE '1900-01-01') <= DATE '{cutoff}'
            """).df()
            if df.empty:
                continue
            buf.append(pd.DataFrame({
                "universe_name": "ashare_ex",
                "date": d,
                "code": df["code"].astype(str),
                "source": SOURCE_RECON,
            }))
        if buf:
            out = pd.concat(buf, ignore_index=True)
            n_total += store.upsert(out, TABLE,
                                    ["universe_name", "date", "code"])
        log.info(f"[universe_daily] 重建 {d0}~{d1} 累计 {n_total} 行 "
                 f"({time.time()-t0:.0f}s)")
    log.info(f"[universe_daily] ashare_ex 历史重建完成：{n_total} 行")
    return n_total


def load(name: str = "ashare_ex", date=None) -> list[str]:
    """取某日池成员（严格 PIT：只读快照表，不回退当前态）"""
    store = Store()
    ensure_table(store)
    if date is None:
        row = store.q(f"SELECT max(date) AS d FROM {TABLE} "
                      f"WHERE universe_name=?", [name])
        if row.empty or row.iloc[0, 0] is None:
            return []
        date = row.iloc[0, 0]
    df = store.q(f"SELECT code FROM {TABLE} WHERE universe_name=? AND date=?",
                 [name, pd.Timestamp(date).date()])
    return sorted(df["code"].astype(str).tolist()) if len(df) else []


def coverage() -> pd.DataFrame:
    """各池覆盖概况（质量看板用）"""
    store = Store()
    ensure_table(store)
    return store.q(f"""
        SELECT universe_name, count(DISTINCT date) AS n_days,
               min(date) AS first_date, max(date) AS last_date,
               count(*) AS n_rows, source
        FROM {TABLE} GROUP BY universe_name, source ORDER BY universe_name, source
    """)
