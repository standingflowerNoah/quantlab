"""fsdb 稳定性压测：定位「引擎劣化」的触发因素与恢复代价

只读，不写入。每步实时输出（flush），单请求超时 12s，不会卡死。
用法：
    python scripts/probe_fsdb_stability.py baseline   # 干净基线：串行延迟
    python scripts/probe_fsdb_stability.py load       # 并发 / 大范围查询
    python scripts/probe_fsdb_stability.py probe      # 单发探测（检查当前状态）
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import msgpack                                             # noqa: E402
import requests                                            # noqa: E402

HOST, PORT = "127.0.0.1", 7899
REQ_TIMEOUT = 12.0
MODE = sys.argv[1] if len(sys.argv) > 1 else "probe"

CODES = ["600519", "000001", "300750", "688981", "601318", "002594",
         "600036", "000858", "601899", "430047", "920002", "301269"]
DAY_PATHS = [f"/?cmd=vals&t=日k&k1=key:{c}&k2=fwz:20260801,20260911"
             for c in CODES]
MIN_PATHS = [f"/?cmd=vals&t=分钟k&k1=key:{c}"
             f"&k2=fwz:20260910093000,20260910150000" for c in CODES]
# 大范围：单股 20 交易日分钟线（≈4800 行）
BIG_PATHS = [f"/?cmd=vals&t=分钟k&k1=key:{c}"
             f"&k2=fwz:20260811000000,20260910235959" for c in CODES]


def get(path: str):
    z = requests.get(f"http://{HOST}:{PORT}{path}", timeout=REQ_TIMEOUT,
                     proxies={"http": None, "https": None})
    return msgpack.unpackb(z.content, raw=False)


def timed(path: str):
    t0 = time.perf_counter()
    try:
        r = get(path)
        n = len(r) if isinstance(r, (list, dict)) else -1
        return True, time.perf_counter() - t0, n
    except Exception:                                       # noqa: BLE001
        return False, time.perf_counter() - t0, -1


def stats(label: str, res: list):
    ok = [r[1] for r in res if r[0]]
    if not ok:
        print(f"  {label:<24} n={len(res):<4} 全部失败/超时", flush=True)
        return
    lat = sorted(ok)

    def p(x):
        return lat[min(len(lat) - 1, int(len(lat) * x))] * 1000
    print(f"  {label:<24} n={len(res):<4} 成功率 {len(ok)/len(res)*100:5.1f}%  "
          f"p50={p(.5):6.0f}  p95={p(.95):6.0f}  max={lat[-1]*1000:7.0f} ms",
          flush=True)


def seg_sequential(paths, n, label):
    res = [timed(paths[i % len(paths)]) for i in range(n)]
    stats(label, res)
    return res


def seg_concurrent(paths, threads, per, label):
    lock = threading.Lock()
    out: list = []

    def w(tid):
        loc = [timed(paths[(tid * per + i) % len(paths)]) for i in range(per)]
        with lock:
            out.extend(loc)

    ths = [threading.Thread(target=w, args=(t,)) for t in range(threads)]
    t0 = time.perf_counter()
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    stats(label, out)
    print(f"      墙钟 {time.perf_counter()-t0:.1f}s", flush=True)
    return out


print("=" * 92, flush=True)
print(f"fsdb 压测 mode={MODE}  {time.strftime('%H:%M:%S')}", flush=True)
print("=" * 92, flush=True)

if MODE == "probe":
    ok, el, n = timed(DAY_PATHS[0])
    print(f"  单发探测: ok={ok} {el*1000:.0f}ms rows={n}", flush=True)

elif MODE == "baseline":
    print("\n[A] 串行日线 ×24（干净基线）", flush=True)
    seg_sequential(DAY_PATHS, 24, "日线 ×24")
    print("\n[B] 串行分钟线单日 ×12", flush=True)
    seg_sequential(MIN_PATHS, 12, "分钟线单日 ×12")
    print("\n[C] 串行大范围（单股 20 日分钟）×6", flush=True)
    seg_sequential(BIG_PATHS, 6, "分钟线 20日 ×6")

elif MODE == "load":
    print("\n[D] 并发 4 线程 × 6 次日线", flush=True)
    seg_concurrent(DAY_PATHS, 4, 6, "并发 4×6")
    print("\n[E] 并发后单发复查", flush=True)
    for i in range(3):
        ok, el, n = timed(DAY_PATHS[0])
        print(f"      复查{i+1}: ok={ok} {el*1000:.0f}ms rows={n}", flush=True)
    print("\n[F] 并发分钟线 4 线程 × 3", flush=True)
    seg_concurrent(MIN_PATHS, 4, 3, "并发分钟 4×3")
    print("\n[G] 再复查单发", flush=True)
    for i in range(3):
        ok, el, n = timed(DAY_PATHS[0])
        print(f"      复查{i+1}: ok={ok} {el*1000:.0f}ms rows={n}", flush=True)

print("\n完成", flush=True)
