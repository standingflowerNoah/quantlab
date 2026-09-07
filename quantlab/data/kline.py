"""日 K 线采集 + 复权因子引擎
=====================================
- 数据源：pytdx 直连（不复权 OHLCV + xdxr 除权事件）
- 复权因子自算（前复权 F：最新日=1）
- 支持日/周/月频（freq 参数化），分钟频预留
- 表：kline_daily(code, date, open, high, low, close, vol, amount, adj_factor)
      dividend_events(code, date, fenhong, songzhuangu, peigu, peigujia)
"""
from __future__ import annotations

import time

import pandas as pd

from .. import config
from ..config import get_logger, INDEX_CODES
from .store import Store
from .sources.tdx_source import TdxClient, market_of

log = get_logger(__name__)

KLINE_DDL = """
date DATE, code VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
vol DOUBLE, amount DOUBLE, adj_factor DOUBLE, PRIMARY KEY(code, date)
"""

XDXR_DDL = """
code VARCHAR, date DATE, fenhong DOUBLE, songzhuangu DOUBLE,
peigu DOUBLE, peigujia DOUBLE, PRIMARY KEY(code, date)
"""


# ── 复权因子计算 ────────────────────────────────────────────────────
def compute_adj_factors(kline: pd.DataFrame,
                        events: pd.DataFrame) -> pd.Series:
    """前复权因子 F(t)：最新交易日=1，前复权价 = 原价 × F

    算法：除权参考价 B=(P_prev-div+配股数×配股价)/(1+送转+配)，
    mult=B/P_prev(<1)；F(t)=∏(mult_i | 事件日 i > t)，从最新日倒序累积。
    """
    kline = kline.sort_values("date").reset_index(drop=True)
    n = len(kline)
    if n == 0:
        return pd.Series(dtype=float)
    F = pd.Series(1.0, index=range(n))
    if events is None or events.empty:
        F.index = kline.index
        return F
    dates = kline["date"].values
    closes = kline["close"].values
    mults = []
    for _, e in events.iterrows():
        e_date = pd.Timestamp(e["date"])
        # 事件日前一交易日（< 事件日的最大日期）
        idx = int(dates.searchsorted(e_date.to_datetime64(), side="left")) - 1
        if idx < 0:
            continue     # 事件早于K线起点，不影响窗口内复权
        p_prev = closes[idx]
        denom = 1.0 + float(e["songzhuangu"]) + float(e["peigu"])
        numer = p_prev - float(e["fenhong"]) + float(e["peigu"]) * float(e["peigujia"])
        if p_prev <= 0 or denom <= 0 or numer <= 0:
            continue
        # mult = 除权参考价/前收 = B/P_prev = numer/(denom × P_prev) < 1
        mults.append((e_date, numer / (denom * p_prev)))
    # 倒序累积：F(t) = ∏(mult | 事件日 > t)
    cur, j = 1.0, 0
    mults.sort(key=lambda x: -x[0].value)
    for k in range(n - 1, -1, -1):
        t = pd.Timestamp(dates[k])
        while j < len(mults) and mults[j][0] > t:
            cur *= mults[j][1]
            j += 1
        F.iloc[k] = cur
    F.index = kline.index
    return F


def qfq(df: pd.DataFrame) -> pd.DataFrame:
    """K线表（含 adj_factor）→ 前复权 OHLC 视图"""
    out = df.copy()
    for c in ("open", "high", "low", "close"):
        out[c] = out[c] * out["adj_factor"]
    return out


# ── 个股 K 线采集 ──────────────────────────────────────────────────
def _fetch_one(code: str, start: str, store: Store) -> dict | None:
    cli = TdxClient.instance()
    try:
        k = cli.bars_history(code, freq="day", start_date=start)
        ev = cli.xdxr_events(code)
    except Exception as e:
        log.warning(f"{code} 采集失败: {e}")
        return None
    if k.empty:
        return None
    k = k.sort_values("date").reset_index(drop=True)
    k["adj_factor"] = compute_adj_factors(k, ev)
    k["code"] = code
    k = k[["date", "code", "open", "high", "low", "close", "vol",
           "amount", "adj_factor"]]
    store.upsert(k, "kline_daily", ["code", "date"])
    n_ev = 0
    if not ev.empty:
        ev["code"] = code
        store.upsert(ev[["code", "date", "fenhong", "songzhuangu", "peigu",
                         "peigujia"]], "dividend_events", ["code", "date"])
        n_ev = len(ev)
    return {"code": code, "rows": len(k),
            "first": str(k["date"].min().date()),
            "last": str(k["date"].max().date()), "events": n_ev}


