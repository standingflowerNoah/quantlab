"""用 t.xiaodefa.top (tushare 代理) 回补全市场个股日频分单型资金流向。

背景
----
主库 fund_flow_daily 仅 31 只票（东财 push2his 只给近 120 日，历史回补不可行）。
tushare 标准 `moneyflow` 接口支持**按交易日全市场**一次返回（~5500 行/日），
历史深度实测 2020-01-02 起有效，单位：金额=万元、量=手。

字段口径（tushare moneyflow → 本表列名）
--------------------------------------
- elg = 特大单, lg = 大单, md = 中单, sm = 小单
- super_net = buy_elg_amount - sell_elg_amount （万元，净流入为正）
- large_net / mid_net / small_net 同理
- main_net   = super_net + large_net （"主力"）
- net_mf_amount = 全单型合计净流入（tushare 原生字段，万元）

工程要点（复用 backfill_minute_xiaodefa.py 实测结论）
----------------------------------------------------
- 绕本机代理直连（ProxyHandler({})）；token 从 env/.secrets/home 三级加载
- 服务端硬并发上限 5 连接；按日查询单请求即可，串行 + 令牌桶 185/min 足够
- 幂等：进度文件 _mf_progress.json 记录已完成日，重跑跳过；parquet 同名覆盖写
- 不碰主库写锁：只写湖 data/lake/clean/moneyflow/part-{year}.parquet

用法
----
    python scripts/backfill_moneyflow_xiaodefa.py --start 2022-01-01 --end 2026-09-11
    python scripts/backfill_moneyflow_xiaodefa.py --start 2026-09-11 --end 2026-09-11   # 增量
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import threading
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "lake" / "clean" / "moneyflow"
PROGRESS = OUT_DIR / "_mf_progress.json"
LOG_FILE = ROOT / "logs" / "backfill_moneyflow_xiaodefa.log"

URL = "https://t.xiaodefa.top/"


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


class FatalAuthError(RuntimeError):
    pass


class RateLimiter:
    def __init__(self, rate_per_min: float) -> None:
        self.interval = 60.0 / max(rate_per_min, 0.1)
        self.lock = threading.Lock()
        self.next_at = 0.0

    def acquire(self) -> None:
        with self.lock:
            now = time.time()
            start = max(now, self.next_at)
            self.next_at = start + self.interval
        while True:
            delay = start - time.time()
            if delay > 0:
                time.sleep(min(delay, 5.0))
                continue
            return


LIMITER = RateLimiter(120.0)   # 按日查询很轻，120/min 保守值


def api(api_name: str, params: dict, retry: int = 8, timeout: float = 30.0) -> dict:
    body = json.dumps(
        {"api_name": api_name, "token": TOKEN, "params": params, "fields": ""}
    ).encode()
    last: Exception | None = None
    for a in range(retry):
        LIMITER.acquire()
        try:
            req = urllib.request.Request(
                URL, data=body,
                headers={"Content-Type": "application/json", "Accept-Encoding": "gzip"},
            )
            with _OPENER.open(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            d = json.loads(raw)
            if d.get("code") != 0:
                msg = str(d.get("msg"))
                if "过期" in msg or "无效" in msg or d.get("code") == 2002:
                    raise FatalAuthError(f"{api_name} code={d.get('code')} {msg}")
                if "过快" in msg or "不要超过" in msg:
                    time.sleep(min(3.0 * (2 ** a), 60.0))
                    last = RuntimeError(f"限流: {msg}")
                    continue
                raise RuntimeError(f"{api_name} code={d.get('code')} msg={msg}")
            return d
        except FatalAuthError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(min(2.0 * (2 ** a), 30.0))
    raise RuntimeError(f"{api_name} 重试{retry}次仍失败: {last}")


FIELDS = ["trade_date", "ts_code", "buy_elg_amount", "sell_elg_amount",
          "buy_lg_amount", "sell_lg_amount", "buy_md_amount", "sell_md_amount",
          "buy_sm_amount", "sell_sm_amount", "net_mf_amount"]


def fetch_day(td: str) -> pd.DataFrame:
    d = api("moneyflow", {"trade_date": td})
    data = d.get("data") or {}
    items = data.get("items") or []
    cols = data.get("fields") or []
    if not items:
        return pd.DataFrame()
    df = pd.DataFrame(items, columns=cols)
    for c in df.columns:
        if c not in ("trade_date", "ts_code"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    # ts_code 600519.SH -> code 600519；北交所 .BJ 同步支持
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
    return df[keep].sort_values("code").reset_index(drop=True)


def load_progress() -> set:
    if PROGRESS.exists():
        try:
            return set(json.loads(PROGRESS.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            return set()
    return set()


def save_progress(done: set) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PROGRESS.write_text(json.dumps(sorted(done)), encoding="utf-8")


def trading_days(start: date, end: date) -> list[str]:
    con = duckdb.connect(str(ROOT / "data" / "quant.duckdb"), read_only=True)
    try:
        df = con.execute(
            "select distinct date from kline_daily where date between ? and ? order by 1",
            [start, end],
        ).fetchdf()
    finally:
        con.close()
    return [d.strftime("%Y%m%d") for d in pd.to_datetime(df["date"])]


def flush_year(buf: dict[int, pd.DataFrame]) -> None:
    for y, df in list(buf.items()):
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        f = OUT_DIR / f"part-{y}.parquet"
        if f.exists():
            old = pd.read_parquet(f)
            df = (pd.concat([old, df], ignore_index=True)
                    .drop_duplicates(subset=["date", "code"], keep="last")
                    .sort_values(["date", "code"]))
        df.to_parquet(f, index=False)
        logging.info("写 %s: %d 行", f.name, len(df))
        del buf[y]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--workers", type=int, default=5,
                    help="并发连接数，代理硬上限 5")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
                  logging.StreamHandler()],
    )
    s = date.fromisoformat(args.start)
    e = date.fromisoformat(args.end) if args.end else date.today()
    days = trading_days(s, e)
    done = load_progress()
    todo = [d for d in days if d not in done]
    logging.info("交易日 %d，已完成 %d，待抓 %d", len(days), len(done), len(todo))
    if not todo:
        logging.info("无待抓取，退出")
        return

    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: dict[str, pd.DataFrame | None] = {}
    n_fail = 0
    abort = threading.Event()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_day, td): td for td in todo}
        for fut in as_completed(futs):
            td = futs[fut]
            try:
                df = fut.result()
                results[td] = df
            except FatalAuthError as e:
                logging.error("token 失效，中止: %s", e)
                abort.set()
                for f in futs:
                    f.cancel()
                break
            except Exception as e:  # noqa: BLE001
                n_fail += 1
                results[td] = None
                logging.error("%s 失败(%d): %s", td, n_fail, e)
                if n_fail >= 15:
                    logging.error("累计失败过多，中止")
                    abort.set()
                    for f in futs:
                        f.cancel()
                    break
    logging.info("抓取结束：成功 %d / 失败 %d",
                 sum(1 for v in results.values() if v is not None),
                 sum(1 for v in results.values() if v is None))

    buf: dict[int, pd.DataFrame] = {}
    for td in todo:
        df = results.get(td)
        if df is None or df.empty:
            if df is not None and df.empty:
                logging.warning("%s 返回空（非交易日？）记完成", td)
                done.add(td)
            continue
        done.add(td)
        y = int(td[:4])
        if y not in buf:
            buf[y] = df
        else:
            buf[y] = pd.concat([buf[y], df], ignore_index=True)
    flush_year(buf)
    save_progress(done)
    logging.info("完成：累计 %d 天", len(done))


if __name__ == "__main__":
    main()
