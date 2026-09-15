"""xiaodefa tushare 代理：数据体系补足回补脚本（全景规划批次 A/B）。

覆盖数据集（落湖 data/lake/clean/，全部幂等覆盖写，不碰主库写锁）：
  bench-index   基准指数日线 index_daily/part-{code}.parquet
                （000002.SH 上证A指 / 399107.SZ 深证A股 / 399102.SZ 创业板综 / 899050.BJ 北证50）
  namechange    曾用名全史 namechange/part-all.parquet（ST-PIT 重建原料）
  suspend       停复牌 2021-12 起 suspend/part-{year}.parquet
  sw            申万行业树 sw_classify/ + 成分历史 sw_member/
  limit-list    涨跌停名单 2022 起 limit_list/part-{year}.parquet（断点续跑）
  delisted      退市股清单 instruments_delisted/part-all.parquet（含 list/delist date）

工程纪律：
  - 绕本机代理直连；限流 120/min；重试 8 次指数退避
  - 写入一律覆盖不删（safe-delete 环境）
  - 网络长任务请后台跑（前台长跑会被 SIGTERM）
用法：
  python scripts/backfill_tushare_datasets.py --datasets all
  python scripts/backfill_tushare_datasets.py --datasets bench-index,namechange
  python scripts/backfill_tushare_datasets.py --datasets limit-list --start 20220101 --end 20240101
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import time
import urllib.request
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
LAKE = ROOT / "data" / "lake" / "clean"
LOG_FILE = ROOT / "logs" / "backfill_tushare_datasets.log"
URL = "https://t.xiaodefa.top/"
RATE_PER_MIN = 120.0


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


class FatalAuthError(RuntimeError):
    pass


def api(name: str, params: dict, retry: int = 8, timeout: float = 40.0,
        fields: str = "") -> tuple[list, list]:
    """返回 (fields, items)；认证失效抛 FatalAuthError。"""
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
                if "过期" in msg or "无效" in msg or d.get("code") == 2002:
                    raise FatalAuthError(f"{name}: {msg}")
                if "过快" in msg or "不要超过" in msg:
                    time.sleep(min(3.0 * (2 ** a), 60.0))
                    last = RuntimeError(f"限流: {msg}")
                    continue
                raise RuntimeError(f"{name} code={d.get('code')} {msg}")
            data = d.get("data") or {}
            return data.get("fields") or [], data.get("items") or []
        except FatalAuthError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(min(2.0 * (2 ** a), 30.0))
    raise RuntimeError(f"{name} 重试{retry}次仍失败: {last}")


def to_df(fields: list, items: list) -> pd.DataFrame:
    if not items:
        return pd.DataFrame()
    return pd.DataFrame(items, columns=fields)


def merge_write(out: Path, df: pd.DataFrame, keys: list[str]) -> None:
    """幂等覆盖写：存在则合并去重（keep=last）。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        old = pd.read_parquet(out)
        df = (pd.concat([old, df], ignore_index=True)
              .drop_duplicates(subset=keys, keep="last"))
    df.to_parquet(out, index=False)
    logging.info("写 %s: %d 行", out.name, len(df))


# ---------------------------------------------------------------- bench-index
BENCH_INDEXES = ["000002.SH", "399107.SZ", "399102.SZ", "899050.BJ"]


