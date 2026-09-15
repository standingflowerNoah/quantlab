"""用 t.xiaodefa.top (tushare 代理) 回填 ETF 分钟 K 线缺口。

背景（2026-09-13 缺口盘点，广播 #51）
------------------------------------
湖内 `data/lake/clean/kline_1min_etf/`（按 code 平铺 part-{code}.parquet）
由 fsdb 供给、2025-01-02 起硬边界。缺口三层：
  1) 历史段：575 只老标的缺 2025-01 ~ 各自分钟首日（~3900 万根）
  2) 洞段：2026-01-21 ~ 03-16 33 个交易日仅剩 165-169 只/天（~420 万根）
  3) 近端：随流水线增量，本脚本不管

xiaodefa `stk_mins` 已实测直接支持 ETF（fund_mins 不存在）：
510300.SH 2012 上市首日、2022 年、159915 洞段均有完整 241 根/日。

与股票回填脚本（backfill_minute_xiaodefa.py）的差异
--------------------------------------------------
- 股票湖按 year 分区追加 part-xdNNNNN；ETF 湖按 code 平铺 part-{code}.parquet
  → 写入走「读现有 → anti-join(code,datetime) → concat → 原子覆盖写」幂等路径
  （safe-delete 拦删除，不删只覆盖）。
- 列名映射：xiaodefa vol → 湖 volume；dtype 对齐 BIGINT（单位换算在 --calibrate 实测）。
- 缺口扫描：分钟起始日 vs 全量日线上市日（勿用 is_etf 过滤，LOF/联接会被吞）+
  持有期内洞天检测。

用法
----
    # 单位校准（拉重叠日对比 fsdb 现有数据，定 vol 换算系数）
    python scripts/backfill_etf_minute_xiaodefa.py --calibrate

    # 扫描缺口（不拉取）
    python scripts/backfill_etf_minute_xiaodefa.py --scan-only

    # 试点（3 只，真写）
    python scripts/backfill_etf_minute_xiaodefa.py --limit 3

    # 全量后台
    python scripts/backfill_etf_minute_xiaodefa.py --workers 5 --rate 185
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
LAKE = ROOT / "data" / "lake" / "clean" / "kline_1min_etf"
ETFDAILY = ROOT / "data" / "lake" / "clean" / "etf_daily" / "year=*"
MIRROR = ROOT / "data" / "lake" / "clean" / "mirror" / "kline_daily.parquet"
PROGRESS = LAKE / "_etf_xd_progress.json"
CALL_LOG = ROOT / "logs" / "backfill_etf_minute_xiaodefa.log"

LAKE_START = date(2025, 1, 2)   # fsdb 硬边界（湖内理论最早日）
BOUNDARY_TOL = 7                # 上市日/起始日容差（天）
CHUNK_TDAY = 32                 # 32 交易日 ≈ 7712 行 < 8000 截断线
ROW_CAP = 8000
MAX_WORKERS = 5                 # 服务端硬并发上限，超出 429
REQ_TIMEOUT = 25.0
CONSECUTIVE_FAIL_ABORT = 30

VOL_SCALE = 1.0                 # xiaodefa vol → 湖 volume 换算系数（--calibrate 实测后固定）


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


TOKEN = _load_token()
URL = "https://t.xiaodefa.top/"

for _k in ("http_proxy", "https_proxy", "all_proxy",
           "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(_k, None)
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

log = logging.getLogger("etf_xd")


class FatalAuthError(RuntimeError):
    pass


class RateLimiter:
    """同股票脚本：时段进入等待前一次性预约，防活锁。"""

    def __init__(self, rate_per_min: float) -> None:
        self.interval = 60.0 / max(rate_per_min, 0.1)
        self.lock = threading.Lock()
        self.next_at = 0.0
        self.cooldown_until = 0.0
        self.n_throttle = 0

    def acquire(self) -> None:
        with self.lock:
            start = max(time.time(), self.next_at, self.cooldown_until)
            self.next_at = start + self.interval
        while True:
            delay = start - time.time()
            if delay > 0:
                time.sleep(min(delay, 5.0))
                continue
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


LIMITER = RateLimiter(185)


def api(api_name: str, params: dict, retry: int = 8) -> dict:
    body = json.dumps(
        {"api_name": api_name, "token": TOKEN, "params": params, "fields": ""}
    ).encode()
    last: Exception | None = None
    for a in range(retry):
        LIMITER.acquire()
        try:
            req = urllib.request.Request(
                URL, data=body,
                headers={"Content-Type": "application/json",
                         "Accept-Encoding": "gzip"})
            with _OPENER.open(req, timeout=REQ_TIMEOUT) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            d = json.loads(raw)
            if d.get("code") != 0:
                msg = str(d.get("msg"))
                if "必填参数" in msg or "接口名" in msg:
                    raise ValueError(f"{api_name} {msg}")
                if "过期" in msg or "无效" in msg or d.get("code") == 2002:
                    raise FatalAuthError(f"{api_name} code={d.get('code')} {msg}")
                if "过快" in msg or "不要超过" in msg:
                    LIMITER.penalize(min(3.0 * (2 ** a), 60.0))
                    last = RuntimeError(f"限流: {msg}")
                    continue
                # 「区间超限」可能是瞬时误报（裸调同参数可成功），交给外层
                # 重试；重试耗尽由 _fetch_span 捕获并二分降级。
                raise RuntimeError(f"{api_name} code={d.get('code')} msg={msg}")
            return d
        except ValueError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(min(2.0 * (a + 1), 15))
    raise last  # type: ignore[misc]


def to_ts_code(code: str) -> str:
    c = str(code).zfill(6)
    if c.startswith(("5", "6", "9")) and c[0] in ("5", "6", "9"):
        return f"{c}.SH"
    if c.startswith(("1", "2")):
        return f"{c}.SZ"
    raise ValueError(f"无法判断交易所: {c}")


def fetch_calendar(start: str, end: str) -> list[str]:
    d = api("trade_cal", {"exchange": "SSE", "start_date": start, "end_date": end})
    return sorted(x[1] for x in d["data"]["items"] if x[2] == 1)


def trading_chunks(days: list[str], size: int = CHUNK_TDAY) -> list[tuple[date, date]]:
    out = []
    for i in range(0, len(days), size):
        blk = days[i:i + size]
        out.append((datetime.strptime(blk[0], "%Y%m%d").date(),
                    datetime.strptime(blk[-1], "%Y%m%d").date()))
    return out


def _fetch_span(ts_code: str, s: date, e: date) -> list:
    """拉取 [s, e]（左闭右闭）。两种触顶都二分降级：
    ① 返回行数触顶 8000（静默截断）；② 服务端报「区间超限」且重试耗尽。"""
    try:
        d = api("stk_mins", {
            "ts_code": ts_code, "freq": "1min",
            "start_date": s.isoformat() + " 09:00:00",
            "end_date": e.isoformat() + " 15:35:00",
        })
    except RuntimeError as ex:
        if "区间超限" not in str(ex) or s >= e:
            raise
        mid = s + timedelta(days=max((e - s).days // 2, 0))
        log.warning("%s %s~%s 区间超限（重试耗尽），二分重取", ts_code, s, e)
        return (_fetch_span(ts_code, s, mid)
                + _fetch_span(ts_code, min(mid + timedelta(days=1), e), e))
    items = d["data"]["items"]
    if len(items) < ROW_CAP - 1:
        return items
    if s >= e:
        log.error("%s %s 单日即触顶（%d 行）", ts_code, s, len(items))
        return items
    mid = s + timedelta(days=max((e - s).days // 2, 0))
    log.warning("%s %s~%s 返回 %d 行触顶，二分重取", ts_code, s, e, len(items))
    return (_fetch_span(ts_code, s, mid)
            + _fetch_span(ts_code, min(mid + timedelta(days=1), e), e))


# --------------------------------------------------------------------------- #
# 缺口扫描
# --------------------------------------------------------------------------- #
def scan_gaps() -> dict[str, list[tuple[date, date]]]:
    """返回 {code: [(seg_s, seg_e), ...]}（交易日切段后的缺口区间）。"""
    con = duckdb.connect()
    con.execute("SET memory_limit='2GB'")
    con.execute("SET threads=4")
    try:
        per = con.execute(f"""
            SELECT code, min(CAST(datetime AS DATE)) m0, max(CAST(datetime AS DATE)) m1
            FROM read_parquet('{(LAKE / 'part-*.parquet').as_posix()}',
                              hive_partitioning=false)
            GROUP BY code
        """).fetchdf()
        dl = con.execute(f"""
            SELECT code, min(CAST(date AS DATE)) d0
            FROM read_parquet('{ETFDAILY.as_posix()}/*.parquet',
                              hive_partitioning=false)
            GROUP BY code
        """).fetchdf()
        cal = con.execute(f"""
            SELECT DISTINCT CAST(date AS DATE) d
            FROM read_parquet('{MIRROR.as_posix()}')
            WHERE CAST(date AS DATE) >= DATE '{LAKE_START.isoformat()}'
        """).fetchdf()
    finally:
        con.close()
    cal_days = sorted(pd.to_datetime(cal["d"]).dt.date)
    dl_map = dict(zip(dl["code"], pd.to_datetime(dl["d0"]).dt.date))

    # 每只 ETF 的已有分钟日集合（用于洞检测）
    con = duckdb.connect()
    con.execute("SET memory_limit='2GB'")
    con.execute("SET threads=4")
    try:
        have = con.execute(f"""
            SELECT DISTINCT code, CAST(datetime AS DATE) d
            FROM read_parquet('{(LAKE / 'part-*.parquet').as_posix()}',
                              hive_partitioning=false)
        """).fetchdf()
    finally:
        con.close()
    have_map: dict[str, set] = {}
    for r in have.itertuples():
        have_map.setdefault(r.code, set()).add(r.d.date())

    cal_set = set(cal_days)
    gaps: dict[str, list[tuple[date, date]]] = {}
    for r in per.itertuples():
        code = str(r.code).zfill(6)
        m0 = r.m0.date() if hasattr(r.m0, "date") else r.m0
        m1 = r.m1.date() if hasattr(r.m1, "date") else r.m1
        d0 = dl_map.get(code)
        # 缺口日集合 = 应有交易日 - 已有
        start_eff = max(d0, LAKE_START) if d0 else LAKE_START
        if start_eff > m1:
            continue
        want = [d for d in cal_days if start_eff <= d <= m1]
        if not want:
            continue
        miss = [d for d in want if d not in have_map.get(code, set())]
        # 起始日缺口（want 全缺且首日 < m0 - 容差）已含在 miss 里
        if not miss:
            continue
        # 连续缺口日合并为区间
        runs: list[tuple[date, date]] = []
        for d in miss:
            if runs and (d - runs[-1][1]).days <= 5:
                runs[-1] = (runs[-1][0], d)
            else:
                runs.append((d, d))
        gaps[code] = runs
    return gaps


def seg_gaps(gaps: dict[str, list[tuple[date, date]]]) -> dict[str, list[tuple[date, date]]]:
    """把缺口区间按交易日重切段（≤CHUNK_TDAY）。"""
    con = duckdb.connect()
    try:
        d = con.execute(f"""
            SELECT DISTINCT CAST(date AS DATE) d
            FROM read_parquet('{MIRROR.as_posix()}')
            WHERE CAST(date AS DATE) >= DATE '{LAKE_START.isoformat()}'
        """).fetchall()
    finally:
        con.close()
    cal = sorted(x[0].date() if hasattr(x[0], "date") else x[0] for x in d)
    cal_set = set(cal)
    out: dict[str, list[tuple[date, date]]] = {}
    n_seg = 0
    for code, runs in gaps.items():
        segs: list[tuple[date, date]] = []
        for s, e in runs:
            # 每个 run 独立切段：跨 run 拼接会让段的自然区间夹住已有数据
            # 的时段，拉回后全部被 anti-join 丢弃（实测浪费可达 55%）。
            days = [x for x in cal if s <= x <= e]
            for i in range(0, len(days), CHUNK_TDAY):
                blk = days[i:i + CHUNK_TDAY]
                segs.append((blk[0], blk[-1]))
        if segs:
            out[code] = segs
            n_seg += len(segs)
    log.info("缺口标的 %d 只，共 %d 段", len(out), n_seg)
    return out


# --------------------------------------------------------------------------- #
# 写入层（按 code 平铺，anti-join + 原子覆盖）
# --------------------------------------------------------------------------- #
_wlock = threading.Lock()
STATS = {"codes": 0, "rows_new": 0, "rows_dup": 0}


def merge_write(code: str, new: pd.DataFrame) -> tuple[int, int]:
    """新数据 anti-join 已有 (code,datetime) 后合并覆盖写。返回 (新增, 重复)。"""
    path = LAKE / f"part-{code}.parquet"
    cols = ["code", "datetime", "open", "high", "low", "close", "volume", "amount"]
    new = new[["code", "datetime", "open", "high", "low", "close",
               "vol", "amount"]].rename(columns={"vol": "volume"})
    new["volume"] = (new["volume"] * VOL_SCALE).round().astype("int64")
    new["amount"] = new["amount"].round().astype("int64")
    new["datetime"] = pd.to_datetime(new["datetime"])
    new = new[cols].drop_duplicates(subset=["code", "datetime"])
    if path.exists():
        old = pd.read_parquet(path)
        old["datetime"] = pd.to_datetime(old["datetime"])
        key_old = set(zip(old["code"], old["datetime"]))
        mask = pd.Series([(c, t) not in key_old
                          for c, t in zip(new["code"], new["datetime"])],
                         index=new.index)
        n_dup = int((~mask).sum())
        new = new[mask]
        merged = (pd.concat([old[cols], new], ignore_index=True)
                  .sort_values("datetime", kind="mergesort")
                  .reset_index(drop=True))
    else:
        n_dup = 0
        merged = new.sort_values("datetime").reset_index(drop=True)
    tmp = path.with_suffix(".tmp.parquet")
    merged.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(path)
    return len(new), n_dup


def pull_one(code: str, segs: list[tuple[date, date]], dry: bool) -> int:
    ts = to_ts_code(code)
    frames = []
    for s, e in segs:
        items = _fetch_span(ts, s, e)
        if items:
            frames.append(pd.DataFrame(
                items,
                columns=["ts_code", "trade_time", "open", "close", "high",
                         "low", "vol", "amount"]))
    if not frames:
        return 0
    df = pd.concat(frames, ignore_index=True)
    df["datetime"] = pd.to_datetime(df["trade_time"]).astype("datetime64[ns]")
    df["code"] = code
    df["vol"] = pd.to_numeric(df["vol"], errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.drop_duplicates(subset=["code", "datetime"]).sort_values("datetime")
    if dry:
        return len(df)
    n_new, n_dup = merge_write(code, df)
    with _wlock:
        STATS["codes"] += 1
        STATS["rows_new"] += n_new
        STATS["rows_dup"] += n_dup
    return n_new


# --------------------------------------------------------------------------- #
# 校准
# --------------------------------------------------------------------------- #
def calibrate() -> None:
    """拉 510300 一个重叠日（2025-01-02，fsdb 与 xiaodefa 都有），定 vol 换算系数。"""
    items = _fetch_span("510300.SH", date(2025, 1, 2), date(2025, 1, 2))
    nd = pd.DataFrame(items, columns=["ts_code", "trade_time", "open", "close",
                                      "high", "low", "vol", "amount"])
    con = duckdb.connect()
    try:
        fd = con.execute(f"""
            SELECT datetime, volume, amount FROM
            read_parquet('{(LAKE / 'part-510300.parquet').as_posix()}',
                         hive_partitioning=false)
            WHERE CAST(datetime AS DATE) = DATE '2025-01-02'
        """).fetchdf()
    finally:
        con.close()
    nd["datetime"] = pd.to_datetime(nd["trade_time"])
    m = nd.merge(fd, on="datetime", how="inner")
    print(f"重叠行数 {len(m)}（xiaodefa {len(nd)} / fsdb {len(fd)}）")
    m["ratio_v"] = m["volume"] / pd.to_numeric(m["vol"])
    m["ratio_a"] = m["amount_x"] / pd.to_numeric(m["amount_y"]).replace(0, pd.NA)
    print("volume/vol 比值分布:", m["ratio_v"].describe(
        percentiles=[.5, .9]).to_dict())
    print("amount 比值分布:", m["ratio_a"].describe(percentiles=[.5]).to_dict())
    print(m[["datetime", "vol", "volume", "ratio_v", "amount_x", "amount_y"]]
          .head(5).to_string(index=False))


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def load_progress() -> dict:
    if PROGRESS.exists():
        return json.loads(PROGRESS.read_text(encoding="utf-8"))
    return {"done": [], "failed": {}}


def save_progress(p: dict) -> None:
    tmp = PROGRESS.with_suffix(".tmp")
    tmp.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(PROGRESS)


def main() -> None:
    global LIMITER, REQ_TIMEOUT
    p = argparse.ArgumentParser()
    p.add_argument("--workers", type=int, default=MAX_WORKERS)
    p.add_argument("--rate", type=float, default=185.0)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--codes", help="逗号分隔指定标的")
    p.add_argument("--scan-only", action="store_true")
    p.add_argument("--calibrate", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--timeout", type=float, default=REQ_TIMEOUT)
    a = p.parse_args()
    REQ_TIMEOUT = a.timeout

    CALL_LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(CALL_LOG, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)])

    if a.calibrate:
        calibrate()
        return

    if not TOKEN:
        log.error("未找到 xiaodefa token")
        raise SystemExit(1)

    gaps = scan_gaps()
    if a.scan_only:
        n_runs = sum(len(v) for v in gaps.values())
        n_span = sum((e - s).days + 1 for v in gaps.values() for s, e in v)
        log.info("缺口标的 %d 只 / 缺口区间 %d 个 / 自然日总跨度 %d",
                 len(gaps), n_runs, n_span)
        return
    segs_map = seg_gaps(gaps)

    if a.codes:
        keep = {c.strip().zfill(6) for c in a.codes.split(",")}
        segs_map = {k: v for k, v in segs_map.items() if k in keep}
    if a.limit:
        segs_map = dict(list(segs_map.items())[: a.limit])

    prog = load_progress()
    done = set(prog.get("done", []))
    todo = {c: s for c, s in segs_map.items() if c not in done}
    log.info("待处理 %d 只（已完成 %d）", len(todo), len(done))
    if not todo:
        log.info("无缺口待补，退出")
        return

    n_workers = min(a.workers, MAX_WORKERS)
    LIMITER = RateLimiter(a.rate)
    t0 = time.time()
    n_ok = n_fail = 0
    fail_detail: dict[str, str] = {}
    consec_fail = 0
    prog["failed"] = fail_detail

    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        futs = {ex.submit(pull_one, c, s, a.dry_run): c for c, s in todo.items()}
        for fut in as_completed(list(futs)):
            code = futs.pop(fut, None)
            try:
                n = fut.result()
                done.add(code)
                n_ok += 1
                consec_fail = 0
                if n_ok % 25 == 0:
                    with _wlock:
                        st = dict(STATS)
                    prog["done"] = sorted(done)
                    save_progress(prog)
                    el = time.time() - t0
                    log.info("进度 %d/%d (%.1f%%)  %.1f 只/分  新增 %s 行  限流 %d  ETA %.1f h",
                             n_ok, len(todo), n_ok / max(len(todo), 1) * 100,
                             n_ok / el * 60, f"{st['rows_new']:,}",
                             LIMITER.n_throttle,
                             (len(todo) - n_ok) / max(n_ok / el, 1e-9) / 3600)
            except FatalAuthError as e:
                log.error("🛑 token 失效，立即中止: %s", e)
                for f in futs:
                    f.cancel()
                prog["done"] = sorted(done)
                save_progress(prog)
                raise SystemExit(2) from None
            except Exception as e:  # noqa: BLE001
                n_fail += 1
                consec_fail += 1
                fail_detail[code] = str(e)[:200]
                log.error("失败 %s: %s", code, str(e)[:200])
                if consec_fail >= CONSECUTIVE_FAIL_ABORT:
                    log.error("🛑 连续 %d 只失败（疑似 token 过期/源端停摆），中止", consec_fail)
                    for f in futs:
                        f.cancel()
                    prog["done"] = sorted(done)
                    save_progress(prog)
                    raise SystemExit(3) from None

    prog["done"] = sorted(done)
    prog["failed"] = fail_detail
    save_progress(prog)
    log.info("完成: 成功 %d 失败 %d 新增 %s 行 重复跳过 %s 耗时 %.1f 分钟",
             n_ok, n_fail, f"{STATS['rows_new']:,}",
             f"{STATS['rows_dup']:,}", (time.time() - t0) / 60)
    if n_fail:
        raise SystemExit(4)


if __name__ == "__main__":
    main()
