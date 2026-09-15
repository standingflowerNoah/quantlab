"""拉取 tushare `stk_high_shock`（个股严重异常波动，交易所官方披露）到本地湖。

接口（https://tushare.pro/document/2?doc_id=452）
------------------------------------------------
- 字段：ts_code / trade_date(公告日期) / name / trade_market(交易所板块) /
        reason(异常说明) / period(异常期间，如 '2026030320260317')
- 单次上限 1000 条；区间查询（start_date+end_date）可用，单日 trade_date
  参数在该代理上不可靠（实测返回 0 行）→ 统一用区间查询

代理数据边界（2026-09-13 实测）
------------------------------
- xiaodefa 代理该接口**仅有 2026-02-09 起的数据**：2020-2025 全年/区间查询
  均 0 行；按 ts_code 反查同样只有 2026 记录
- 即：历史（2022-2025）严重异常波动清单不可从该代理回补，缺口已声明；
  本脚本负责把可用窗口拉全并支持日常增量

落盘
----
- `data/lake/clean/stk_high_shock/part-{year}.parquet`
- 列：date(公告日) / code / name / trade_market / reason /
       period_start / period_end（period 拆分）/ period_raw
- 幂等：重跑覆盖写（safe-delete 环境不删文件只覆盖）

用法
----
    python scripts/backfill_stk_high_shock.py                    # 全量 2026 至今
    python scripts/backfill_stk_high_shock.py --start 20260901   # 近端增量
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
OUT_DIR = ROOT / "data" / "lake" / "clean" / "stk_high_shock"
LOG_FILE = ROOT / "logs" / "backfill_stk_high_shock.log"
URL = "https://t.xiaodefa.top/"
CHUNK_DAYS = 180          # 半年一段，远小于 1000 行上限（数据极稀疏）
REQ_TIMEOUT = 30.0


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


def api(params: dict, retry: int = 6) -> dict:
    body = json.dumps(
        {"api_name": "stk_high_shock", "token": TOKEN, "params": params, "fields": ""}
    ).encode()
    last: Exception | None = None
    for a in range(retry):
        try:
            req = urllib.request.Request(
                URL, data=body,
                headers={"Content-Type": "application/json", "Accept-Encoding": "gzip"})
            with _OPENER.open(req, timeout=REQ_TIMEOUT) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            d = json.loads(raw)
            if d.get("code") != 0:
                msg = str(d.get("msg"))
                if "过期" in msg or "无效" in msg or d.get("code") == 2002:
                    raise FatalAuthError(msg)
                raise RuntimeError(msg)
            return d
        except FatalAuthError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(min(2.0 * (2 ** a), 30.0))
    raise RuntimeError(f"stk_high_shock 重试{retry}次仍失败: {last}")


def fetch_range(s: str, e: str) -> pd.DataFrame:
    d = api({"start_date": s, "end_date": e})
    data = d.get("data") or {}
    items = data.get("items") or []
    cols = data.get("fields") or []
    if not items:
        return pd.DataFrame()
    df = pd.DataFrame(items, columns=cols)
    # period '2026030320260317' → start/end
    pr = df["period"].astype(str)
    df["period_start"] = pd.to_datetime(
        pr.str[:8], format="%Y%m%d", errors="coerce")
    df["period_end"] = pd.to_datetime(
        pr.str[8:16], format="%Y%m%d", errors="coerce")
    df["period_raw"] = df["period"]
    df["code"] = df["ts_code"].str.split(".").str[0]
    df["date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d", errors="coerce")
    keep = ["date", "code", "name", "trade_market", "reason",
            "period_start", "period_end", "period_raw"]
    return df[keep].sort_values(["date", "code"]).reset_index(drop=True)


def flush_year(buf: dict[int, pd.DataFrame]) -> None:
    for y, df in list(buf.items()):
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        f = OUT_DIR / f"part-{y}.parquet"
        if f.exists():
            old = pd.read_parquet(f)
            df = (pd.concat([old, df], ignore_index=True)
                    .drop_duplicates(subset=["date", "code", "reason", "period_raw"],
                                     keep="last")
                    .sort_values(["date", "code"]))
        df.to_parquet(f, index=False)
        logging.info("写 %s: %d 行", f.name, len(df))
        del buf[y]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20260101")
    ap.add_argument("--end", default=None)
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
                  logging.StreamHandler()])
    s = date.fromisoformat(args.start)
    e = date.fromisoformat(args.end) if args.end else date.today()
    if not TOKEN:
        raise SystemExit("token 未配置（XIAODEFA_TOKEN / .secrets / ~/.workbuddy）")

    buf: dict[int, pd.DataFrame] = {}
    n_total = 0
    cur = s
    while cur <= e:
        seg_end = min(date.fromordinal(
            min(cur.toordinal() + CHUNK_DAYS, e.toordinal() + 1) - 1), e)
        df = fetch_range(cur.strftime("%Y%m%d"), seg_end.strftime("%Y%m%d"))
        logging.info("%s ~ %s: %d 行", cur, seg_end, len(df))
        if len(df) >= 1000:
            logging.warning("触顶 1000 行（罕见，数据稀疏），请减小 CHUNK_DAYS")
        if not df.empty:
            y = int(cur.strftime("%Y"))
            if seg_end.year != y:
                # 跨年段按实际年份拆行
                for yy, g in df.groupby(df["date"].dt.year):
                    buf.setdefault(int(yy), pd.DataFrame())
                    buf[int(yy)] = pd.concat([buf[int(yy)], g], ignore_index=True)
            else:
                if y not in buf:
                    buf[y] = df
                else:
                    buf[y] = pd.concat([buf[y], df], ignore_index=True)
            n_total += len(df)
        cur = date.fromordinal(seg_end.toordinal() + 1)
        time.sleep(1.2)
    flush_year(buf)
    logging.info("完成：本次 %d 行", n_total)

    # 汇总
    import duckdb
    files = sorted(OUT_DIR.glob("part-*.parquet"))
    if files:
        con = duckdb.connect()
        fs = ", ".join(f"'{f.as_posix()}'" for f in files)
        print(con.execute(f"""
            select year(date) y, count(*) n, count(distinct code) nc,
                   min(date) mn, max(date) mx
            from read_parquet([{fs}], hive_partitioning=false)
            group by 1 order by 1
        """).fetchdf().to_string())
        print("\n=== reason 分布 top10 ===")
        print(con.execute(f"""
            select reason, count(*) n from read_parquet([{fs}], hive_partitioning=false)
            group by 1 order by n desc limit 10
        """).fetchdf().to_string())
        con.close()


if __name__ == "__main__":
    main()