def fetch_bench_index(start: str, end: str) -> None:
    out_dir = LAKE / "index_daily"
    for ts in BENCH_INDEXES:
        out = out_dir / f"part-{ts}.parquet"
        frames = []
        s = date.fromisoformat(f"{start[:4]}-{start[4:6]}-{start[6:]}")
        e = date.fromisoformat(f"{end[:4]}-{end[4:6]}-{end[6:]}")
        cur = s
        while cur <= e:
            seg_end = min(date.fromordinal(
                min(cur.toordinal() + 1000, e.toordinal() + 1) - 1), e)  # ~4年/段
            f, it = api("index_daily", {"ts_code": ts,
                                        "start_date": cur.strftime("%Y%m%d"),
                                        "end_date": seg_end.strftime("%Y%m%d")})
            if it:
                frames.append(to_df(f, it))
            cur = date.fromordinal(seg_end.toordinal() + 1)
        if not frames:
            logging.warning("%s 无数据", ts)
            continue
        df = pd.concat(frames, ignore_index=True)
        df["code"] = ts
        df["date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
        for c in ("close", "open", "high", "low", "pre_close", "change", "pct_chg", "vol", "amount"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        keep = [c for c in ("date", "code", "open", "high", "low", "close",
                            "pre_close", "pct_chg", "vol", "amount") if c in df.columns]
        df = df[keep].sort_values("date").reset_index(drop=True)
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out, index=False)   # 指数全史整文件重写
        logging.info("bench %s: %d 行 %s ~ %s", ts, len(df),
                     df["date"].min().date(), df["date"].max().date())


# ---------------------------------------------------------------- namechange
def fetch_namechange(start_year: int, end_year: int) -> None:
    out = LAKE / "namechange" / "part-all.parquet"
    frames = []
    for y in range(start_year, end_year + 1):
        f, it = api("namechange", {"start_date": f"{y}0101", "end_date": f"{y}1231"})
        if it:
            frames.append(to_df(f, it))
            logging.info("namechange %d: %d 行", y, len(it))
    if not frames:
        logging.warning("namechange 无数据")
        return
    df = pd.concat(frames, ignore_index=True)
    df["code"] = df["ts_code"].str.split(".").str[0]
    for c in ("start_date", "end_date", "ann_date"):
        df[c] = pd.to_datetime(df[c], format="%Y%m%d", errors="coerce")
    keep = ["ts_code", "code", "name", "start_date", "end_date", "ann_date", "change_reason"]
    df = df[keep]
    merge_write(out, df, ["ts_code", "name", "start_date", "ann_date"])


# ---------------------------------------------------------------- suspend
def fetch_suspend(start: str, end: str) -> None:
    out_dir = LAKE / "suspend"
    s = date.fromisoformat(f"{start[:4]}-{start[4:6]}-{start[6:]}")
    e = date.fromisoformat(f"{end[:4]}-{end[4:6]}-{end[6:]}")
    buf: dict[int, list] = {}
    cur = s
    while cur <= e:
        # 当月末（或 end）为段终点
        if cur.month == 12:
            nxt = date(cur.year + 1, 1, 1)
        else:
            nxt = date(cur.year, cur.month + 1, 1)
        seg_end = min(date.fromordinal(nxt.toordinal() - 1), e)
        f, it = api("suspend_d", {"start_date": cur.strftime("%Y%m%d"),
                                  "end_date": seg_end.strftime("%Y%m%d")})
        if it:
            sdf = to_df(f, it)
            sdf["date"] = pd.to_datetime(sdf["trade_date"], format="%Y%m%d", errors="coerce")
            for yy, g in sdf.groupby(sdf["date"].dt.year):
                buf.setdefault(int(yy), []).append(g)
            logging.info("suspend %s~%s: %d 行", cur, seg_end, len(it))
        cur = date.fromordinal(seg_end.toordinal() + 1)
    for y, frames in buf.items():
        df = pd.concat(frames, ignore_index=True)
        df["code"] = df["ts_code"].str.split(".").str[0]
        df = df[["date", "code", "ts_code", "suspend_type", "suspend_timing"]]
        merge_write(out_dir / f"part-{y}.parquet", df, ["date", "code", "suspend_type"])


# ---------------------------------------------------------------- 申万
def fetch_sw() -> None:
    # 行业树
    frames = []
    for lv in ("L1", "L2", "L3"):
        f, it = api("index_classify", {"level": lv, "src": "SW2021"})
        if it:
            frames.append(to_df(f, it))
            logging.info("sw_classify %s: %d", lv, len(it))
    df = pd.concat(frames, ignore_index=True)
    (LAKE / "sw_classify").mkdir(parents=True, exist_ok=True)
    df.to_parquet(LAKE / "sw_classify" / "part-all.parquet", index=False)
    logging.info("sw_classify: %d 行", len(df))

    # 成分历史：index_member 在该代理上不按指数过滤（返回全集，已证伪废弃）。
    # 正确通道：index_member_all 按 L1 循环，返回每股 L1/L2/L3 三级归属（当前快照）。
    members = []
    l1_codes = df[df["level"] == "L1"]["index_code"].tolist()
    for i, idx_code in enumerate(l1_codes):
        f, it = api("index_member_all", {"l1_code": idx_code})
        if it:
            d = to_df(f, it)
            members.append(d)
        if (i + 1) % 10 == 0:
            logging.info("sw_member_all 进度 %d/%d", i + 1, len(l1_codes))
    mdf = pd.concat(members, ignore_index=True)
    mdf["code"] = mdf["ts_code"].str.split(".").str[0]
    keep = [c for c in ("l1_code", "l1_name", "l2_code", "l2_name",
                        "l3_code", "l3_name", "ts_code", "code", "name") if c in mdf.columns]
    mdf = mdf[keep].drop_duplicates()
    (LAKE / "sw_member_all").mkdir(parents=True, exist_ok=True)
    mdf.to_parquet(LAKE / "sw_member_all" / "part-all.parquet", index=False)
    logging.info("sw_member_all: %d 行 / %d 只股票（三级申万快照）",
                 len(mdf), mdf["code"].nunique())


# ---------------------------------------------------------------- limit-list
def fetch_limit_list(start: str, end: str) -> None:
    out_dir = LAKE / "limit_list"
    prog = out_dir / "_limit_progress.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    done: set = set()
    if prog.exists():
        done = set(json.loads(prog.read_text(encoding="utf-8")))

    import duckdb
    con = duckdb.connect(str(ROOT / "data" / "quant.duckdb"), read_only=True)
    try:
        days = con.execute(
            "select distinct date from kline_daily where date between ? and ? order by 1",
            [f"{start[:4]}-{start[4:6]}-{start[6:]}", f"{end[:4]}-{end[4:6]}-{end[6:]}"],
        ).fetchdf()["date"]
    finally:
        con.close()
    todo = [d.strftime("%Y%m%d") for d in pd.to_datetime(days) if d.strftime("%Y%m%d") not in done]
    logging.info("limit_list 交易日 %d，已完成 %d，待抓 %d", len(days), len(done), len(todo))

    buf: dict[int, pd.DataFrame] = {}
    n_fail = 0
    for i, td in enumerate(todo):
        day_frames = []
        for lt in ("U", "D", "Z"):
            try:
                f, it = api("limit_list_d", {"trade_date": td, "limit_type": lt})
            except Exception as e:  # noqa: BLE001
                n_fail += 1
                logging.error("%s %s 失败: %s", td, lt, e)
                if n_fail > 30:
                    raise
                continue
            if it:
                d = to_df(f, it)
                d["limit_type"] = lt
                day_frames.append(d)
        if day_frames:
            df = pd.concat(day_frames, ignore_index=True)
            df["code"] = df["ts_code"].str.split(".").str[0]
            df["date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
            for c in ("close", "pct_chg", "amount", "float_mv", "total_mv",
                      "turnover_ratio", "fd_amount", "open_times", "limit_times"):
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
            y = int(td[:4])
            buf[y] = pd.concat([buf.get(y, pd.DataFrame()), df], ignore_index=True)
        done.add(td)
        if (i + 1) % 50 == 0:
            logging.info("limit_list 进度 %d/%d", i + 1, len(todo))
            for y, df in buf.items():
                merge_write(out_dir / f"part-{y}.parquet", df, ["date", "code", "limit_type"])
            buf = {}
            prog.write_text(json.dumps(sorted(done)), encoding="utf-8")
    for y, df in buf.items():
        merge_write(out_dir / f"part-{y}.parquet", df, ["date", "code", "limit_type"])
    prog.write_text(json.dumps(sorted(done)), encoding="utf-8")
    logging.info("limit_list 完成：累计 %d 天", len(done))


# ---------------------------------------------------------------- delisted
def fetch_delisted() -> None:
    out = LAKE / "instruments_delisted" / "part-all.parquet"
    # 默认 fields 不含 delist_date，必须显式请求
    f, it = api("stock_basic", {"list_status": "D"},
                fields="ts_code,symbol,name,area,industry,market,list_date,delist_date,list_status")
    df = to_df(f, it)
    if df.empty:
        logging.warning("退市清单为空")
        return
    df["code"] = df["ts_code"].str.split(".").str[0]
    for c in ("list_date", "delist_date"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], format="%Y%m%d", errors="coerce")
    keep = [c for c in ("ts_code", "code", "name", "market", "exchange", "list_status",
                        "list_date", "delist_date", "industry", "area") if c in df.columns]
    out.parent.mkdir(parents=True, exist_ok=True)
    df[keep].to_parquet(out, index=False)
    if "delist_date" in df.columns:
        dd = df["delist_date"]
        recent = df[dd.notna() & (dd >= pd.Timestamp("2021-12-01"))]
        logging.info("退市清单: 全史 %d 只；2022 起退市 %d 只", len(df), len(recent))
    else:
        logging.info("退市清单: 全史 %d 只（无 delist_date 列，列=%s）", len(df), list(df.columns))


# ---------------------------------------------------------------- listed（G11：上市日期）
def fetch_listed() -> None:
    out = LAKE / "instruments_basic" / "part-all.parquet"
    f, it = api("stock_basic", {"list_status": "L"},
                fields="ts_code,symbol,name,area,industry,market,list_date,list_status,exchange")
    df = to_df(f, it)
    if df.empty:
        logging.warning("存量股清单为空")
        return
    df["code"] = df["ts_code"].str.split(".").str[0]
    df["list_date"] = pd.to_datetime(df["list_date"], format="%Y%m%d", errors="coerce")
    keep = [c for c in ("ts_code", "code", "name", "market", "exchange", "list_status",
                        "list_date", "industry", "area") if c in df.columns]
    out.parent.mkdir(parents=True, exist_ok=True)
    df[keep].to_parquet(out, index=False)
    logging.info("存量股清单: %d 只（含 list_date）", len(df))


# ---------------------------------------------------------------- margin-detail（批次 D）
def fetch_margin_detail(start: str, end: str) -> None:
    out_dir = LAKE / "margin_detail"
    prog = out_dir / "_margin_progress.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    done: set = set()
    if prog.exists():
        done = set(json.loads(prog.read_text(encoding="utf-8")))

    import duckdb
    con = duckdb.connect(str(ROOT / "data" / "quant.duckdb"), read_only=True)
    try:
        days = con.execute(
            "select distinct date from kline_daily where date between ? and ? order by 1",
            [f"{start[:4]}-{start[4:6]}-{start[6:]}", f"{end[:4]}-{end[4:6]}-{end[6:]}"],
        ).fetchdf()["date"]
    finally:
        con.close()
    todo = [d.strftime("%Y%m%d") for d in pd.to_datetime(days) if d.strftime("%Y%m%d") not in done]
    logging.info("margin_detail 交易日 %d，已完成 %d，待抓 %d", len(days), len(done), len(todo))

    buf: dict[int, pd.DataFrame] = {}
    n_fail = 0
    for i, td in enumerate(todo):
        try:
            f, it = api("margin_detail", {"trade_date": td})
        except Exception as e:  # noqa: BLE001
            n_fail += 1
            logging.error("%s 失败: %s", td, e)
            if n_fail > 30:
                raise
            continue
        if it:
            df = to_df(f, it)
            df["code"] = df["ts_code"].str.split(".").str[0]
            df["date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
            for c in df.columns:
                if c not in ("trade_date", "ts_code", "code", "date", "exchange"):
                    df[c] = pd.to_numeric(df[c], errors="coerce")
            y = int(td[:4])
            buf[y] = pd.concat([buf.get(y, pd.DataFrame()), df], ignore_index=True)
        done.add(td)
        if (i + 1) % 50 == 0:
            logging.info("margin_detail 进度 %d/%d", i + 1, len(todo))
            for y, df in buf.items():
                merge_write(out_dir / f"part-{y}.parquet", df, ["date", "code"])
            buf = {}
            prog.write_text(json.dumps(sorted(done)), encoding="utf-8")
    for y, df in buf.items():
        merge_write(out_dir / f"part-{y}.parquet", df, ["date", "code"])
    prog.write_text(json.dumps(sorted(done)), encoding="utf-8")
    logging.info("margin_detail 完成：累计 %d 天", len(done))


# ---------------------------------------------------------------- daily-basic（每日指标）
def fetch_daily_basic(start: str, end: str) -> None:
    """每日指标：换手(双口径)/量比/PE/PB/自由流通股本/涨跌停状态 limit_status。
    单次 6000 条 → 按日全市场一次返回。"""
    out_dir = LAKE / "daily_basic"
    _backfill_by_day("daily_basic", out_dir, start, end)


# ---------------------------------------------------------------- stk-limit（涨跌停价）
def fetch_stk_limit(start: str, end: str) -> None:
    """每日涨跌停价格（全市场，含未触板股票）。"""
    out_dir = LAKE / "stk_limit"
    _backfill_by_day("stk_limit", out_dir, start, end)


def _safe_numeric(df: pd.DataFrame, exclude: tuple[str, ...] = ("trade_date", "ts_code", "code", "date", "exchange", "name")) -> None:
    """仅对「全列为数值形态」的列做 to_numeric（保留 name/reason 等字符串列）。"""
    for c in df.columns:
        if c in exclude:
            continue
        s = df[c].dropna().astype(str)
        if len(s) and s.str.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?").all():
            df[c] = pd.to_numeric(df[c], errors="coerce")


def _paged(api_name: str, params: dict, page_cap: int, max_pages: int = 40):
    """offset 分页全量拉取（防服务器忽略 offset 死循环：max_pages 硬顶）。"""
    frames = []
    offset = 0
    for _ in range(max_pages):
        p = dict(params)
        if offset:
            p["offset"] = offset
        f, it = api(api_name, p)
        if not it:
            break
        frames.append(to_df(f, it))
        offset += len(it)
        if len(it) < page_cap:
            break
    return frames


def _trading_days(start: str, end: str) -> list[str]:
    """交易日历：湖内 index_daily 的 000002.SH（上证A指，1990 起全史）。
    不再连主库（2026-09-14 晨 4 段补跑撞 pipeline 写锁事故的根因修复），
    湖是 Parquet 文件直读，与 DuckDB 主库锁完全解耦，可并行。"""
    import duckdb
    pat = str(ROOT / "data" / "lake" / "clean" / "index_daily" / "part-000002.SH.parquet")
    con = duckdb.connect()
    try:
        days = con.execute(
            "select distinct date from read_parquet(?) "
            "where date between ? and ? order by 1",
            [pat, f"{start[:4]}-{start[4:6]}-{start[6:]}", f"{end[:4]}-{end[4:6]}-{end[6:]}"],
        ).fetchdf()["date"]
    finally:
        con.close()
    return [d.strftime("%Y%m%d") for d in pd.to_datetime(days)]


def _backfill_by_day(api_name: str, out_dir: Path, start: str, end: str) -> None:
    """按日全市场接口的通用回补（断点续跑 + 按年落盘）。"""
    prog = out_dir / f"_{api_name}_progress.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    done: set = set()
    if prog.exists():
        done = set(json.loads(prog.read_text(encoding="utf-8")))

    todo = [d for d in _trading_days(start, end) if d not in done]
    logging.info("%s 交易日 %d，已完成 %d，待抓 %d", api_name, len(todo) + len(done), len(done), len(todo))

    buf: dict[int, pd.DataFrame] = {}
    n_fail = 0
    for i, td in enumerate(todo):
        try:
            f, it = api(api_name, {"trade_date": td})
        except Exception as e:  # noqa: BLE001
            n_fail += 1
            logging.error("%s %s 失败: %s", api_name, td, e)
            if n_fail > 30:
                raise
            continue
        if it:
            df = to_df(f, it)
            df["code"] = df["ts_code"].str.split(".").str[0]
            df["date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
            _safe_numeric(df)
            y = int(td[:4])
            buf[y] = pd.concat([buf.get(y, pd.DataFrame()), df], ignore_index=True)
        done.add(td)
        if (i + 1) % 50 == 0:
            logging.info("%s 进度 %d/%d", api_name, i + 1, len(todo))
            for y, df in buf.items():
                merge_write(out_dir / f"part-{y}.parquet", df, ["date", "code"])
            buf = {}
            prog.write_text(json.dumps(sorted(done)), encoding="utf-8")
    for y, df in buf.items():
        merge_write(out_dir / f"part-{y}.parquet", df, ["date", "code"])
    prog.write_text(json.dumps(sorted(done)), encoding="utf-8")
    logging.info("%s 完成：累计 %d 天", api_name, len(done))


# ================================================================ 批次 2（用户点名 + 图上品类）
# ---------------------------------------------------------------- macro（宏观族）
def fetch_macro() -> None:
    """宏观 8 表：GDP/CPI/PPI/PMI/M2/SHIBOR报价/LPR/社融，各一调用全史。"""
    today = date.today().strftime("%Y%m%d")
    specs = [
        ("cn_gdp", {"start_date": "19900101", "end_date": today}),
        ("cn_cpi", {"start_date": "19900101", "end_date": today}),
        ("cn_ppi", {"start_date": "19990101", "end_date": today}),
        ("cn_pmi", {"start_date": "20050101", "end_date": today}),
        ("cn_m", {"start_m": "199601", "end_m": today[:6]}),
        ("shibor_quote", {"start_date": "20060101", "end_date": today}),
        ("shibor_lpr", {"start_date": "20130101", "end_date": today}),
        ("sf_month", {"start_m": "200201", "end_m": today[:6]}),
    ]
    out_dir = LAKE / "macro"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, params in specs:
        try:
            if name == "shibor_quote":   # 全史 ~5000 行超页上限，需 offset 分页
                frames = _paged("shibor_quote", params, page_cap=2000)
                it_all = []
                df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
                if not df.empty:
                    _safe_numeric(df, exclude=("quarter", "month", "date"))
                    df.to_parquet(out_dir / f"{name}.parquet", index=False)
                    logging.info("macro %s: %d 行", name, len(df))
                continue
            f, it = api(name, params)
        except Exception as e:  # noqa: BLE001
            logging.error("macro %s 失败: %s", name, e)
            continue
        df = to_df(f, it)
        if df.empty:
            logging.warning("macro %s 空数据", name)
            continue
        _safe_numeric(df, exclude=("quarter", "month", "date"))
        df.to_parquet(out_dir / f"{name}.parquet", index=False)
        logging.info("macro %s: %d 行", name, len(df))


# ---------------------------------------------------------------- stk-shock（个股异常波动，近端）
def fetch_stk_shock(start: str, end: str) -> None:
    """个股异常波动官方公告。代理仅近端有数据（2022/2015 探测全 0），按日快速过空。"""
    _backfill_by_day("stk_shock", LAKE / "stk_shock", start, end)


# ---------------------------------------------------------------- cyq-perf（筹码及胜率）
def fetch_cyq_perf(start: str, end: str) -> None:
    """每日筹码及胜率（成本分位/winner_ratio）。2018 起按日全市场。"""
    _backfill_by_day("cyq_perf", LAKE / "cyq_perf", start, end)


# ---------------------------------------------------------------- auction（集合竞价）
def fetch_auction(start: str, end: str) -> None:
    """开盘/收盘集合竞价，一次拉 O+C 两接口（2015 起全史可得）。"""
    out_dir = LAKE / "stk_auction"
    prog = out_dir / "_auction_progress.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    done: set = set()
    if prog.exists():
        done = set(json.loads(prog.read_text(encoding="utf-8")))
    todo = [d for d in _trading_days(start, end) if d not in done]
    logging.info("stk_auction 待抓 %d 天", len(todo))

    buf: dict[int, pd.DataFrame] = {}
    n_fail = 0
    for i, td in enumerate(todo):
        frames = []
        for tag, name in (("open", "stk_auction_o"), ("close", "stk_auction_c")):
            try:
                f, it = api(name, {"trade_date": td})
            except Exception as e:  # noqa: BLE001
                n_fail += 1
                logging.error("%s %s 失败: %s", name, td, e)
                if n_fail > 30:
                    raise
                continue
            if it:
                d = to_df(f, it)
                d["auction_type"] = tag
                frames.append(d)
        if frames:
            df = pd.concat(frames, ignore_index=True)
            df["code"] = df["ts_code"].str.split(".").str[0]
            df["date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
            _safe_numeric(df)
            y = int(td[:4])
            buf[y] = pd.concat([buf.get(y, pd.DataFrame()), df], ignore_index=True)
        done.add(td)
        if (i + 1) % 50 == 0:
            logging.info("stk_auction 进度 %d/%d", i + 1, len(todo))
            for y, df in buf.items():
                merge_write(out_dir / f"part-{y}.parquet", df,
                            ["date", "code", "auction_type"])
            buf = {}
            prog.write_text(json.dumps(sorted(done)), encoding="utf-8")
    for y, df in buf.items():
        merge_write(out_dir / f"part-{y}.parquet", df, ["date", "code", "auction_type"])
    prog.write_text(json.dumps(sorted(done)), encoding="utf-8")
    logging.info("stk_auction 完成：累计 %d 天", len(done))


# ---------------------------------------------------------------- top10-holders（前十大股东/流通）
def fetch_top10_holders(start_year: int, end_year: int) -> None:
    """按报告期 period + offset 分页（页上限 6000；全市场一期约 5.5 万行）。"""
    for api_name in ("top10_holders", "top10_floatholders"):
        out_dir = LAKE / api_name
        out_dir.mkdir(parents=True, exist_ok=True)
        prog = out_dir / "_progress.json"
        done: set = set()
        if prog.exists():
            done = set(json.loads(prog.read_text(encoding="utf-8")))
        periods = [f"{y}{md}" for y in range(start_year, end_year + 1)
                   for md in ("0331", "0630", "0930", "1231")]
        todo = [p for p in periods if p not in done]
        logging.info("%s 期数 %d，待抓 %d", api_name, len(periods), len(todo))
        for i, period in enumerate(todo):
            frames = _paged(api_name, {"period": period}, page_cap=6000)
            if frames:
                df = pd.concat(frames, ignore_index=True)
                df["code"] = df["ts_code"].str.split(".").str[0]
                for c in ("ann_date", "end_date"):
                    if c in df.columns:
                        df[c] = pd.to_datetime(df[c], format="%Y%m%d", errors="coerce")
                _safe_numeric(df, exclude=("trade_date", "ts_code", "code", "date",
                                           "holder_name", "holder_type"))
                merge_write(out_dir / f"part-{period}.parquet", df,
                            ["ts_code", "ann_date", "end_date", "holder_name"])
            done.add(period)
            if (i + 1) % 8 == 0:
                prog.write_text(json.dumps(sorted(done)), encoding="utf-8")
                logging.info("%s 进度 %d/%d 期", api_name, i + 1, len(todo))
        prog.write_text(json.dumps(sorted(done)), encoding="utf-8")
        logging.info("%s 完成：%d 期", api_name, len(done))


# ---------------------------------------------------------------- repurchase（回购）
def fetch_repurchase(start_year: int, end_year: int) -> None:
    """按年 + offset 分页（页上限 2000）。"""
    out_dir = LAKE / "repurchase"
    for y in range(start_year, end_year + 1):
        frames = _paged("repurchase", {"start_date": f"{y}0101", "end_date": f"{y}1231"},
                        page_cap=2000)
        if frames:
            df = pd.concat(frames, ignore_index=True)
            df["code"] = df["ts_code"].str.split(".").str[0]
            for c in ("ann_date", "end_date", "exp_date"):
                if c in df.columns:
                    df[c] = pd.to_datetime(df[c], format="%Y%m%d", errors="coerce")
            _safe_numeric(df)
            merge_write(out_dir / f"part-{y}.parquet", df,
                        ["ts_code", "ann_date", "proc"])
        logging.info("repurchase %d 完成", y)


# ---------------------------------------------------------------- holdertrade（股东增减持）
def fetch_holdertrade(start_year: int, end_year: int) -> None:
    """按月区间全市场 + offset 保险（页上限未知，空页即断）。"""
    out_dir = LAKE / "holdertrade"
    for y in range(start_year, end_year + 1):
        year_frames = []
        for m in range(1, 13):
            mend = 31 if m in (1, 3, 5, 7, 8, 10, 12) else (30 if m != 2 else 28)
            frames = _paged("stk_holdertrade",
                            {"start_date": f"{y}{m:02d}01", "end_date": f"{y}{m:02d}{mend}"},
                            page_cap=2000)
            if frames:
                year_frames.append(pd.concat(frames, ignore_index=True))
        if year_frames:
            df = pd.concat(year_frames, ignore_index=True)
            df["code"] = df["ts_code"].str.split(".").str[0]
            for c in ("ann_date", "change_date", "end_date"):
                if c in df.columns:
                    df[c] = pd.to_datetime(df[c], format="%Y%m%d", errors="coerce")
            _safe_numeric(df, exclude=("trade_date", "ts_code", "code", "date",
                                       "holder_name", "holder_type", "change_reason",
                                       "in_out", "manage_type"))
            merge_write(out_dir / f"part-{y}.parquet", df,
                        ["ts_code", "ann_date", "holder_name", "change_date" if "change_date" in df.columns else "ann_date"])
        logging.info("holdertrade %d 完成", y)


# ---------------------------------------------------------------- margin-secs（两融标的）
def fetch_margin_secs(start: str, end: str) -> None:
    _backfill_by_day("margin_secs", LAKE / "margin_secs", start, end)


# ---------------------------------------------------------------- surv（机构调研）
def fetch_surv(start_year: int, end_year: int) -> None:
    """机构调研按月区间全市场（~400-800 行/月）。"""
    out_dir = LAKE / "stk_surv"
    for y in range(start_year, end_year + 1):
        year_frames = []
        for m in range(1, 13):
            mend = 31 if m in (1, 3, 5, 7, 8, 10, 12) else (30 if m != 2 else 28)
            frames = _paged("stk_surv",
                            {"start_date": f"{y}{m:02d}01", "end_date": f"{y}{m:02d}{mend}"},
                            page_cap=2000)
            if frames:
                year_frames.append(pd.concat(frames, ignore_index=True))
        if year_frames:
            df = pd.concat(year_frames, ignore_index=True)
            df["code"] = df["ts_code"].str.split(".").str[0]
            for c in ("surv_date", "ann_date"):
                if c in df.columns:
                    df[c] = pd.to_datetime(df[c], format="%Y%m%d", errors="coerce")
            merge_write(out_dir / f"part-{y}.parquet", df,
                        ["ts_code", "surv_date"])
        logging.info("stk_surv %d 完成", y)


# ---------------------------------------------------------------- ah-comparison（AH比价，近端）
def fetch_ah_comparison(start: str, end: str) -> None:
    _backfill_by_day("stk_ah_comparison", LAKE / "ah_comparison", start, end)


# ---------------------------------------------------------------- pledge-stat（质押统计，按股全史）
def fetch_pledge_stat() -> None:
    """股权质押周频统计：代理不支持按 end_date 全市场 → 按股循环全史。"""
    out_dir = LAKE / "pledge_stat"
    out_dir.mkdir(parents=True, exist_ok=True)
    prog = out_dir / "_progress.json"
    done: set = set()
    if prog.exists():
        done = set(json.loads(prog.read_text(encoding="utf-8")))
    codes: set = set()
    for src in ("instruments_basic", "instruments_delisted"):
        p = LAKE / src / "part-all.parquet"
        if p.exists():
            codes.update(pd.read_parquet(p, columns=["ts_code"])["ts_code"].tolist())
    todo = sorted(codes - done)
    logging.info("pledge_stat 股票 %d，已完成 %d，待抓 %d", len(codes), len(done), len(todo))
    buf: list[pd.DataFrame] = []
    n_fail = 0
    for i, ts in enumerate(todo):
        try:
            f, it = api("pledge_stat", {"ts_code": ts})
        except Exception as e:  # noqa: BLE001
            n_fail += 1
            logging.error("pledge_stat %s 失败: %s", ts, e)
            if n_fail > 30:
                raise
            continue
        if it:
            df = to_df(f, it)
            df["code"] = df["ts_code"].str.split(".").str[0]
            df["end_date"] = pd.to_datetime(df["end_date"], format="%Y%m%d", errors="coerce")
            _safe_numeric(df, exclude=("ts_code", "code", "end_date"))
            buf.append(df)
        done.add(ts)
        if (i + 1) % 500 == 0:
            logging.info("pledge_stat 进度 %d/%d", i + 1, len(todo))
            if buf:
                merge_write(out_dir / "part-all.parquet",
                            pd.concat(buf, ignore_index=True), ["ts_code", "end_date"])
            buf = []
            prog.write_text(json.dumps(sorted(done)), encoding="utf-8")
    if buf:
        merge_write(out_dir / "part-all.parquet",
                    pd.concat(buf, ignore_index=True), ["ts_code", "end_date"])
    prog.write_text(json.dumps(sorted(done)), encoding="utf-8")
    logging.info("pledge_stat 完成：累计 %d 只", len(done))


# ---------------------------------------------------------------- pledge-detail（质押明细）
def fetch_pledge_detail(start_year: int, end_year: int) -> None:
    """按月公告区间 + offset 分页（页上限 1500）。"""
    out_dir = LAKE / "pledge_detail"
    for y in range(start_year, end_year + 1):
        year_frames = []
        for m in range(1, 13):
            mend = 31 if m in (1, 3, 5, 7, 8, 10, 12) else (30 if m != 2 else 28)
            frames = _paged("pledge_detail",
                            {"start_date": f"{y}{m:02d}01", "end_date": f"{y}{m:02d}{mend}"},
                            page_cap=1500)
            if frames:
                year_frames.append(pd.concat(frames, ignore_index=True))
        if year_frames:
            df = pd.concat(year_frames, ignore_index=True)
            df["code"] = df["ts_code"].str.split(".").str[0]
            for c in ("ann_date", "end_date"):
                if c in df.columns:
                    df[c] = pd.to_datetime(df[c], format="%Y%m%d", errors="coerce")
            _safe_numeric(df, exclude=("ts_code", "code", "ann_date", "end_date",
                                       "holder_name", "holder_type", "pledge_number"))
            merge_write(out_dir / f"part-{y}.parquet", df,
                        ["ts_code", "ann_date", "holder_name", "end_date"])
        logging.info("pledge_detail %d 完成", y)


# ---------------------------------------------------------------- options（期权）
def fetch_options(start: str, end: str) -> None:
    """期权：opt_basic 全表（分交易所 offset 分页）+ opt_daily 按日分页（页上限 15000）。"""
    out_dir = LAKE / "options"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 合约基础表
    frames = []
    for ex in ("SSE", "SZSE", "CFFEX", "DCE", "CZCE", "SHFE", "INE", "GFEX", "BSE"):
        frames.extend(_paged("opt_basic", {"exchange": ex}, page_cap=12000))
    if frames:
        df = pd.concat(frames, ignore_index=True)
        merge_write(out_dir / "opt_basic.parquet", df, ["ts_code"])
        logging.info("opt_basic: %d 条合约", len(df))

    # 期权日线
    prog = out_dir / "_opt_daily_progress.json"
    done: set = set()
    if prog.exists():
        done = set(json.loads(prog.read_text(encoding="utf-8")))
    todo = [d for d in _trading_days(start, end) if d not in done]
    logging.info("opt_daily 待抓 %d 天", len(todo))
    buf: dict[int, pd.DataFrame] = {}
    n_fail = 0
    for i, td in enumerate(todo):
        day_frames = []
        offset = 0
        for _ in range(20):   # 单日 30 万行硬顶防死循环
            try:
                p = {"trade_date": td}
                if offset:
                    p["offset"] = offset
                f, it = api("opt_daily", p)
            except Exception as e:  # noqa: BLE001
                n_fail += 1
                logging.error("opt_daily %s 失败: %s", td, e)
                if n_fail > 30:
                    raise
                day_frames = []
                break
            if not it:
                break
            day_frames.append(to_df(f, it))
            offset += len(it)
            if len(it) < 15000:
                break
        if day_frames:
            df = pd.concat(day_frames, ignore_index=True)
            df["code"] = df["ts_code"].str.split(".").str[0]
            df["date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
            _safe_numeric(df)
            y = int(td[:4])
            buf[y] = pd.concat([buf.get(y, pd.DataFrame()), df], ignore_index=True)
        done.add(td)
        if (i + 1) % 30 == 0:
            logging.info("opt_daily 进度 %d/%d", i + 1, len(todo))
            for yy, d2 in buf.items():
                merge_write(out_dir / f"opt_daily-{yy}.parquet", d2, ["date", "ts_code"])
            buf = {}
            prog.write_text(json.dumps(sorted(done)), encoding="utf-8")
    for yy, d2 in buf.items():
        merge_write(out_dir / f"opt_daily-{yy}.parquet", d2, ["date", "ts_code"])
    prog.write_text(json.dumps(sorted(done)), encoding="utf-8")
    logging.info("opt_daily 完成：累计 %d 天", len(done))


# ------------------------------------------- forecast/express（业绩预告/快报）
_WATCH = {"last": time.time()}


def _spawn_watchdog(stall_limit: int = 1800) -> None:
    """卡死看门狗（2026-09-14 晚实测：单线程脚本 54 分钟零进度 + 57 线程泄漏，
    疑似连接层挂死）。daemon 线程每 5 分钟巡检，进度停滞超 stall_limit 硬退出，
    由外层 wrapper 断点重拉。"""
    import threading
    import os

    def _watch():
        while True:
            time.sleep(300)
            if time.time() - _WATCH["last"] > stall_limit:
                logging.error("看门狗：进度停滞超 %ds，硬退出（rc=2）待 wrapper 重拉", stall_limit)
                os._exit(2)

    threading.Thread(target=_watch, daemon=True).start()


def _event_codes() -> list[str]:
    """标的清单：湖内 instruments_basic + instruments_delisted（不连主库）。"""
    import duckdb
    con = duckdb.connect()
    try:
        rows = con.execute(
            "select distinct ts_code from read_parquet(?) "
            "union select distinct ts_code from read_parquet(?)",
            [str(LAKE / "instruments_basic" / "part-*.parquet"),
             str(LAKE / "instruments_delisted" / "part-*.parquet")],
        ).fetchall()
    finally:
        con.close()
    return sorted(r[0] for r in rows if r[0])


def _flush_event_years(buf: list, out_dir: Path, keys: list) -> None:
    """按 ann_date 年分桶幂等落盘，flush 后清空缓冲。"""
    if not buf:
        return
    df = pd.concat(buf, ignore_index=True)
    years = pd.to_datetime(df["ann_date"]).dt.year.fillna(0).astype(int).astype(str)
    for y, g in df.groupby(years):
        merge_write(out_dir / f"part-{y}.parquet", g, keys)
    buf.clear()


def fetch_forecast_express(api_name: str) -> None:
    """业绩预告 forecast / 业绩快报 express。
    代理强制必填 ts_code（按期查询报 50101「必填参数, 标的」），故按标的循环；
    单只一次返回全史（页上限内）。按 ann_date 年分桶落湖，每 200 只 flush
    （merge_write 幂等，keys 含 update_flag 保留同日更正公告）。"""
    out_dir = LAKE / api_name
    out_dir.mkdir(parents=True, exist_ok=True)
    prog = out_dir / "_progress.json"
    done: set = set()
    if prog.exists():
        done = set(json.loads(prog.read_text(encoding="utf-8")))
    codes = _event_codes()
    todo = [c for c in codes if c not in done]
    logging.info("%s 标的 %d，待抓 %d", api_name, len(codes), len(todo))
    _spawn_watchdog()
    text_cols = ("trade_date", "ts_code", "code", "date", "name", "type",
                 "summary", "change_reason", "perf_summary")
    keys = ["ts_code", "ann_date", "end_date", "update_flag"]
    buf: list = []
    n_rows = 0
    for i, code in enumerate(todo):
        _WATCH["last"] = time.time()
        try:
            frames = _paged(api_name, {"ts_code": code}, page_cap=8000, max_pages=3)
        except FatalAuthError:
            raise
        except RuntimeError as e:
            logging.warning("%s %s 失败（不记done，下轮重试）: %s", api_name, code, str(e)[:80])
            continue
        if frames:
            df = pd.concat(frames, ignore_index=True)
            df["code"] = df["ts_code"].str.split(".").str[0]
            for c in ("ann_date", "end_date", "first_ann_date"):
                if c in df.columns:
                    df[c] = pd.to_datetime(df[c], format="%Y%m%d", errors="coerce")
            _safe_numeric(df, exclude=text_cols)
            buf.append(df)
            n_rows += len(df)
        done.add(code)
        if (i + 1) % 200 == 0:
            _flush_event_years(buf, out_dir, keys)
            prog.write_text(json.dumps(sorted(done)), encoding="utf-8")
            logging.info("%s 进度 %d/%d，累计 %d 行", api_name, i + 1, len(todo), n_rows)
    _flush_event_years(buf, out_dir, keys)
    prog.write_text(json.dumps(sorted(done)), encoding="utf-8")
    logging.info("%s 完成：%d 标的，%d 行", api_name, len(done), n_rows)


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--datasets", default="all",
                    help="逗号分隔：bench-index,namechange,suspend,sw,limit-list,delisted,listed,"
                         "margin-detail,daily-basic,stk-limit,macro,stk-shock,cyq-perf,auction,"
                         "top10-holders,repurchase,holdertrade,margin-secs,surv,ah-comparison,"
                         "pledge-stat,pledge-detail,options,forecast,express 或 all")
    ap.add_argument("--start", default="19901219")
    ap.add_argument("--end", default=None)
    ap.add_argument("--auction-start", default="20200101", help="集合竞价回补起点")
    ap.add_argument("--opt-start", default="20150209", help="期权日线回补起点")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
                  logging.StreamHandler()])
    if not TOKEN:
        raise SystemExit("token 未配置")
    end = args.end or date.today().strftime("%Y%m%d")
    ds = (args.datasets.split(",") if args.datasets != "all"
          else ["bench-index", "namechange", "suspend", "sw", "limit-list", "delisted", "listed"])
    for d in ds:
        t0 = time.time()
        logging.info("===== %s 开始 =====", d)
        if d == "bench-index":
            fetch_bench_index(args.start, end)
        elif d == "namechange":
            fetch_namechange(1995, int(end[:4]))
        elif d == "suspend":
            fetch_suspend("20211201", end)
        elif d == "sw":
            fetch_sw()
        elif d == "limit-list":
            fetch_limit_list("20220101", end)
        elif d == "delisted":
            fetch_delisted()
        elif d == "listed":
            fetch_listed()
        elif d == "margin-detail":
            fetch_margin_detail("20220101", end)
        elif d == "daily-basic":
            fetch_daily_basic("20220101", end)
        elif d == "stk-limit":
            fetch_stk_limit("20220101", end)
        elif d == "macro":
            fetch_macro()
        elif d == "stk-shock":
            fetch_stk_shock("20260201", end)
        elif d == "cyq-perf":
            fetch_cyq_perf("20180101", end)
        elif d == "auction":
            fetch_auction(args.auction_start, end)
        elif d == "top10-holders":
            fetch_top10_holders(2010, int(end[:4]))
        elif d == "repurchase":
            fetch_repurchase(2011, int(end[:4]))
        elif d == "holdertrade":
            fetch_holdertrade(2018, int(end[:4]))
        elif d == "margin-secs":
            fetch_margin_secs("20220101", end)
        elif d == "surv":
            fetch_surv(2022, int(end[:4]))
        elif d == "ah-comparison":
            fetch_ah_comparison("20260101", end)
        elif d == "pledge-stat":
            fetch_pledge_stat()
        elif d == "pledge-detail":
            fetch_pledge_detail(2022, int(end[:4]))
        elif d == "options":
            fetch_options(args.opt_start, end)
        elif d == "forecast":
            fetch_forecast_express("forecast")
        elif d == "express":
            fetch_forecast_express("express")
        else:
            logging.warning("未知数据集 %s", d)
        logging.info("===== %s 完成 (%.0fs) =====", d, time.time() - t0)


if __name__ == "__main__":
    main()
