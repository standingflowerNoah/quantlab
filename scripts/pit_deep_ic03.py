"""对 |IC|>0.03 的 registry SqlFactor 因子执行 deep PIT 穿越重放审计。

背景：日常流水线 maybe_audit 走 quick 模式，PIT 检查标 SKIP（"quick 模式"）。
本脚本对 dashboard 中 |IC5| 或 |IC20| > 0.03 的 A 组（registry SqlFactor）
因子逐个调用 pit_audit：把底层数据截断到 t0 重算因子截面，与全量版对比，
任何不一致 = 使用了 t0 之后的数据（前视穿越）。

结果写回 data/lake/factor_audit/<name>.json 的 checks.pit；
PIT FAIL 时 verdict 降为 FAIL。幂等可重跑。
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import time

sys.path.insert(0, ".")

import pandas as pd

from quantlab.config import get_logger
from quantlab.data.store import Store
from quantlab.factor.audit import pit_audit
from quantlab.factor.registry import get_factor
import quantlab.factor.library  # noqa: F401  触发注册

log = get_logger("pit_deep")

IC_TH = 0.03


def selected_names() -> list[str]:
    out = []
    for fp in sorted(glob.glob("data/lake/factor_audit/*.json")):
        name = os.path.basename(fp)[:-5]
        d = json.load(open(fp, encoding="utf-8"))
        ic = (d.get("checks") or {}).get("ic") or {}
        i5, i20 = ic.get("ic5"), ic.get("ic20")
        if not ((i5 and abs(i5) > IC_TH) or (i20 and abs(i20) > IC_TH)):
            continue
        try:
            f = get_factor(name)
        except Exception:
            continue
        if hasattr(f, "_sql"):
            out.append(name)
    return out


def main() -> None:
    names = selected_names()
    log.info("deep PIT 审计 %d 个 A 组因子: %s", len(names), names)
    store = Store()
    summary = []
    for k, name in enumerate(names, 1):
        t0 = time.time()
        try:
            factor = get_factor(name)
            df = store.read_factor(name)
            if df is None or df.empty:
                summary.append((name, "SKIP", "因子库无数据", 0))
                continue
            pit = pit_audit(factor, df)
            status = pit.get("status")
            ndiff = sum(x.get("n_diff", 0) for x in pit.get("detail", [])
                        if isinstance(x, dict))
            # 写回 json
            fp = f"data/lake/factor_audit/{name}.json"
            d = json.load(open(fp, encoding="utf-8"))
            d.setdefault("checks", {})["pit"] = pit
            if status == "FAIL":
                d["verdict"] = "FAIL"
                d["issues"] = list(d.get("issues") or []) + [
                    f"PIT 穿越重放不一致: {ndiff} 处"]
            json.dump(d, open(fp, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            summary.append((name, status, pit.get("asof_caveat"), ndiff))
            log.info("[%d/%d] %s -> %s (n_diff=%d, %.0fs)",
                     k, len(names), name, status, ndiff, time.time() - t0)
        except Exception as e:
            summary.append((name, "ERROR", str(e)[:80], 0))
            log.error("[%d/%d] %s 异常: %s", k, len(names), name, e)
    print("\n===== A 组 deep PIT 汇总 =====")
    for n, s, c, nd in summary:
        print(f"{n:26s} {s:6s} n_diff={nd:5d}  asof={c}")
    n_fail = sum(1 for _, s, _, _ in summary if s == "FAIL")
    print(f"\n总计 {len(summary)}，FAIL {n_fail}")


if __name__ == "__main__":
    main()
