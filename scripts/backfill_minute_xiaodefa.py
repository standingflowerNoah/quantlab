"""用 t.xiaodefa.top (tushare 代理) 回填 QuantLab 历史分钟 K 线。

背景
----
湖内 `data/lake/clean/kline_1min/` 由 free-stockdb(fsdb) 供给，覆盖 2025-01-02 起；
2022-2024 为历史空白（东财/baostock 均无 1min 免费源）。本脚本用 tushare 代理补齐。

已实测的口径与约束（2026-09-11）
--------------------------------
- 深度：600519.SH 1min 回溯至 2005-01-04 有效，每日恒 241 根（北交所 271 根，含 15:00-15:30 盘后）。
- 覆盖：沪/深/创业板/科创板/北交所/ETF 均支持；指数需换用 `idx_mins` 接口。
- 单次上限：**8000 行**。按月切段（~5100 行）安全；按 30 交易日切段会触发服务端
  **静默截断**（实测丢 50/726 天），务必按自然月切。
- 只支持按 `ts_code` 单只查询，无「按日全市场」批量模式。
- **⚠️ 硬并发上限 = 5 个连接**：超过即返回 `429 请不要超过5个线程`（实测并发 12 时
  288 次请求里 142 次被拒）。**这不是速率限制而是连接数限制**，调速率参数无用，
  必须把 workers 压到 ≤5。并发 5 直连可持续 ~210 req/min。
- **⚠️ 必须绕开本机 HTTP 代理隧道**：本机全局 `https_proxy=http://127.0.0.1:57092`，
  走代理偶发 502 且延迟从 1.4s 抬到 4.5s。脚本已在导入时清除代理 env 并用
  `ProxyHandler({})` 直连。
- 价格：**不复权原始价**，与 fsdb 同源（12 只样本中 9 只逐根字节级一致），
  日总量差 < 50 股；少数票存在 2~4% 的分钟量能重分配 → 跨源混用需注意。

用法
----
    # 冒烟测试（10 只票，2024-01 单月）
    python scripts/backfill_minute_xiaodefa.py --start 2024-01 --end 2024-01 --limit 10

    # 正式回填 2022-2024 全市场
    python scripts/backfill_minute_xiaodefa.py --start 2022-01 --end 2024-12 --workers 12

    # 断点续跑（默认行为，读 _xd_progress.json 跳过已完成）
    python scripts/backfill_minute_xiaodefa.py --start 2022-01 --end 2024-12

    # 校验：抽样比对日总量与 kline_daily
    python scripts/backfill_minute_xiaodefa.py --verify-only --start 2022-01 --end 2024-12
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
LAKE_1MIN = ROOT / "data" / "lake" / "clean" / "kline_1min"
DAILY = ROOT / "data" / "lake" / "clean" / "mirror" / "kline_daily.parquet"
PROGRESS = LAKE_1MIN / "_xd_progress.json"
CALL_LOG = ROOT / "logs" / "backfill_minute_xiaodefa.log"

TOKEN = os.environ.get(
    "XIAODEFA_TOKEN",
    "65cc2e7b8c4266142c1bc426db6d4c16f183531d34f7e701833f1790",
)
URL = "https://t.xiaodefa.top/"

# ⚠️ 本机全局挂了 HTTP 代理隧道（env https_proxy=http://127.0.0.1:57092），
# 走代理会偶发 502 且延迟从 1.5s 抬到 4.5s。此源可直连，必须绕开代理。
for _k in ("http_proxy", "https_proxy", "all_proxy",
           "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(_k, None)
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 服务端硬性并发上限：超过即 429 "请不要超过5个线程"
MAX_WORKERS = 5

FLUSH_ROWS = 2_000_000  # 缓冲到该行数即落盘
CHUNK = "month"         # 切段粒度：month（唯一安全值）

log = logging.getLogger("xd_backfill")

# 全局落盘计数器（决定 part-xdNNNNN 编号）
_seq_lock = threading.Lock()
_seq = 0


def _next_seq() -> int:
    global _seq
    with _seq_lock:
        _seq += 1
        return _seq


def init_seq() -> None:
    """从已有 part-xdNNNNN.parquet 恢复编号，避免覆盖旧文件。"""
    global _seq
    mx = 0
    for f in LAKE_1MIN.glob("year=*/part-xd*.parquet"):
        try:
            mx = max(mx, int(f.stem.replace("part-xd", "")))
        except ValueError:
            continue
    _seq = mx
    if mx:
        log.info("已有 part-xd 最大编号 %d，从 %d 继续", mx, mx + 1)


# --------------------------------------------------------------------------- #
# 接口层
# --------------------------------------------------------------------------- #
class RateLimiter:
    """全局令牌桶 + 限流惩罚。代理对 stk_mins 有速率限制（'您请求速度过快'），
    大 payload 下并发 12 会触发，必须主动限速而非单纯靠并发。

    实现要点：时段必须在**进入等待前一次性预约**。若把预约放进重试循环里，
    多个线程会互相把 next_at 不断往后推，形成活锁（所有线程永久 sleep）。
    """

    def __init__(self, rate_per_min: float) -> None:
        self.interval = 60.0 / max(rate_per_min, 0.1)
        self.lock = threading.Lock()
        self.next_at = 0.0
        self.cooldown_until = 0.0
        self.n_throttle = 0

    def acquire(self) -> None:
        with self.lock:
            now = time.time()
            start = max(now, self.next_at, self.cooldown_until)
            self.next_at = start + self.interval
        while True:
            delay = start - time.time()
            if delay > 0:
                time.sleep(min(delay, 5.0))
                continue
            # 等待期间可能被 penalize 拉长冷却，需要重新确认
            with self.lock:
                rem = self.cooldown_until - time.time()
            if rem > 0:
                time.sleep(min(rem, 5.0))
                continue
            return

    def penalize(self, seconds: float) -> None:
        with self.lock:
            self.n_throttle += 1
            self.cooldown_until = max(self.cooldown_until, time.time() + seconds)


LIMITER: RateLimiter = RateLimiter(60)


def api(api_name: str, params: dict, retry: int = 8, timeout: int = 180) -> dict:
    """调用代理接口。代理隧道偶发 502、且对 stk_mins 有速率限制，均需重试。"""
    body = json.dumps(
        {"api_name": api_name, "token": TOKEN, "params": params, "fields": ""}
    ).encode()
    last: Exception | None = None
    for a in range(retry):
        LIMITER.acquire()
        try:
            req = urllib.request.Request(
                URL,
                data=body,
                headers={"Content-Type": "application/json", "Accept-Encoding": "gzip"},
            )
            with _OPENER.open(req, timeout=timeout) as r:
                raw = r.read()
                enc = r.headers.get("Content-Encoding")
            if enc == "gzip":
                raw = gzip.decompress(raw)
            d = json.loads(raw)
            if d.get("code") != 0:
                msg = str(d.get("msg"))
                if "区间超限" in msg or "必填参数" in msg or "接口名" in msg:
                    raise ValueError(f"{api_name} {msg}")
                if "过快" in msg or "不要超过" in msg:
                    # 指数退避 + 全局冷却，避免所有 worker 同时撞限制
                    back = min(3.0 * (2 ** a), 60.0)
                    LIMITER.penalize(back)
                    last = RuntimeError(f"限流: {msg}")
                    continue
                raise RuntimeError(f"{api_name} code={d.get('code')} msg={msg}")
            return d
        except ValueError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(min(2.0 * (a + 1), 15))
    raise last  # type: ignore[misc]


def fetch_calendar(start: str, end: str) -> list[str]:
    """返回 YYYYMMDD 格式的交易日列表。"""
    d = api("trade_cal", {"exchange": "SSE", "start_date": start, "end_date": end})
    return sorted(x[1] for x in d["data"]["items"] if x[2] == 1)


def month_chunks(start: str, end: str) -> list[tuple[date, date]]:
    """把 [start, end] 拆成自然月区间（右开）。"""
    s = datetime.strptime(start, "%Y-%m-%d").date().replace(day=1)
    e = datetime.strptime(end, "%Y-%m-%d").date()
    out = []
    y, m = s.year, s.month
    while date(y, m, 1) <= e:
        nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        out.append((date(y, m, 1), nxt))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# --------------------------------------------------------------------------- #
# 代码转换
# --------------------------------------------------------------------------- #
def to_ts_code(code: str) -> str:
    """湖内 6 位代码 -> tushare ts_code。"""
    c = str(code).zfill(6)
    if c.startswith(("60", "68", "90", "51", "56", "58", "50", "52")):
        return f"{c}.SH"
    if c.startswith(("00", "30", "20", "15", "16", "18", "12")):
        return f"{c}.SZ"
    if c.startswith(("92", "83", "87", "43", "82", "88")):
        return f"{c}.BJ"
    raise ValueError(f"无法判断交易所: {c}")


def load_universe(start_ymd: str, end_ymd: str) -> list[str]:
    """从 kline_daily 取目标区间内出现过的全部标的（不含指数）。"""
    con = duckdb.connect()
    try:
        df = con.execute(
            f"""SELECT DISTINCT code FROM read_parquet('{DAILY.as_posix()}')
                WHERE date >= '{start_ymd[:4]}-{start_ymd[4:6]}-01'
                  AND date <= '{end_ymd[:4]}-{end_ymd[4:6]}-{end_ymd[6:]}'""",
            ).fetchall()
    finally:
        con.close()
    codes = sorted({r[0] for r in df})
    return codes


# --------------------------------------------------------------------------- #
# 拉取单只
# --------------------------------------------------------------------------- #
ROW_CAP = 8000  # 服务端单次返回上限；触顶会「静默截断」，实测按 30 交易日切段会丢 50/726 天


def _fetch_span(ts_code: str, s: date, e: date, freq: str) -> list:
    """拉取 [s, e] 区间。若返回行数触顶（8000），说明被静默截断，自动二分重取。"""
    d = api("stk_mins", {
        "ts_code": ts_code,
        "freq": freq,
        "start_date": s.isoformat() + " 09:00:00",
        "end_date": e.isoformat() + " 15:35:00",
    })
    items = d["data"]["items"]
    if len(items) < ROW_CAP - 1:
        return items
    if s >= e:
        log.error("%s %s 单日即触顶（%d 行），无法再拆分", ts_code, s, len(items))
        return items
    mid = s + (e - s) / 2
    log.warning("%s %s~%s 返回 %d 行触顶，二分重取", ts_code, s, e, len(items))
    return _fetch_span(ts_code, s, mid, freq) + _fetch_span(ts_code, mid + timedelta(days=1), e, freq)


def pull_stock(ts_code: str, months: list[tuple[date, date]], freq: str) -> pd.DataFrame:
    frames = []
    for s, e in months:
        items = _fetch_span(ts_code, s, e - timedelta(days=1), freq)
        if items:
            frames.append(pd.DataFrame(
                items,
                columns=["ts_code", "trade_time", "open", "close", "high", "low", "vol", "amount"],
            ))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["datetime"] = pd.to_datetime(df["trade_time"]).astype("datetime64[us]")
    df["code"] = ts_code.split(".")[0]
    df = df[["datetime", "code", "open", "high", "low", "close", "vol", "amount"]]
    df = df.drop_duplicates(subset=["code", "datetime"]).sort_values("datetime")
    df["vol"] = pd.to_numeric(df["vol"], errors="coerce").astype("float64")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").astype("float64")
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #
class Writer:
    """按 year 分区缓冲落盘，part 命名 part-xdNNNNN.parquet。"""

    def __init__(self, outdir: Path | None = None, dry_run: bool = False) -> None:
        self.outdir = Path(outdir) if outdir else LAKE_1MIN
        self.dry_run = dry_run
        self.buf: dict[int, list[pd.DataFrame]] = {}
        self.n = 0

    def add(self, df: pd.DataFrame) -> None:
        if df.empty:
            return
        if self.dry_run:
            self.n += len(df)
            return
        for y, g in df.groupby(df["datetime"].dt.year):
            self.buf.setdefault(int(y), []).append(g.drop(columns=["datetime"]).assign(
                datetime=g["datetime"].values, year=int(y)
            ))
        self.n += len(df)
        if self.n >= FLUSH_ROWS:
            self.flush()

    def flush(self) -> None:
        if self.dry_run:
            log.info("[dry-run] 跳过落盘，累计 %d 行", self.n)
            self.buf = {}
            self.n = 0
            return
        for y, parts in self.buf.items():
            if not parts:
                continue
            df = pd.concat(parts, ignore_index=True)
            d = self.outdir / f"year={y}"
            d.mkdir(parents=True, exist_ok=True)
            out = d / f"part-xd{_next_seq():05d}.parquet"
            df = df[["datetime", "code", "open", "high", "low", "close", "vol", "amount", "year"]]
            df.to_parquet(out, index=False, compression="zstd")
            log.info("落盘 %s  %d 行", out, len(df))
        self.buf = {}
        self.n = 0


def load_progress() -> dict:
    """进度结构: {"ranges": {"<start>~<end>": {"done": [...], "failed": {...}}}}"""
    if PROGRESS.exists():
        d = json.loads(PROGRESS.read_text(encoding="utf-8"))
        if "ranges" not in d:
            d = {"ranges": {}}
        return d
    return {"ranges": {}}


def save_progress(p: dict) -> None:
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROGRESS.with_suffix(".tmp")
    tmp.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(PROGRESS)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run_backfill(args: argparse.Namespace) -> None:
    start_ymd = args.start.replace("-", "")
    end_ymd = args.end.replace("-", "")

    months = month_chunks(args.start, args.end)
    days = fetch_calendar(start_ymd, end_ymd)
    log.info("区间 %s ~ %s : %d 个自然月, %d 个交易日", args.start, args.end, len(months), len(days))

    codes = load_universe(start_ymd, end_ymd)
    if args.limit:
        codes = codes[: args.limit]
    # 指数不通过 stk_mins 提供
    codes = [c for c in codes if not c.startswith(("000300", "000905", "000852", "399006"))]
    log.info("标的数 %d", len(codes))

    years = sorted({s.year for s, _ in months} | {e.year for _, e in months})
    for y in years:
        d = LAKE_1MIN / f"year={y}"
        if not d.exists():
            continue
        foreign = [f for f in d.glob("*.parquet") if not f.stem.startswith("part-xd")]
        if foreign:
            log.warning("year=%d 已存在 %d 个非 part-xd 文件（fsdb 来源）。"
                        "本脚本只追加 part-xd*，不会覆盖，但请注意湖内将出现双源并存。",
                        y, len(foreign))

    # 进度必须按「区间」隔离：同一只票在不同区间下完成度不同，
    # 用全局 done 集合会导致换区间后误跳过（如冒烟跑过 2024-01，全量跑就会漏 2022-2023）。
    prog_all = load_progress()
    rng = f"{args.start}~{args.end}"
    ranges = prog_all.setdefault("ranges", {})
    if prog_all.get("months") and prog_all["months"] != rng:
        log.warning("进度文件区间 %s 与本次 %s 不同，按新区间独立记录",
                    prog_all["months"], rng)
    cur = ranges.setdefault(rng, {"done": [], "failed": {}})
    done = set(cur["done"])
    prog = cur
    prog["months"] = rng

    todo = [c for c in codes if c not in done]
    log.info("已完成 %d, 待处理 %d", len(done), len(todo))

    if args.workers > MAX_WORKERS:
        log.warning("workers=%d 超过服务端硬上限 %d（会触发 429 请不要超过5个线程），已钳制",
                    args.workers, MAX_WORKERS)
        args.workers = MAX_WORKERS

    init_seq()
    writer = Writer(args.outdir, args.dry_run)
    lock = threading.Lock()
    t0 = time.time()
    n_ok = n_fail = 0
    fail_detail: dict[str, str] = {}

    def work(code: str):
        ts = to_ts_code(code)
        df = pull_stock(ts, months, args.freq)
        return code, df, None

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(work, c): c for c in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            code = futs[fut]
            try:
                _, df, _ = fut.result()
                with lock:
                    writer.add(df)
                    done.add(code)
                    n_ok += 1
                    if n_ok % 50 == 0:
                        writer.flush()
                        if not args.dry_run:
                            cur["done"] = sorted(done)
                            cur["failed"] = fail_detail
                            save_progress(prog_all)
                        el = time.time() - t0
                        log.info("进度 %d/%d (%.1f%%)  %.0f 只/分钟  ETA %.1f h",
                                 n_ok, len(todo), n_ok / len(todo) * 100,
                                 n_ok / el * 60, (len(todo) - n_ok) / (n_ok / el) / 3600)
            except Exception as e:  # noqa: BLE001
                n_fail += 1
                fail_detail[code] = str(e)[:200]
                log.error("失败 %s: %s", code, str(e)[:200])

    writer.flush()
    if not args.dry_run:
        cur["done"] = sorted(done)
        cur["failed"] = fail_detail
        save_progress(prog_all)
    log.info("完成: 成功 %d, 失败 %d, 限流次数 %d, 耗时 %.1f 分钟",
             n_ok, n_fail, LIMITER.n_throttle, (time.time() - t0) / 60)


def run_verify(args: argparse.Namespace) -> None:
    """抽样校验：湖内分钟日总量 vs kline_daily 日总量。"""
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"""SELECT code, CAST(datetime AS DATE) d, sum(vol) v, sum(amount) a
                FROM read_parquet('{(LAKE_1MIN / 'year=*').as_posix()}/*.parquet', union_by_name=true)
                WHERE datetime >= '{args.start}-01'
                GROUP BY 1,2""",
        ).fetchdf()
        daily = con.execute(
            f"""SELECT code, date d, vol v, amount a
                FROM read_parquet('{DAILY.as_posix()}')
                WHERE date >= '{args.start}-01'""",
        ).fetchdf()
    finally:
        con.close()
    m = rows.merge(daily, on=["code", "d"], suffixes=("_min", "_day"))
    m["vol_dev"] = (m["v_min"] - m["v_day"]).abs() / m["v_day"].replace(0, pd.NA)
    print(f"可比对 (code, date) 组合: {len(m):,}")
    print(m["vol_dev"].describe(percentiles=[0.5, 0.9, 0.99]).to_string())
    print("偏差 >1% 的比例: %.3f%%" % ((m["vol_dev"] > 0.01).mean() * 100))


def main() -> None:
    global LIMITER
    p = argparse.ArgumentParser(description="用 tushare 代理回填历史分钟 K 线")
    p.add_argument("--start", required=True, help="起始月 YYYY-MM")
    p.add_argument("--end", required=True, help="结束月 YYYY-MM")
    p.add_argument("--freq", default="1min", choices=["1min", "5min", "15min", "30min", "60min"])
    p.add_argument("--workers", type=int, default=MAX_WORKERS,
                   help=f"并发连接数。本代理硬上限 {MAX_WORKERS}，超出会被 429 拒绝，脚本自动钳制。")
    p.add_argument("--rate", type=float, default=240.0,
                   help="全局请求速率上限（次/分钟）。5 并发直连实测约 210，默认留余量。")
    p.add_argument("--limit", type=int, default=0, help="只处理前 N 只（冒烟测试）")
    p.add_argument("--verify-only", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="只拉取不落盘，用于冒烟测试")
    p.add_argument("--outdir", default=None, help="覆盖输出根目录（默认湖内 kline_1min）")
    args = p.parse_args()

    CALL_LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(CALL_LOG, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )
    # 确保 start/end 是完整日期；--end YYYY-MM 展开到该月最后一天
    if len(args.start) == 7:
        args.start = args.start + "-01"
    if len(args.end) == 7:
        y, m = (int(x) for x in args.end.split("-"))
        nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        args.end = (nxt - timedelta(days=1)).isoformat()
    if args.verify_only:
        run_verify(args)
    else:
        LIMITER = RateLimiter(args.rate)
        log.info("限速 %.0f 请求/分钟, 并发 %d", args.rate, args.workers)
        run_backfill(args)


if __name__ == "__main__":
    main()