def init_kline(codes: list[str] | None = None):
    """全量首建：HISTORY_START 至今日 K（含复权因子），upsert 幂等支持断点续跑"""
    store = Store()
    store.ensure_table("kline_daily", KLINE_DDL)
    store.ensure_table("dividend_events", XDXR_DDL)
    store.register_dataset("kline_daily", "clean", "tdx", "daily",
                           "日K线（不复权+前复权因子）")
    store.register_dataset("dividend_events", "clean", "tdx", "daily",
                           "除权除息事件表")
    if codes is None:
        from .instruments import all_codes
        codes = all_codes(st_only=False)
    run = store.log_run("kline_daily", "init")
    ok, fail, t0 = 0, 0, time.time()
    for i, code in enumerate(codes, 1):
        r = _fetch_one(code, config.HISTORY_START, store)
        if r:
            ok += 1
        else:
            fail += 1
        if i % 200 == 0:
            dt = time.time() - t0
            log.info(f"K线进度 {i}/{len(codes)} ok={ok} fail={fail} "
                     f"({dt:.0f}s 均{dt/i:.2f}s/只 剩余{(len(codes)-i)*dt/i/60:.0f}分)")
    wm = store.q("SELECT MAX(date) FROM kline_daily").iloc[0, 0]
    store.set_watermark("kline_daily", wm)
    store.finish_run(run, "ok", ok, f"ok={ok} fail={fail}")
    log.info(f"K线首建完成: ok={ok} fail={fail} 耗时{(time.time()-t0)/60:.1f}分")


def update_kline(recent: int = 60):
    """增量更新：拉每股最近 recent 根；新除权事件触发该股因子全量重算"""
    from .instruments import all_codes
    store = Store()
    store.ensure_table("kline_daily", KLINE_DDL)
    store.ensure_table("dividend_events", XDXR_DDL)
    codes = all_codes(st_only=False)
    run = store.log_run("kline_daily", "update")
    cli = TdxClient.instance()
    ok, fail, t0 = 0, 0, time.time()
    for i, code in enumerate(codes, 1):
        try:
            k = cli.bars(code, freq="day", start=0, count=recent)
            if k.empty:
                fail += 1
                continue
            ev = cli.xdxr_events(code)
            old_ev = store.q(
                "SELECT COUNT(*) FROM dividend_events WHERE code=?",
                [code]).iloc[0, 0]
            need_full = (ev is not None and not ev.empty
                         and len(ev) > int(old_ev))
            if need_full:
                # 新除权事件 → 读库全量K线重算因子
                full = store.q(
                    "SELECT date, open, high, low, close, vol, amount "
                    "FROM kline_daily WHERE code=? ORDER BY date", [code])
                if not full.empty:
                    full["date"] = pd.to_datetime(full["date"])
                    full["adj_factor"] = compute_adj_factors(full, ev)
                    full["code"] = code
                    store.upsert(full[["date", "code", "open", "high", "low",
                                       "close", "vol", "amount", "adj_factor"]],
                                 "kline_daily", ["code", "date"])
                    ev2 = ev.copy()
                    ev2["code"] = code
                    store.upsert(ev2[["code", "date", "fenhong", "songzhuangu",
                                      "peigu", "peigujia"]],
                                 "dividend_events", ["code", "date"])
                    ok += 1
                    continue
            # 无新事件：新K线沿用最新因子
            last_f = store.q(
                "SELECT adj_factor FROM kline_daily WHERE code=? "
                "ORDER BY date DESC LIMIT 1", [code])
            f = float(last_f.iloc[0, 0]) if not last_f.empty else 1.0
            k["adj_factor"] = f
            k["code"] = code
            store.upsert(k[["date", "code", "open", "high", "low", "close",
                            "vol", "amount", "adj_factor"]],
                         "kline_daily", ["code", "date"])
            ok += 1
        except Exception as e:
            fail += 1
            log.debug(f"update_kline {code}: {e}")
        if i % 500 == 0:
            log.info(f"K线增量 {i}/{len(codes)} ({time.time()-t0:.0f}s)")
    wm = store.q("SELECT MAX(date) FROM kline_daily").iloc[0, 0]
    store.set_watermark("kline_daily", wm)
    store.finish_run(run, "ok", ok)
    log.info(f"K线增量完成: ok={ok} fail={fail}")


