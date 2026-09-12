"""探测 t.xiaodefa.top 在真实 payload（自然月 1min，~5000 行）下的可持续速率。

结论决定回填工期。用法:
    python scripts/probe_xiaodefa_rate.py [并发数 ...]
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

MONTHS = [(date(y, m, 1), (date(y, m + 1, 1) if m < 12 else date(y + 1, 1, 1)))
          for y in (2022, 2023, 2024) for m in range(1, 13)]


class Meter:
    def __init__(self):
        self.lock = threading.Lock()
        self.req = 0
        self.throttled = 0
        self.net_err = 0
        self.rows = 0

    def hit(self, kind, rows=0):
        with self.lock:
            self.req += 1
            self.rows += rows
            if kind == "throttle":
                self.throttled += 1
            elif kind == "net":
                self.net_err += 1


def call(meter: Meter, params: dict, retry: int = 4) -> list:
    body = json.dumps({"api_name": "stk_mins", "token": TOKEN, "params": params, "fields": ""}).encode()
    for a in range(retry):
        try:
            req = urllib.request.Request(
                URL, data=body,
                headers={"Content-Type": "application/json", "Accept-Encoding": "gzip"},
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            d = json.loads(raw)
            if d.get("code") != 0:
                msg = str(d.get("msg"))
                if "过快" in msg:
                    meter.hit("throttle")
                    time.sleep(1.0 * (a + 1))
                    continue
                raise RuntimeError(msg)
            items = d["data"]["items"]
            meter.hit("ok", len(items))
            return items
        except RuntimeError:
            raise
        except Exception:
            meter.hit("net")
            time.sleep(1.5 * (a + 1))
    return []


def run(workers: int, n_stocks: int, meter: Meter) -> float:
    codes = [f"{600000 + i:06d}.SH" for i in range(n_stocks)]

    def stock(code):
        for s, e in MONTHS:
            call(meter, {
                "ts_code": code, "freq": "1min",
                "start_date": s.isoformat() + " 09:00:00",
                "end_date": e.isoformat() + " 15:05:00",
            })

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(stock, codes))
    return time.time() - t0


def main():
    print(f"{'并发':>4} {'请求':>6} {'耗时s':>7} {'req/min':>9} {'限流':>6} {'网络错':>7} {'行数':>10} {'行/s':>9}")
    for w in [int(x) for x in (sys.argv[1:] or ["2", "4", "6", "8"])]:
        m = Meter()
        el = run(w, w * 12, m)  # 每 worker 12 只票
        print(f"{w:>4} {m.req:>6} {el:>7.0f} {m.req / el * 60:>9.0f} "
              f"{m.throttled:>6} {m.net_err:>7} {m.rows:>10,} {m.rows / el:>9,.0f}")
        time.sleep(5)


if __name__ == "__main__":
    main()
