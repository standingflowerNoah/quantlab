"""tushare 冗余兜底层（2026-09-14）：主源 + fsdb/sina 兜底后仍未推进的域，
用 xiaodefa tushare 代理再补一轮。

背景（2026-09-11 事故）：通达信服务器池全挂 + fsdb 上游停在 09-10，
kline_daily/index_kline 双双降级 0 命中，信号滞后一天需人工补数。
本模块把「tushare 作为所有更新数据的冗余」内置到 update_all 尾部：
sweep() 按注册表逐域检查水位，落后目标交易日才触发 tushare 兜底，
幂等 upsert，触发/结果均有广播可追溯。

注册表（API 支持性已用 20260911 阳性对照逐一验证，见
scripts/probe_ts_redundancy_apis.py；单位口径用 09-10 跨日一致性验证）：
  stale 触发（表 max(date) < 目标交易日即补）：
    kline_daily     daily+adj_factor 比值传播（vol 手→股×100, amount 千元→元×1000）
    daily_snapshot  daily(pct_chg/pre_close)+daily_basic(pe/pb/mv/换手/量比)
    index_kline     index_daily 7 只（880003.SH 通达信自编指数无对应，排除）
    margin_total    margin 分交易所求和（元→亿），rqye_chg=两日 rzrqye 差
    block_trade     block_trade（vol/amount 原生万股/万元；close/premium 无来源留空）
    dragon_tiger    top_list（net/buy/sell 元→万）
  on_fail 触发（仅主域 FAIL 时补，低频/未来窗口语义）：
    lockup          share_float（float_share 股→万股；窗口 target-3d~+90d）
    holder_num      stk_holdernumber（ann_date 近 10 日窗口）

明确排除（无 tushare 等价 / 已有冗余 / 语义不可回补）：
  hot_topic（同花顺热榜）、northbound_daily（北向逐日披露已停）、
  finance_snapshot（通达信盘面快照）、fund_flow_daily（全市场资金流冗余
  已由 lake moneyflow 域=tushare 主源覆盖）、perf_forecast（forecast 接口
  需按标的查询且金额单位未验证）、分钟/ETF 域（fsdb 主源+专用回补脚本）、
  instruments/industry_map/index_members（结构与语义复杂，V2 再议）。
"""
from __future__ import annotations

import pandas as pd

from ...config import get_logger
from ..store import Store
from .xd_tushare import api, to_df

log = get_logger(__name__)

KLINE_MAX_DAYS = 8          # kline_daily 单次 sweep 最多补的交易日数
OTHER_MAX_DAYS = 10         # 其余 stale 域单次最多回补天数
LOCKUP_WINDOW_DAYS = 90     # 与 eastmoney update_lockup 窗口一致
HOLDER_LOOKBACK_DAYS = 10


def _trading_days(store: Store, start: pd.Timestamp,
                  end: pd.Timestamp) -> list[pd.Timestamp]:
    df = store.q(
        "SELECT trade_date FROM trade_calendar "
        "WHERE trade_date > ? AND trade_date <= ? ORDER BY trade_date",
        [pd.Timestamp(start), pd.Timestamp(end)])
    return [pd.Timestamp(d) for d in df["trade_date"]]


def _missing_days(store: Store, table: str, target: pd.Timestamp,
                  cap: int) -> list[pd.Timestamp]:
    """表中目标交易日之后仍缺的交易日列表（cap 截断，从最早缺日起）"""
    try:
        mx = store.q(f"SELECT max(date) AS d FROM {table}")["d"][0]
    except Exception:                                        # noqa: BLE001
        return []
    if mx is None:
        return []                       # 空表=未初始化，不做全量兜底
    if pd.Timestamp(mx) >= target:
        return []
    days = _trading_days(store, pd.Timestamp(mx), target)
    return days[-cap:] if len(days) > cap else days


