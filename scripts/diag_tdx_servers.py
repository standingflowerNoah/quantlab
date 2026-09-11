"""通达信服务器池诊断：两层探测（TCP 连通 + 协议层取数）

背景：2026-09-11 起 `kline_daily/index_kline/finance_snapshot` 三域连续报
"通达信服务器池全部不可用"。项目只硬编码 3 个 IP，而 pytdx 内置 104 个。
本脚本判定：是"全网不通"（网络/防火墙）还是"我们的池子太小/太旧"。

用法：
    python scripts/diag_tdx_servers.py [扫描数量, 默认40]
"""
from __future__ import annotations

import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from pytdx.config.hosts import hq_hosts           # noqa: E402
from pytdx.hq import TdxHq_API                    # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 40

# 项目当前池（对照组）
CURRENT = [("218.75.126.9", 7709), ("115.238.56.198", 7709),
           ("115.238.90.165", 7709)]


def tcp_ok(host: str, port: int, t: float = 3.0) -> bool:
    s = socket.socket()
    s.settimeout(t)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def probe(host: str, port: int):
    """返回 (tcp, 日线根数, 分钟根数, 备注)"""
    if not tcp_ok(host, port):
        return host, port, False, -1, -1, "TCP 不通"
    api = TdxHq_API(heartbeat=False)
    try:
        if not api.connect(host, port, time_out=6):
            return host, port, True, -1, -1, "握手失败"
        d = api.get_security_bars(4, 1, "600519", 0, 5)     # 日线
        m = api.get_security_bars(8, 1, "600000", 0, 5)     # 1 分钟
        return host, port, True, len(d or []), len(m or []), "ok"
    except Exception as e:                                   # noqa: BLE001
        return host, port, True, -1, -1, f"{type(e).__name__}"
    finally:
        try:
            api.disconnect()
        except Exception:                                    # noqa: BLE001
            pass


def main():
    print("=" * 96, flush=True)
    print("通达信服务器池诊断", flush=True)
    print("=" * 96, flush=True)

    # ── 1. 先测项目当前池 ──────────────────────────────────────
    print("\n[1] 项目当前硬编码池（3 个）", flush=True)
    for host, port in CURRENT:
        h, p, tcp, d, m, note = probe(host, port)
        print(f"  {h:<18} tcp={'Y' if tcp else 'N'}  日线={d:<4} 分钟={m:<4} {note}",
              flush=True)

    # ── 2. 扫 pytdx 内置池 ────────────────────────────────────
    cand = [(h[1], h[2]) for h in hq_hosts][:N]
    print(f"\n[2] pytdx 内置池抽样（前 {len(cand)} 个 / 共 {len(hq_hosts)}）",
          flush=True)
    t0 = time.time()
    rows = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = [ex.submit(probe, h, p) for h, p in cand]
        for i, f in enumerate(as_completed(futs), 1):
            rows.append(f.result())
            if i % 10 == 0:
                print(f"    ...{i}/{len(cand)} ({time.time()-t0:.0f}s)", flush=True)

    tcp_n = sum(1 for r in rows if r[2])
    ok = [r for r in rows if r[3] > 0]
    print(f"\n  TCP 可连 {tcp_n}/{len(rows)}    协议层可用（日线>0）{len(ok)}/{len(rows)}",
          flush=True)

    print("\n[3] 可用服务器明细", flush=True)
    for h, p, tcp, d, m, note in sorted(ok, key=lambda x: -x[3])[:20]:
        print(f"  {h:<18}:{p:<6} 日线={d:<4} 分钟={m:<4}", flush=True)

    if not ok:
        print("  （无）", flush=True)

    # ── 4. 判定 ───────────────────────────────────────────────
    print("\n[4] 判定", flush=True)
    if not rows:
        print("  无样本", flush=True)
    elif tcp_n == 0:
        print("  ❌ 全部 TCP 不通 → 本机网络/防火墙层面阻断 7709，非服务器问题",
              flush=True)
    elif ok:
        print(f"  ✅ 有 {len(ok)} 个服务器协议层可用 → 问题是【我们的池子太小/太旧】，"
              f"扩展 TDX_SERVERS 即可恢复", flush=True)
    else:
        print("  ⚠️ TCP 可连但协议层全部取不到数据 → 疑中间设备代答握手/丢应用层数据，"
              "或 pytdx 协议版本与当前服务端不兼容", flush=True)


if __name__ == "__main__":
    main()
