"""xiaodefa tushare 代理：湖表数据集的日度增量接入（update_all 域实现）。

覆盖湖表（全部幂等覆盖写，落 data/lake/clean/）：
  - stk_high_shock   严重异常波动官方公告（scripts/backfill_stk_high_shock.py 全史版）
  - moneyflow        全市场分单资金流（scripts/backfill_moneyflow_xiaodefa.py 全史版）
  - namechange       曾用名（年度重拉当年，ST-PIT 原料）
  - suspend          停复牌（近 40 天滚动窗口）
  - limit_list       涨跌停名单（当日 U/D/Z 三档）
  - index_daily      bench 基准指数（4 只，水位后增量）

全史回补请用 scripts/ 下的专用脚本；本模块只做增量。
"""
from __future__ import annotations

import gzip
import json
import os
import time
import urllib.request
from pathlib import Path

import pandas as pd

from ...config import get_logger, QUANT_ROOT

log = get_logger(__name__)

ROOT = QUANT_ROOT
LAKE = ROOT / "data" / "lake" / "clean"
URL = "https://t.xiaodefa.top/"
RATE_PER_MIN = 100.0   # 流水线内保守限流


def _load_token() -> str:
    t = os.environ.get("XIAODEFA_TOKEN", "").strip()
    if t:
        return t
    for p in (ROOT / ".secrets" / "xiaodefa_token",
              Path.home() / ".workbuddy" / "xiaodefa_token"):
        try:
            if p.exists():
                t = p.read_text(encoding="utf-8").strip()
                if t:
                    return t
        except OSError:
            continue
    return ""