# ── kline_daily：daily + adj_factor 比值传播 ─────────────────────────
def _bf_kline_daily(store: Store, days: list[pd.Timestamp]) -> int:
    n = 0
    for d in days:
        td = d.strftime("%Y%m%d")
        prev_days = store.q(
            "SELECT max(date) AS d FROM kline_daily WHERE date < ?",
            [d])["d"][0]
        if prev_days is None:
            log.warning(f"[tushare冗余] kline {td}: 无前一日基准，跳过")
            continue
        prev = pd.Timestamp(prev_days).strftime("%Y%m%d")

        f, it = api("daily", {"trade_date": td})
        if not it:
            log.warning(f"[tushare冗余] kline {td}: 代理无数据")
            continue
        df = to_df(f, it)
        df["code"] = df["ts_code"].str.split(".").str[0]
        for c in ("open", "high", "low", "close", "vol", "amount"):
            df[c] = pd.to_numeric(df[c], errors="coerce")

        # 主库最近一日因子为基准，tushare 两日累计因子取比值传播
        base_df = store.q(
            "SELECT code, adj_factor FROM kline_daily WHERE date = ?", [prev_days])
        base_map = dict(zip(base_df["code"], base_df["adj_factor"]))

        fa, ia = api("adj_factor", {"trade_date": td})
        ts_now = to_df(fa, ia) if ia else pd.DataFrame()
        fa2, ia2 = api("adj_factor", {"trade_date": prev})
        ts_prev = to_df(fa2, ia2) if ia2 else pd.DataFrame()
        now_map = dict(zip(ts_now["ts_code"].str.split(".").str[0],
                           pd.to_numeric(ts_now["adj_factor"], errors="coerce")))
        prev_map = dict(zip(ts_prev["ts_code"].str.split(".").str[0],
                            pd.to_numeric(ts_prev["adj_factor"], errors="coerce")))
        codes = df["code"]
        now_f = codes.map(now_map).astype(float)
        prev_f = codes.map(prev_map).astype(float)
        ratio = (now_f / prev_f).where(
            now_f.notna() & prev_f.notna() & (prev_f != 0), 1.0)
        adj = codes.map(base_map).astype(float).fillna(1.0) * ratio

        out = pd.DataFrame({
            "date": d, "code": codes,
            "open": df["open"], "high": df["high"], "low": df["low"],
            "close": df["close"],
            "vol": df["vol"] * 100.0,          # 手 → 股
            "amount": df["amount"] * 1000.0,   # 千元 → 元
            "adj_factor": adj,
        })
        bad = (out[["open", "high", "low", "close"]] <= 0) | out["close"].isna()
        out = out[~bad.any(axis=1)]            # 停牌/占位 0 价行剔除（fsdb 惯例）
        if out.empty:
            continue
        need = out["adj_factor"].isna()
        if need.any():
            out.loc[need, "adj_factor"] = 1.0
        n += store.upsert(out, "kline_daily", ["code", "date"])
    return n


# ── daily_snapshot：daily + daily_basic ─────────────────────────────
def _bf_daily_snapshot(store: Store, days: list[pd.Timestamp]) -> int:
    try:
        names = store.q("SELECT code, name FROM instruments")
        name_map = dict(zip(names["code"], names["name"]))
    except Exception:                                        # noqa: BLE001
        name_map = {}
    n = 0
    for d in days:
        td = d.strftime("%Y%m%d")
        f, it = api("daily", {"trade_date": td})
        if not it:
            continue
        dk = to_df(f, it)
        f2, it2 = api("daily_basic", {"trade_date": td})
        if not it2:
            continue
        db = to_df(f2, it2)
        # close 两源同值：以 daily 的 close/pre_close 为准，去重防列名冲突
        df = dk.merge(db.drop(columns=["trade_date", "close"]),
                      on="ts_code", how="inner")
        df["code"] = df["ts_code"].str.split(".").str[0]
        out = pd.DataFrame({
            "date": d, "code": df["code"],
            "name": df["code"].map(name_map).fillna(""),
            "price": pd.to_numeric(df["close"], errors="coerce"),
            "last_close": pd.to_numeric(df["pre_close"], errors="coerce"),
            "change_pct": pd.to_numeric(df["pct_chg"], errors="coerce"),
            "turnover_pct": pd.to_numeric(df["turnover_rate"], errors="coerce"),
            "pe_ttm": pd.to_numeric(df["pe_ttm"], errors="coerce"),
            "pb": pd.to_numeric(df["pb"], errors="coerce"),
            "mcap_yi": pd.to_numeric(df["total_mv"], errors="coerce") / 1e4,
            "float_mcap_yi": pd.to_numeric(df["circ_mv"], errors="coerce") / 1e4,
            "vol_ratio": pd.to_numeric(df["volume_ratio"], errors="coerce"),
        })
        out = out[out["price"].notna() & (out["price"] > 0)]
        if out.empty:
            continue
        n += store.upsert(out, "daily_snapshot", ["date", "code"])
    return n


