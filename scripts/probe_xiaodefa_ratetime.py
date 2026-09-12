"""定时窗测量 t.xiaodefa.top 的可持续请求速率（真实 payload：单月 1min ~5000 行）。

每个并发档位跑固定秒数，统计成功请求数 → req/min，以及限流/网络错误次数。

用法:
    python scripts/probe_xiaodefa_ratetime.py [并发] [秒数]
    默认: python scripts/probe_xiaodefa_ratetime.py 4 120 8 120
"""
import gzip
import json
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pathlib as _pl
def _load_token():
    t = os.environ.get("XIAODEFA_TOKEN", "").strip()
    if t: return t
    for p in (_pl.Path(__file__).resolve().parent.parent / ".secrets" / "xiaodefa_token",
              _pl.Path.home() / ".workbuddy" / "xiaodefa_token"):
        if p.exists():
            t = p.read_text(encoding="utf-8").strip()
            if t: return t
    return ""
TOKEN = _load_token()
URL = "https://t.xiaodefa.top/"

# 本机全局 HTTP 代理隧道（env https_proxy=127.0.0.1:57092）会 502 并抬高延迟，必须绕开
import os as _os
for _k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    _os.environ.pop(_k, None)
_os.environ["no_proxy"] = "*"
_os.environ["NO_PROXY"] = "*"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

MONTHS = [(date(y, m, 1), (date(y, m + 1, 1) if m < 12 else date(y + 1, 1, 1)))
          for y in (2022, 2023, 2024) for m in range(1, 13)]


def call(params, stats, stop_at):
    """返回 (ok, msg)。限流即返回，不重试，以便如实统计被拒比例。"""
    if time.time() > stop_at:
        return None
    body = json.dumps({"api_name": "stk_mins", "token": TOKEN, "params": params, "fields": ""}).encode()
    try:
        req = urllib.request.Request(
            URL, data=body,
            headers={"Content-Type": "application/json", "Accept-Encoding": "gzip"},
        )
        with _OPENER.open(req, timeout=90) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        d = json.loads(raw)
        if d.get("code") != 0:
            msg = str(d.get("msg"))
            if "过快" in msg:
                stats["throttle"] += 1
            else:
                stats["other_err"] += 1
            return None
        stats["ok"] += 1
        stats["rows"] += len(d["data"]["items"])
        return True
    except Exception:
        stats["net_err"] += 1
        return None


def worker(codes, stats, stop_at, lock_stop):
    while time.time() < stop_at:
        with lock_stop:
            if not codes:
                return
            code = codes.pop()
        for s, e in MONTHS:
            if time.time() > stop_at:
                return
            call({"ts_code": code, "freq": "1min",
                  "start_date": s.isoformat() + " 09:00:00",
                  "end_date": e.isoformat() + " 15:05:00"}, stats, stop_at)


def run(workers: int, seconds: int, start_id: int):
    stats = dict(ok=0, throttle=0, other_err=0, net_err=0, rows=0)
    codes = [f"{600000 + i:06d}.SH" for i in range(start_id, start_id + workers * 20)]
    stop_at = time.time() + seconds
    lock_stop = threading.Lock()
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for _ in range(workers):
            ex.submit(worker, codes, stats, stop_at, lock_stop)
    el = time.time() - t0
    return stats, el


def main():
    argv = sys.argv[1:]
    tiers = [(int(argv[i]), int(argv[i + 1])) for i in range(0, len(argv) - 1, 2)] or [(4, 120), (8, 120)]
    print(f"{'并发':>4} {'秒':>5} {'成功':>6} {'req/min':>9} {'行/s':>9} {'限流':>6} {'网络错':>7} {'其他错':>7}")
    sid = 0
    for w, sec in tiers:
        stats, el = run(w, sec, sid)
        sid += w * 20
        print(f"{w:>4} {el:>5.0f} {stats['ok']:>6} {stats['ok']/el*60:>9.0f} "
              f"{stats['rows']/el:>9,.0f} {stats['throttle']:>6} {stats['net_err']:>7} {stats['other_err']:>7}")
        time.sleep(5)


if __name__ == "__main__":
    main()