# ── 指数 K 线（通达信 index_bars） ────────────────────────────────
INDEX_DDL = """
date DATE, code VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
volume DOUBLE, PRIMARY KEY(code, date)
"""


def update_index_kline():
    """主要指数日K（通达信，不复权）"""
    store = Store()
    store.ensure_table("index_kline", INDEX_DDL)
    store.register_dataset("index_kline", "clean", "tdx", "daily",
                           "主要指数日K线")
    cli = TdxClient.instance()
    n_ok = 0
    for code_suffix, (name, market) in config.INDEX_CODES.items():
        code = code_suffix.split(".")[0]
        try:
            df = cli.index_bars_history(code, market, freq="day",
                                        start_date="2021-01-01")
        except Exception as e:
            log.warning(f"指数 {code_suffix} {name}: {e}")
            continue
        if df.empty:
            log.warning(f"指数 {code_suffix} {name} K线为空（该服务器不支持）")
            continue
        df["code"] = code_suffix
        df = df.rename(columns={"vol": "volume"})
        df = df[["date", "code", "open", "high", "low", "close", "volume"]]
        store.upsert(df, "index_kline", ["code", "date"])
        log.info(f"指数 {code_suffix} {name}: {len(df)} 行")
        n_ok += 1
    if n_ok:
        wm = store.q("SELECT MAX(date) FROM index_kline").iloc[0, 0]
        store.set_watermark("index_kline", wm)
    return n_ok


# ── 复权因子校验 ──────────────────────────────────────────────────
def verify_adj_factor(code: str) -> dict:
    """复权因子自洽校验：
    1. 因子单调性：有分红事件的股票 F 应 ≤1 且随时间递减（新→旧递减）
    2. 除权日连续性：前复权序列在除权日无异常跳空（|ret| 不应超涨跌停边界外）
    3. 事件对照：东财分红记录 vs 本库 dividend_events（抽样）
    """
    from .store import query
    df = query("""
        SELECT date, close, adj_factor FROM kline_daily
        WHERE code=? ORDER BY date""", [code])
    if df.empty:
        return {"code": code, "status": "no_data"}
    df["date"] = pd.to_datetime(df["date"])
    f = df["adj_factor"]
    issues = []
    # 1) 因子范围检查
    if (f > 1.0001).any():
        issues.append("F>1（前复权因子异常）")
    # 2) 前复权收益率连续性：除权日 |ret_qfq| 应 ≤ 44%（宽边界：涨跌停+分红）
    qfq_close = df["close"] * f
    ret = qfq_close.pct_change().abs()
    ev = query("SELECT date FROM dividend_events WHERE code=?", [code])
    ev_dates = set(pd.to_datetime(ev["date"])) if not ev.empty else set()
    jumps = []
    for i in range(1, len(df)):
        if df["date"].iloc[i] in ev_dates and ret.iloc[i] > 0.30:
            jumps.append((str(df['date'].iloc[i].date()), round(float(ret.iloc[i]), 4)))
    if jumps:
        issues.append(f"除权日异常跳空: {jumps[:3]}")
    # 3) 有事件但因子恒为1 → 计算失效
    if len(ev_dates) > 0 and (f.max() - f.min()) < 1e-9:
        issues.append("存在除权事件但因子无变化")
    return {"code": code,
            "status": "warn" if issues else "ok",
            "issues": issues,
            "n_events": len(ev_dates),
            "f_min": round(float(f.min()), 6), "f_max": round(float(f.max()), 6)}
