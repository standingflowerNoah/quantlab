"""探测 fsdb `日k` 表对 ETF/基金代码的历史深度
================================================================
背景：etf.py 的 update_etf_daily 把 start=end=当日 → 只落了快照。
本脚本对全量基金代码跑一次大范围查询，量化历史可回补性。

输出：reports/fsdb_etf_history_probe.json + 控制台聚合统计
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quantlab.data.sources import fsdb_source as fs          # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "reports" / "fsdb_etf_history_probe.json"
START, END = "19900101", "20261231"


def probe(code: str) -> dict:
    try:
        d = fs.day_bars(code, START, END)
    except Exception as e:                                   # noqa: BLE001
        return {"code": code, "err": str(e)[:80]}
    if d is None or d.empty:
        return {"code": code, "n": 0, "d0": None, "d1": None}
    dd = d["date"].astype(str)
    return {"code": code, "n": int(len(d)), "d0": dd.min(), "d1": dd.max(),
            "cols": list(d.columns)}


def main() -> None:
    codes = fs.fund_codes()
    print(f"基金代码 {len(codes)} 只，开始探测（并发 4）")
    fs.ensure_healthy()
    t0 = time.time()
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(probe, c): c for c in codes}
        for i, f in enumerate(as_completed(futs), 1):
            rows.append(f.result())
            if i % 400 == 0:
                print(f"  {i}/{len(codes)} ({time.time()-t0:.0f}s)")

    ok = [r for r in rows if r.get("n")]
    empty = [r for r in rows if r.get("n") == 0]
    err = [r for r in rows if r.get("err")]
    print(f"\n完成 {time.time()-t0:.0f}s：有数据 {len(ok)} / 空 {len(empty)} / 错误 {len(err)}")
    if ok:
        total_rows = sum(r["n"] for r in ok)
        d0s = sorted(r["d0"] for r in ok)
        print(f"总行数 {total_rows:,}，最早起始 {d0s[0]}，样例 {d0s[:5]}")
        # 按起始年份分布
        from collections import Counter
        yr = Counter(r["d0"][:4] for r in ok)
        print("\n起始年份分布（前 15）：")
        for y, n in sorted(yr.items())[:15]:
            print(f"  {y}: {n}")
        # 深度分档
        buckets = {"<=2010": 0, "2011-2015": 0, "2016-2020": 0, "2021-2023": 0,
                   "2024+": 0}
        for r in ok:
            y = int(r["d0"][:4])
            if y <= 2010:
                buckets["<=2010"] += 1
            elif y <= 2015:
                buckets["2011-2015"] += 1
            elif y <= 2020:
                buckets["2016-2020"] += 1
            elif y <= 2023:
                buckets["2021-2023"] += 1
            else:
                buckets["2024+"] += 1
        print("\n起始深度分档：")
        for k, v in buckets.items():
            print(f"  {k}: {v} 只")
        print(f"\n字段：{ok[0].get('cols')}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细 → {OUT}")


if __name__ == "__main__":
    main()
