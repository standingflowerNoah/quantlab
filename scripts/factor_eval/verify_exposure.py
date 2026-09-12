# -*- coding: utf-8 -*-
"""exp_* 暴露列口径污染检查
================================
背景：2026-09-13 凌晨回填期间，多个僵尸回填进程（修正 core_ranked
口径 bug 之前启动的旧代码）与新进程并发写同一份 L0 parquet。
旧代码 rs（核心因子秩）取全截面 rank，新代码取因子∩核心交集内
rank——覆盖不完整时 exp_* 会偏。

本脚本对已入库因子的 exp_* 列逐日重算对比（现行正确口径），
输出污染因子清单（max|diff| > 1e-9 即污染），供 --force 定点重跑。

用法：
  python scripts/factor_eval/verify_exposure.py [--pool ashare_ex] [--out reports/]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from quantlab.factor import metric_store as ms  # noqa: E402

TOL = 1e-9


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--pool", default="ashare_ex")
    p.add_argument("--out", default="reports/exposure_contamination.csv")
    a = p.parse_args()

    stored = ms.read_l0(a.pool)
    if stored.empty:
        print("L0 为空")
        return
    stored["date"] = pd.to_datetime(stored["date"])
    names = sorted(stored["factor"].unique())
    print(f"检查 {len(names)} 因子的 exp_* 口径（容差 {TOL}）")

    # 进程级缓存（现行正确口径）
    d0 = str(stored["date"].min().date())
    d1 = str(stored["date"].max().date())
    ms.worker_init(d0, d1, mem_limit_gb=4)

    rows = []
    try:
        for i, name in enumerate(names, 1):
            s = stored[stored["factor"] == name].set_index("date")
            cc = ms.compute_factor_core_corr(
                name, a.pool, start=d0, end=d1, con=ms._W["con"],
                core_src="core_vals" if a.pool == "ashare_ex" else None)
            if cc.empty:
                rows.append({"factor": name, "max_diff": float("nan"),
                             "bad_cols": "no_recompute"})
                continue
            cc["date"] = pd.to_datetime(cc["date"])
            diffs = {}
            for core in ms.STYLE_REPS:  # exp_{style} 列
                sub = cc[cc["core"] == core].set_index("date")["rho"]
                col = f"exp_{core}"
                if col not in s.columns or s[col].notna().sum() == 0:
                    continue
                joined = pd.concat([s[col], sub], axis=1,
                                    join="inner", keys=["old", "new"])
                if joined.empty:
                    continue
                diffs[col] = (joined["old"] - joined["new"]).abs().max()
            md = max((v for v in diffs.values() if pd.notna(v)), default=0.0)
            bad = [k for k, v in diffs.items()
                   if pd.notna(v) and v > TOL]
            rows.append({"factor": name, "max_diff": md,
                         "bad_cols": ";".join(bad) if bad else ""})
            if i % 10 == 0 or i == len(names):
                flag = sum(1 for r in rows if r["max_diff"] > TOL)
                print(f"  [{i}/{len(names)}] 污染 {flag} 个", flush=True)
    finally:
        ms._worker_finalize()

    df = pd.DataFrame(rows)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False, encoding="utf-8-sig")
    bad_df = df[df["max_diff"] > TOL]
    print(f"\n结果 -> {out}")
    if bad_df.empty:
        print("全部干净：无 exp_* 口径污染")
    else:
        print(f"污染因子 {len(bad_df)} 个（需 --force 定点重跑）：")
        print(bad_df.to_string(index=False))


if __name__ == "__main__":
    main()
