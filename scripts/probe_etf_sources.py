"""fsdb 侧 ETF 数据可得性探测
================================================================
1. 候选表名扫描（t=表名 & k1=key:510300，有界）→ 发现是否有未接入的 ETF 相关表
2. ETF 分钟覆盖抽样（20 只 × 3 个日期）
3. ETF 复权表明细（510300 的 14 条到底是什么）
4. 复权表全量覆盖率抽样（50 只）
"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quantlab.data.sources import fsdb_source as fs          # noqa: E402

CODE = "510300"

CANDIDATES = [
    "日k", "分钟k", "复权", "板块", "股票代码",
    "ETF", "基金", "净值", "份额", "规模", "成分", "持仓",
    "ETF净值", "基金净值", "ETF份额", "基金份额", "基金规模",
    "IOPV", "折溢价", "指数", "日历", "交易日历", "tick",
    "5分钟k", "15分钟k", "30分钟k", "60分钟k", "周k", "月k",
    "ETF成分", "基金持仓", "股票", "证券", "权息",
]


def hr(t: str) -> None:
    print(f"\n{'='*64}\n{t}\n{'='*64}")


hr("1. fsdb 候选表名扫描（k1=key:510300）")
for t in CANDIDATES:
    for cmd in ("vals", "keys"):
        try:
            r = fs.http_get(f"/?cmd={cmd}&t={t}&k1=key:{CODE}")
        except Exception as e:                            # noqa: BLE001
            msg = str(e)[:70]
            if "不存在" in msg or "invalid" in msg.lower() or "无此" in msg:
                break                                     # 表不存在，跳过 keys
            print(f"  {t:<8} {cmd:<5} ERR {msg}")
            break
        if r is None:
            continue
        kind = type(r).__name__
        n = len(r) if hasattr(r, "__len__") else "-"
        print(f"  ✅ {t:<8} {cmd:<5} -> {kind} n={n}  {str(r)[:110]}")

hr("2. ETF 分钟覆盖抽样")
ETFS = ["510300", "510500", "159915", "518880", "512880", "588000",
        "513100", "159901", "510050", "159919", "512000", "515000",
        "588080", "159949", "515790", "512690", "159928", "512010",
        "516110", "159941"]
DATES = ["20250102", "20260105", "20260910"]


def probe_min(code: str) -> dict:
    out = {"code": code}
    for d in DATES:
        try:
            x = fs.minute_bars(code, d, d)
            out[d] = 0 if x is None else len(x)
        except Exception:                                 # noqa: BLE001
            out[d] = -1
    return out


t0 = time.time()
with ThreadPoolExecutor(max_workers=4) as ex:
    res = [f.result() for f in as_completed(
        [ex.submit(probe_min, c) for c in ETFS])]
res.sort(key=lambda r: r["code"])
print(f"  {'code':<8} " + "  ".join(f"{d:<10}" for d in DATES))
for r in res:
    print(f"  {r['code']:<8} " + "  ".join(f"{r[d]:<10}" for d in DATES))
ok = sum(1 for r in res if r["20260910"] > 0)
print(f"  → 2026-09-10 有数据 {ok}/{len(ETFS)} 只（耗时 {time.time()-t0:.0f}s）")

hr("3. ETF 复权表明细（510300）")
try:
    krows = fs.http_get(f"/?cmd=keys&t=复权&k1=key:{CODE}")
    vrows = fs.http_get(f"/?cmd=vals&t=复权&k1=key:{CODE}")
    ks = krows if isinstance(krows, list) else []
    vs = vrows if isinstance(vrows, list) else []
    print(f"  keys n={len(ks)}  vals n={len(vs)}")
    for k, v in list(zip(ks, vs))[:20]:
        print(f"    {k}  ->  {v}")
except Exception as e:                                    # noqa: BLE001
    print("  ERR", str(e)[:120])

hr("4. 复权表覆盖率抽样（50 只基金代码）")
codes = fs.fund_codes()[:50]


def probe_adj(c: str) -> tuple[str, int]:
    try:
        a = fs.adj_factors(c)
        return c, (0 if a is None or a.empty else len(a))
    except Exception:                                     # noqa: BLE001
        return c, -1


with ThreadPoolExecutor(max_workers=4) as ex:
    adj = [f.result() for f in as_completed(
        [ex.submit(probe_adj, c) for c in codes])]
nonzero = [c for c, n in adj if n > 0]
print(f"  50 只中 {len(nonzero)} 只有复权事件，{sum(1 for _, n in adj if n == 0)} 只为 0")
print(f"  非零样例: {nonzero[:12]}")
