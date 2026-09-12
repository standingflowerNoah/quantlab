# -*- coding: utf-8 -*-
"""审计 JSON 的 checks.ic 批量升级 v2：Pearson + Rank 双类型 × h=1/5/20
=====================================================================
- 逐因子调 factor_ic_extended（SQL 单查询每 horizon，Store readonly），
  原地 patch data/lake/factor_audit/{name}.json 的 checks.ic 块；
  其余检查项（structure/pit/coverage…）原样保留。
- legacy 键 ic5/icir5/win5/ic20/icir20 = rank 值，FDR/batch 读取向后兼容。
- 串行执行；不与流水线并行（虽然只读，避免无关负担）。

用法：python scripts/refresh_audit_ic.py [--names a,b] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
AUDIT_DIR = ROOT / "data" / "lake" / "factor_audit"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--names", help="逗号分隔子集（缺省全部）")
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 个（调试）")
    a = p.parse_args()

    from quantlab.factor.quality import factor_ic_extended  # noqa: PLC0415

    files = sorted(AUDIT_DIR.glob("*.json"))
    if a.names:
        wanted = {s.strip() for s in a.names.split(",")}
        files = [f for f in files if f.stem in wanted]
    if a.limit:
        files = files[: a.limit]

    n_ok = n_skip = n_fail = 0
    t0 = time.time()
    for i, fp in enumerate(files, 1):
        name = fp.stem
        try:
            ext = factor_ic_extended(name, horizons=(1, 5, 20))
            if ext is None or ext.get("rank_ic5") is None:
                n_skip += 1
                print(f"[{i}/{len(files)}] {name}: 无数据，跳过", flush=True)
                continue
            d = json.loads(fp.read_text(encoding="utf-8"))
            ic = dict(ext)
            ic.update({
                "ic5": ext["rank_ic5"], "icir5": ext["rank_icir5"],
                "win5": ext["rank_win5"],
                "ic20": ext["rank_ic20"], "icir20": ext["rank_icir20"],
                "direction": 1 if ext["rank_ic5"] > 0 else -1,
                "source": "computed_v2",
            })
            d["checks"]["ic"] = ic
            d["ic_updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            d["ic_schema"] = "v2"
            fp.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                          encoding="utf-8")
            n_ok += 1
        except Exception as e:
            n_fail += 1
            print(f"[{i}/{len(files)}] {name}: FAIL {str(e)[:100]}", flush=True)
            continue
        if i % 10 == 0 or i == len(files):
            el = time.time() - t0
            eta = el / i * (len(files) - i)
            print(f"[{i}/{len(files)}] ok={n_ok} skip={n_skip} fail={n_fail} "
                  f"elapsed={el:.0f}s eta={eta:.0f}s", flush=True)

    print(f"\n完成：ok={n_ok} skip={n_skip} fail={n_fail} "
          f"总耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