for _k in ("http_proxy", "https_proxy", "all_proxy",
           "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(_k, None)
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
TOKEN = _load_token()

_next_at = 0.0


def _throttle() -> None:
    global _next_at
    now = time.time()
    start = max(now, _next_at)
    _next_at = start + 60.0 / RATE_PER_MIN
    d = start - now
    if d > 0:
        time.sleep(min(d, 5.0))


def api(name: str, params: dict, retry: int = 6, timeout: float = 40.0,
        fields: str = "") -> tuple[list, list]:
    body = json.dumps(
        {"api_name": name, "token": TOKEN, "params": params, "fields": fields}
    ).encode()
    last: Exception | None = None
    for a in range(retry):
        _throttle()
        try:
            req = urllib.request.Request(
                URL, data=body,
                headers={"Content-Type": "application/json", "Accept-Encoding": "gzip"})
            with _OPENER.open(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            d = json.loads(raw)
            if d.get("code") != 0:
                msg = str(d.get("msg"))
                if "过期" in msg or "无效" in msg:
                    raise RuntimeError(f"{name} token 失效: {msg}")
                if "过快" in msg or "不要超过" in msg:
                    time.sleep(min(3.0 * (2 ** a), 60.0))
                    last = RuntimeError(f"限流: {msg}")
                    continue
                raise RuntimeError(f"{name} code={d.get('code')} {msg}")
            data = d.get("data") or {}
            return data.get("fields") or [], data.get("items") or []
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(min(2.0 * (2 ** a), 30.0))
    raise RuntimeError(f"{name} 重试{retry}次仍失败: {last}")


def to_df(fields: list, items: list) -> pd.DataFrame:
    if not items:
        return pd.DataFrame()
    return pd.DataFrame(items, columns=fields)


def _merge_write(out: Path, df: pd.DataFrame, keys: list[str]) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        old = pd.read_parquet(out)
        df = (pd.concat([old, df], ignore_index=True)
              .drop_duplicates(subset=keys, keep="last"))
    df.to_parquet(out, index=False)
    return len(df)


def _last_trading_day(date) -> str:
    from .. import calendar
    d = date
    if d is None:
        d = calendar.last_trading_day()
    return pd.Timestamp(d).strftime("%Y%m%d")


# --------------------------------------------------------------- 各域增量

def update_stk_high_shock(date=None) -> int:
    out_dir = LAKE / "stk_high_shock"
    td = _last_trading_day(date)
    files = sorted(out_dir.glob("part-*.parquet"))
    import duckdb
    con = duckdb.connect()
    try:
        if files:
            fs = ", ".join(f"'{f.as_posix()}'" for f in files)
            mx = con.execute(
                f"select max(date) from read_parquet([{fs}], hive_partitioning=false)"
            ).fetchone()[0]
        else:
            mx = None
    finally:
        con.close()
    start = (pd.Timestamp(mx) + pd.Timedelta(days=1)).strftime("%Y%m%d") if mx else "20260101"
    if start > td:
        return 0
    f, it = api("stk_high_shock", {"start_date": start, "end_date": td})
    if not it:
        return 0
    df = to_df(f, it)
    pr = df["period"].astype(str)
    df["period_start"] = pd.to_datetime(pr.str[:8], format="%Y%m%d", errors="coerce")
    df["period_end"] = pd.to_datetime(pr.str[8:16], format="%Y%m%d", errors="coerce")
    df["period_raw"] = df["period"]
    df["code"] = df["ts_code"].str.split(".").str[0]
    df["date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
    keep = ["date", "code", "name", "trade_market", "reason",
            "period_start", "period_end", "period_raw"]
    n_new = 0
    for y, g in df[keep].groupby(df["date"].dt.year):
        n_new += len(g)
        _merge_write(out_dir / f"part-{int(y)}.parquet", g,
                     ["date", "code", "reason", "period_raw"])
    return n_new


def update_moneyflow(date=None) -> int:
    """单日全市场资金流（全史版见 scripts/backfill_moneyflow_xiaodefa.py）。"""
    td = _last_trading_day(date)
    out_dir = LAKE / "moneyflow"
    f, it = api("moneyflow", {"trade_date": td})
    if not it:
        return 0
    df = to_df(f, it)
    for c in df.columns:
        if c not in ("trade_date", "ts_code"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["code"] = df["ts_code"].str.split(".").str[0]
    df["date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
    for a, b in [("buy_elg_amount", "sell_elg_amount"),
                 ("buy_lg_amount", "sell_lg_amount"),
                 ("buy_md_amount", "sell_md_amount"),
                 ("buy_sm_amount", "sell_sm_amount")]:
        df[a.replace("buy", "net")] = df[a] - df[b]
    df = df.rename(columns={"net_elg_amount": "super_net",
                            "net_lg_amount": "large_net",
                            "net_md_amount": "mid_net",
                            "net_sm_amount": "small_net"})
    df["main_net"] = df["super_net"] + df["large_net"]
    keep = ["date", "code", "main_net", "super_net", "large_net",
            "mid_net", "small_net", "net_mf_amount"]
    y = int(td[:4])
    return _merge_write(out_dir / f"part-{y}.parquet", df[keep], ["date", "code"])


def update_namechange(date=None) -> int:
    """重拉当年（变更记录可能补录，整年覆盖合并）。"""
    year = _last_trading_day(date)[:4]
    f, it = api("namechange", {"start_date": f"{year}0101", "end_date": f"{year}1231"})
    if not it:
        return 0
    df = to_df(f, it)
    df["code"] = df["ts_code"].str.split(".").str[0]
    for c in ("start_date", "end_date", "ann_date"):
        df[c] = pd.to_datetime(df[c], format="%Y%m%d", errors="coerce")
    keep = ["ts_code", "code", "name", "start_date", "end_date", "ann_date", "change_reason"]
    out = LAKE / "namechange" / "part-all.parquet"
    old = pd.read_parquet(out) if out.exists() else pd.DataFrame()
    cur_year = pd.Timestamp(int(year), 1, 1)
    if len(old):
        old = old[old["start_date"] < cur_year]
    df2 = pd.concat([old, df[keep]], ignore_index=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    df2.to_parquet(out, index=False)
    return len(df)


def update_suspend(date=None) -> int:
    """近 40 天滚动窗口重拉（停复牌事件补录常见）。"""
    td = _last_trading_day(date)
    start = (pd.Timestamp(td) - pd.Timedelta(days=40)).strftime("%Y%m%d")
    f, it = api("suspend_d", {"start_date": start, "end_date": td})
    if not it:
        return 0
    df = to_df(f, it)
    df["code"] = df["ts_code"].str.split(".").str[0]
    df["date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
    keep = ["date", "code", "ts_code", "suspend_type", "suspend_timing"]
    n = 0
    for y, g in df[keep].groupby(df["date"].dt.year):
        n += len(g)
        _merge_write(LAKE / "suspend" / f"part-{int(y)}.parquet", g,
                     ["date", "code", "suspend_type"])
    return n


def update_limit_list(date=None) -> int:
    """当日 U/D/Z 三档涨跌停名单（全史版见 backfill_tushare_datasets.py）。"""
    td = _last_trading_day(date)
    frames = []
    for lt in ("U", "D", "Z"):
        f, it = api("limit_list_d", {"trade_date": td, "limit_type": lt})
        if it:
            d = to_df(f, it)
            d["limit_type"] = lt
            frames.append(d)
    if not frames:
        return 0
    df = pd.concat(frames, ignore_index=True)
    df["code"] = df["ts_code"].str.split(".").str[0]
    df["date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
    for c in ("close", "pct_chg", "amount", "float_mv", "total_mv",
              "turnover_ratio", "fd_amount", "open_times", "limit_times"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    y = int(td[:4])
    return _merge_write(LAKE / "limit_list" / f"part-{y}.parquet", df,
                        ["date", "code", "limit_type"])


BENCH_INDEXES = ["000002.SH", "399107.SZ", "399102.SZ", "899050.BJ"]
def update_bench_index(date=None) -> int:
    """4 只 bench 基准指数水位后增量（全史版见 backfill_tushare_datasets.py）。"""
    import duckdb
    td = _last_trading_day(date)
    n_new = 0
    con = duckdb.connect()
    try:
        for ts in BENCH_INDEXES:
            out = LAKE / "index_daily" / f"part-{ts}.parquet"
            if out.exists():
                mx = con.execute(
                    f"select max(date) from read_parquet('{out.as_posix()}', hive_partitioning=false)"
                ).fetchone()[0]
                start = (pd.Timestamp(mx) + pd.Timedelta(days=1)).strftime("%Y%m%d")
            else:
                start = "19901219"
            if start > td:
                continue
            f, it = api("index_daily", {"ts_code": ts, "start_date": start, "end_date": td})
            if not it:
                continue
            df = to_df(f, it)
            df["code"] = ts
            df["date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
            for c in ("close", "open", "high", "low", "pre_close", "pct_chg", "vol", "amount"):
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
            keep = [c for c in ("date", "code", "open", "high", "low", "close",
                                "pre_close", "pct_chg", "vol", "amount") if c in df.columns]
            old = pd.read_parquet(out) if out.exists() else pd.DataFrame()
            full = (pd.concat([old, df[keep]], ignore_index=True)
                    .drop_duplicates("date", keep="last").sort_values("date"))
            out.parent.mkdir(parents=True, exist_ok=True)
            full.to_parquet(out, index=False)
            n_new += len(df)
    finally:
        con.close()
    return n_new


# ------------------------------------------------- 批次 2 域：用户点名 + 图上品类
def _safe_numeric(df: pd.DataFrame) -> None:
    """仅对「全列为数值形态」的列做 to_numeric（保留 name/reason 等字符串列）。"""
    for c in df.columns:
        if c in ("trade_date", "ts_code", "code", "date", "exchange"):
            continue
        s = df[c].dropna().astype(str)
        if len(s) and s.str.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?").all():
            df[c] = pd.to_numeric(df[c], errors="coerce")


def _daily_upsert(api_name: str, lake_name: str, date,
                  by_day: bool = True, keys: list[str] | None = None) -> int:
    """单日事件流通用增量：一调用落地 part-{year}.parquet（幂等合并）。"""
    td = _last_trading_day(date)
    params = {"trade_date": td} if by_day else {"start_date": td, "end_date": td}
    f, it = api(api_name, params)
    if not it:
        return 0
    df = to_df(f, it)
    df["code"] = df["ts_code"].str.split(".").str[0]
    df["date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce") \
        if "trade_date" in df.columns else pd.Timestamp(td)
    _safe_numeric(df)
    y = int(td[:4])
    return _merge_write(LAKE / lake_name / f"part-{y}.parquet", df,
                        keys or ["date", "code"])


def update_stk_shock(date=None) -> int:
    """个股异常波动官方公告（普通口径，与 stk_high_shock 严重口径配对）。"""
    return _daily_upsert("stk_shock", "stk_shock", date)


def update_cyq_perf(date=None) -> int:
    """每日筹码及胜率（成本分位/winner_ratio）。"""
    return _daily_upsert("cyq_perf", "cyq_perf", date)


def update_margin_secs(date=None) -> int:
    """两融标的池（盘前）。"""
    return _daily_upsert("margin_secs", "margin_secs", date)


def update_ah_comparison(date=None) -> int:
    """AH 比价（近端口径，代理仅 2026 起有数据）。"""
    return _daily_upsert("stk_ah_comparison", "ah_comparison", date)


def update_stk_surv(date=None) -> int:
    """机构调研记录（公告口径，按日期窗口）。"""
    return _daily_upsert("stk_surv", "stk_surv", date, by_day=False,
                         keys=["date", "code"])


def update_repurchase(date=None) -> int:
    """股票回购（公告口径）。"""
    return _daily_upsert("repurchase", "repurchase", date, by_day=False,
                         keys=["date", "code", "ann_date"])


def update_holdertrade(date=None) -> int:
    """股东增减持（公告口径）。"""
    return _daily_upsert("stk_holdertrade", "holdertrade", date, by_day=False,
                         keys=["date", "code", "ann_date"])


def update_auction(date=None) -> int:
    """开盘/收盘集合竞价（两接口）。"""
    td = _last_trading_day(date)
    frames = []
    for tag, name in (("open", "stk_auction_o"), ("close", "stk_auction_c")):
        f, it = api(name, {"trade_date": td})
        if it:
            d = to_df(f, it)
            d["auction_type"] = tag
            frames.append(d)
    if not frames:
        return 0
    df = pd.concat(frames, ignore_index=True)
    df["code"] = df["ts_code"].str.split(".").str[0]
    df["date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
    _safe_numeric(df)
    y = int(td[:4])
    return _merge_write(LAKE / "stk_auction" / f"part-{y}.parquet", df,
                        ["date", "code", "auction_type"])


def update_opt_daily(date=None) -> int:
    """期权日线（单日可超 1.5 万行，offset 分页）。"""
    td = _last_trading_day(date)
    frames = []
    offset = 0
    for _ in range(20):
        p = {"trade_date": td}
        if offset:
            p["offset"] = offset
        f, it = api("opt_daily", p)
        if not it:
            break
        frames.append(to_df(f, it))
        offset += len(it)
        if len(it) < 15000:
            break
    if not frames:
        return 0
    df = pd.concat(frames, ignore_index=True)
    df["code"] = df["ts_code"].str.split(".").str[0]
    df["date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
    _safe_numeric(df)
    y = int(td[:4])
    return _merge_write(LAKE / "options" / f"opt_daily-{y}.parquet", df,
                        ["date", "ts_code"])