# ── index_kline：index_daily（880003.SH 无对应，排除） ───────────────
def _bf_index_kline(store: Store, days: list[pd.Timestamp]) -> int:
    from ...config import INDEX_CODES
    n = 0
    for ts in INDEX_CODES:
        if ts.startswith("880003"):
            continue                          # 通达信自编平均股价，代理无
        start = days[0].strftime("%Y%m%d")
        end = days[-1].strftime("%Y%m%d")
        try:
            f, it = api("index_daily",
                        {"ts_code": ts, "start_date": start, "end_date": end})
        except Exception as e:                            # noqa: BLE001
            log.warning(f"[tushare冗余] index {ts}: {e}")
            continue
        if not it:
            continue
        df = to_df(f, it)
        out = pd.DataFrame({
            "date": pd.to_datetime(df["trade_date"], format="%Y%m%d"),
            "code": ts,
            "open": pd.to_numeric(df["open"], errors="coerce"),
            "high": pd.to_numeric(df["high"], errors="coerce"),
            "low": pd.to_numeric(df["low"], errors="coerce"),
            "close": pd.to_numeric(df["close"], errors="coerce"),
            # tushare 指数 vol 单位手；与 TDX 口径可能存差异，仅作冗余参考
            "volume": pd.to_numeric(df["vol"], errors="coerce") * 100.0,
        })
        n += store.upsert(out, "index_kline", ["code", "date"])
    return n


