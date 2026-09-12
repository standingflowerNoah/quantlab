"""探测 t.xiaodefa.top (tushare 代理) 分钟线能力：深度 / 限流 / 缺口 / 口径。

用法:
    python scripts/probe_xiaodefa_mins.py coverage    # 缺口普查
    python scripts/probe_xiaodefa_mins.py throughput  # 并发吞吐
"""
import gzip
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

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


def call(api, params, retry=6):
    """带重试的调用。注意：本机有 HTTP 代理，偶发 'Tunnel connection failed: 502'
    属代理抖动而非接口故障，必须重试而非判失败。"""
    body = json.dumps({"api_name": api, "token": TOKEN, "params": params, "fields": ""}).encode()
    last = None
    for a in range(retry):
        try:
            req = urllib.request.Request(
                URL, data=body,
                headers={"Content-Type": "application/json", "Accept-Encoding": "gzip"},
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
                enc = r.headers.get("Content-Encoding")
            if enc == "gzip":
                raw = gzip.decompress(raw)
            d = json.loads(raw)
            if d.get("code") in (429, 502, 503):
                raise RuntimeError(f"throttled code={d.get('code')} {d.get('msg')}")
            return d
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(min(2.0 * (a + 1), 12))
    raise last


def trading_days(start, end):
    d = call("trade_cal", {"exchange": "SSE", "start_date": start, "end_date": end})
    return [x[1] for x in d["data"]["items"] if x[2] == 1]


def pull_stock(code, days, chunk=30):
    segs = [days[i:i + chunk] for i in range(0, len(days), chunk)]
    got, rows, failed = set(), 0, 0
    for s in segs:
        try:
            r = call("stk_mins", {
                "ts_code": code, "freq": "1min",
                "start_date": s[0] + " 09:00:00", "end_date": s[-1] + " 15:05:00",
            })
        except Exception:  # noqa: BLE001
            failed += 1
            continue
        if r.get("code") != 0:
            failed += 1
            continue
        for x in r["data"]["items"]:
            got.add(x[1][:10])
            rows += 1
    return code, got, rows, failed, len(segs)


def cmd_coverage():
    days = trading_days("20220101", "20241231")
    codes = sys.argv[2].split(",") if len(sys.argv) > 2 else [
        "600519.SH", "000001.SZ", "300750.SZ", "002594.SZ",
        "688981.SH", "920819.BJ", "601398.SH", "000651.SZ",
    ]
    allday = set(days)
    t0 = time.time()
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(lambda c: pull_stock(c, days), codes))
    el = time.time() - t0
    print(f"\n{len(codes)} 只票并发(4)拉取 {len(days)} 个交易日，耗时 {el:.0f}s")
    for code, got, rows, failed, nseg in res:
        miss = sorted(allday - got)
        print(f"\n{code}: 覆盖 {len(got)}/{len(days)} 天 ({len(got)/len(days):.1%}), "
              f"{rows} 行, 缺失 {len(miss)} 天, 日均 {rows/max(len(got),1):.1f} 根, 失败段 {failed}/{nseg}")
        if miss:
            print("   缺失:", ",".join(miss[:20]), "..." if len(miss) > 20 else "")


def cmd_throughput():
    days = trading_days("20240101", "20241231")
    segs = [days[i:i + 30] for i in range(0, len(days), 30)]
    codes = [f"{600000+i:06d}.SH" for i in range(60)]
    t0 = time.time()
    n = 0
    errs = {}
    with ThreadPoolExecutor(16) as ex:
        for code, got, rows in ex.map(lambda c: pull_stock(c, days, 30), codes):
            n += 1
    el = time.time() - t0
    print(f"16 并发 {len(codes)} 只票 x {len(segs)} 段 = {len(codes)*len(segs)} 请求, "
          f"耗时 {el:.0f}s -> {len(codes)*len(segs)/el*60:.0f} req/min")
    print("errors:", errs if errs else "无")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "coverage"
    {"coverage": cmd_coverage, "throughput": cmd_throughput}[cmd]()
