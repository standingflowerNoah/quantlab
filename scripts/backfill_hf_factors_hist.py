#!/usr/bin/env python3
"""hf_* 高频因子历史回填 + 全量重审计 + 评估
=====================================================
前置：minute_feat 已含 2022-2024（scripts/backfill_minute_feat_hist.py 跑完）。

三个阶段（可分开跑，--phase）：
  compute  逐因子 compute_factor(start=2022-01-01, end=2024-12-31) 追加历史年份
           （append 模式对新年份是干净的——2022-2024 此前无任何行，无旧值残留）
  audit    逐因子 audit_factor(deep=True, force=True) 全量重审
           （必须显式调：maybe_audit 会因 latest_date 未变而跳过，IC 仍停留在 2025-2026）
  eval     逐因子 run_eval.py --type factor --start 2022-01-01 --end 2026-09-11

用法：
    python scripts/backfill_hf_factors_hist.py --phase compute
    python scripts/backfill_hf_factors_hist.py --phase audit
    python scripts/backfill_hf_factors_hist.py --phase eval
    python scripts/backfill_hf_factors_hist.py --phase all
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(ROOT / "logs" / "backfill_hf_factors_hist.log",
                                                  encoding="utf-8"),
                              logging.StreamHandler(sys.stdout)])
log = logging.getLogger("hf_hist")

AUDIT_DIR = ROOT / "data" / "lake" / "factor_audit"
FACTOR_DIR = ROOT / "data" / "lake" / "factor"

# 按既有目录发现（含 hf_wf_composite；动态避免漏新因子）
def hf_names() -> list[str]:
    return sorted(p.name for p in FACTOR_DIR.glob("hf_*") if p.is_dir())


def phase_compute(names: list[str], start: str, end: str) -> None:
    from quantlab.factor import compute_factor
    for i, name in enumerate(names, 1):
        t0 = time.time()
        try:
            df = compute_factor(name, start=start, end=end, save=True)
            n = len(df)
            log.info(f"[{i}/{len(names)}] {name}: 追加 {n:,} 行 "
                     f"({df['date'].min().date()}~{df['date'].max().date()}) "
                     f"{time.time() - t0:.0f}s")
        except Exception as e:  # noqa: BLE001
            log.error(f"[{i}/{len(names)}] {name}: 失败 {type(e).__name__}: {str(e)[:200]}")
        df = None
        import gc; gc.collect()


def phase_audit(names: list[str]) -> None:
    from quantlab.factor.audit import audit_factor
    from quantlab.factor.registry import get_factor
    for i, name in enumerate(names, 1):
        t0 = time.time()
        try:
            rec = audit_factor(name, deep=True, force=True)
            st = rec.get("checks", {}).get("structure", {})
            ic = rec.get("checks", {}).get("ic", {})
            log.info(f"[{i}/{len(names)}] {name}: verdict={rec.get('verdict')} "
                     f"{st.get('date_min')}~{st.get('date_max')} n={st.get('n_rows'):,} "
                     f"rank_ic5={ic.get('rank_ic5')} {time.time() - t0:.0f}s")
        except Exception as e:  # noqa: BLE001
            log.error(f"[{i}/{len(names)}] {name}: 审计失败 {type(e).__name__}: {str(e)[:200]}")


def phase_eval(names: list[str], start: str, end: str, tags: str) -> None:
    py = sys.executable
    script = ROOT / "scripts" / "factor_eval" / "run_eval.py"
    for i, name in enumerate(names, 1):
        cmd = [py, str(script), "--type", "factor", "--name", name,
               "--start", start, "--end", end]
        if tags:
            cmd += ["--tags", tags]
        log.info(f"[{i}/{len(names)}] eval {name}: {' '.join(cmd[2:])}")
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=str(ROOT))
        tail = (r.stdout or "").strip().splitlines()
        keep = [l for l in tail if any(k in l for k in
                ("报告", "verdict", "rank_ic", "FDR", "report", "md", "存档"))][-3:]
        log.info(f"    rc={r.returncode} {time.time() - t0:.0f}s  " + " | ".join(keep))
        if r.returncode != 0:
            log.error("    stderr: " + (r.stderr or "")[-400:])


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--phase", default="all",
                   choices=["compute", "audit", "eval", "all"])
    p.add_argument("--start", default="2022-01-01")
    p.add_argument("--end", default="2024-12-31")
    p.add_argument("--eval-end", default="2026-09-11",
                   help="评估区间终点（含既有 2025/2026）")
    p.add_argument("--tags", default="hf,hist-backfill")
    p.add_argument("--names", default="", help="逗分隔子集（缺省全部 hf_*）")
    a = p.parse_args()

    names = [x.strip() for x in a.names.split(",") if x.strip()] or hf_names()
    log.info(f"hf 因子共 {len(names)} 个: {names}")

    if a.phase in ("compute", "all"):
        phase_compute(names, a.start, a.end)
    if a.phase in ("audit", "all"):
        phase_audit(names)
    if a.phase in ("eval", "all"):
        phase_eval(names, a.start, a.eval_end, a.tags)
    log.info("完成")


if __name__ == "__main__":
    main()