# ── margin_total：margin 分交易所求和（元→亿），rqye_chg=两日差 ──────
def _bf_margin_total(store: Store, days: list[pd.Timestamp]) -> int:
    td_set = set(days)
    start = (days[0] - pd.Timedelta(days=7)).strftime("%Y%m%d")
    end = days[-1].strftime("%Y%m%d")
    f, it = api("margin", {"start_date": start, "end_date": end})
    if not it:
        return 0
    df = to_df(f, it)
    for c in ("rzye", "rqye", "rzrqye", "rzmre"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    tot = (df.groupby("trade_date")[["rzye", "rqye", "rzrqye", "rzmre"]]
           .sum().sort_index() / 1e8)                     # 元 → 亿
    tot["rqye_chg"] = tot["rzrqye"].diff()
    n = 0
    for td, r in tot.iterrows():
        d = pd.Timestamp(str(td))
        if d not in td_set:
            continue
        row = pd.DataFrame([{"date": d, "rzye": r["rzye"], "rqye": r["rqye"],
                             "rzrqye": r["rzrqye"], "rzmre": r["rzmre"],
                             "rqye_chg": r["rqye_chg"]}])
        n += store.upsert(row, "margin_total", ["date"])
    return n


# ── block_trade：block_trade（万股/万元原生） ────────────────────────
def _bf_block_trade(store: Store, days: list[pd.Timestamp]) -> int:
    n = 0
    for d in days:
        td = d.strftime("%Y%m%d")
        f, it = api("block_trade", {"start_date": td, "end_date": td})
        if not it:
            continue
        df = to_df(f, it)
        out = pd.DataFrame({
            "date": d,
            "code": df["ts_code"].str.split(".").str[0],
            "name": "",                                   # 代理无简称，留空
            "price": pd.to_numeric(df["price"], errors="coerce"),
            "close": None,                                # 当日收盘无来源，留空
            "premium_pct": None,
            "vol_wan": pd.to_numeric(df["vol"], errors="coerce"),
            "amount_wan": pd.to_numeric(df["amount"], errors="coerce"),
            "buyer": df.get("buyer", ""),
            "seller": df.get("seller", ""),
        })
        keys = ["date", "code", "price", "amount_wan"]
        out = out.drop_duplicates(subset=keys, keep="first")
        n += store.upsert(out, "block_trade", keys)
    return n


# ── dragon_tiger：top_list（元→万） ─────────────────────────────────
def _bf_dragon_tiger(store: Store, days: list[pd.Timestamp]) -> int:
    n = 0
    for d in days:
        td = d.strftime("%Y%m%d")
        f, it = api("top_list", {"trade_date": td})
        if not it:
            continue
        df = to_df(f, it)
        out = pd.DataFrame({
            "date": d,
            "code": df["ts_code"].str.split(".").str[0],
            "name": df.get("name", ""),
            "reason": df.get("reason", ""),
            "net_buy_wan": pd.to_numeric(df["net_amount"], errors="coerce") / 1e4,
            "buy_wan": pd.to_numeric(df["l_buy"], errors="coerce") / 1e4,
            "sell_wan": pd.to_numeric(df["l_sell"], errors="coerce") / 1e4,
            "close": pd.to_numeric(df["close"], errors="coerce"),
            "change_pct": pd.to_numeric(df["pct_change"], errors="coerce"),
            "turnover_pct": pd.to_numeric(df["turnover_rate"], errors="coerce"),
        })
        n += store.upsert(out, "dragon_tiger", ["date", "code", "reason"])
    return n


# ── lockup（on_fail）：share_float（股→万股），窗口对齐 eastmoney ────
def _bf_lockup(store: Store, target: pd.Timestamp) -> int:
    start = (target - pd.Timedelta(days=3)).strftime("%Y%m%d")
    end = (target + pd.Timedelta(days=LOCKUP_WINDOW_DAYS)).strftime("%Y%m%d")
    f, it = api("share_float", {"start_date": start, "end_date": end})
    if not it:
        return 0
    df = to_df(f, it)
    try:
        names = store.q("SELECT code, name FROM instruments")
        name_map = dict(zip(names["code"], names["name"]))
    except Exception:                                        # noqa: BLE001
        name_map = {}
    out = pd.DataFrame({
        "code": df["ts_code"].str.split(".").str[0],
        "name": df["ts_code"].str.split(".").str[0].map(name_map).fillna(""),
        "date": pd.to_datetime(df["float_date"], format="%Y%m%d",
                               errors="coerce"),
        "shares": pd.to_numeric(df["float_share"], errors="coerce") / 1e4,
        "ratio": pd.to_numeric(df["float_ratio"], errors="coerce"),
        "type": df.get("share_type", ""),
    })
    out = out[out["date"].notna()]
    return store.upsert(out, "lockup", ["code", "date", "type"])


# ── holder_num（on_fail）：stk_holdernumber（近 10 日公告窗口） ─────
def _bf_holder_num(store: Store, target: pd.Timestamp) -> int:
    start = (target - pd.Timedelta(days=HOLDER_LOOKBACK_DAYS)).strftime("%Y%m%d")
    end = target.strftime("%Y%m%d")
    f, it = api("stk_holdernumber", {"start_date": start, "end_date": end})
    if not it:
        return 0
    df = to_df(f, it)
    out = pd.DataFrame({
        "code": df["ts_code"].str.split(".").str[0],
        "end_date": pd.to_datetime(df["end_date"], format="%Y%m%d",
                                   errors="coerce"),
        "notice_date": pd.to_datetime(df["ann_date"], format="%Y%m%d",
                                      errors="coerce"),
        "holder_num": pd.to_numeric(df["holder_num"], errors="coerce")
        .astype("Int64"),
    })
    out = out[out["end_date"].notna() & out["notice_date"].notna()]
    return store.upsert(out, "holder_num", ["code", "end_date", "notice_date"])


# ── 注册表与 sweep 编排 ──────────────────────────────────────────────
# entry: (mode, check_table, backfill(store, days) | backfill(store, target))
REGISTRY: dict[str, dict] = {
    "kline_daily":     {"mode": "stale", "cap": KLINE_MAX_DAYS,
                        "fn": _bf_kline_daily},
    "daily_snapshot":  {"mode": "stale", "cap": OTHER_MAX_DAYS,
                        "fn": _bf_daily_snapshot},
    "index_kline":     {"mode": "stale", "cap": OTHER_MAX_DAYS,
                        "fn": _bf_index_kline},
    "margin_total":    {"mode": "stale", "cap": OTHER_MAX_DAYS,
                        "fn": _bf_margin_total},
    "block_trade":     {"mode": "stale", "cap": OTHER_MAX_DAYS,
                        "fn": _bf_block_trade},
    "dragon_tiger":    {"mode": "stale", "cap": OTHER_MAX_DAYS,
                        "fn": _bf_dragon_tiger},
    "lockup":          {"mode": "on_fail", "fn": _bf_lockup},
    "holder_num":      {"mode": "on_fail", "fn": _bf_holder_num},
}


def sweep(store: Store, target: pd.Timestamp,
          results: dict | None = None) -> int:
    """对注册表逐域检查并兜底。results=update_all 各域结果（判 on_fail）。

    返回本轮回补的域数；逐域结果记日志并汇总广播。
    """
    target = pd.Timestamp(target)
    detail: dict[str, str] = {}
    n_domain = 0
    for domain, cfg in REGISTRY.items():
        try:
            if cfg["mode"] == "stale":
                days = _missing_days(store, cfg.get("table", domain), target,
                                     cfg["cap"])
                if not days:
                    detail[domain] = "fresh"
                    continue
                n = cfg["fn"](store, days)
                detail[domain] = f"backfilled {n} rows ({days[0].date()}~{days[-1].date()})"
            else:  # on_fail
                res = str((results or {}).get(domain, ""))
                if "FAIL" not in res:
                    detail[domain] = "skip(主域成功)"
                    continue
                n = cfg["fn"](store, target)
                detail[domain] = f"backfilled {n} rows (on_fail)"
            try:
                col = "date"
                mx = store.q(f"SELECT max({col}) AS d FROM {domain}")["d"][0]
                if mx is not None:
                    store.set_watermark(domain, mx)
            except Exception:                                # noqa: BLE001
                pass
            n_domain += 1
            log.info(f"[tushare冗余] {domain}: {detail[domain]}")
        except Exception as e:                               # noqa: BLE001
            detail[domain] = f"FAIL: {str(e)[:80]}"
            log.error(f"[tushare冗余] {domain} 兜底失败: {e}")
    backfilled = {k: v for k, v in detail.items()
                  if not v.startswith(("fresh", "skip"))}
    if backfilled:
        from ...broadcast import broadcast
        broadcast("data", "tushare 冗余兜底触发", action="change",
                  detail="; ".join(f"{k}:{v}" for k, v in detail.items())[:800],
                  impact="主源/一级兜底未推进的域已由 tushare 代理补齐，"
                         "字段口径见 quantlab/data/sources/ts_redundancy.py 注释",
                  source="ts_redundancy")
    else:
        log.info(f"[tushare冗余] 全域水位新鲜，无需兜底: {detail}")
    return n_domain
