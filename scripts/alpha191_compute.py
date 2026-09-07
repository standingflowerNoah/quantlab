#!/usr/bin/env python3
"""国泰君安 Alpha191 批量计算 + IC 评估
=====================================
用法（与 data update 串行，DuckDB 单写者）：
  $PY scripts/alpha191_compute.py calc --range 1-191        # 计算落库
  $PY scripts/alpha191_compute.py calc --names alpha001,alpha002
  $PY scripts/alpha191_compute.py ic                        # 批量 IC 评估
  $PY scripts/alpha191_compute.py report                    # 汇总报告
"""
import argparse
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import pandas as pd

from quantlab.config import FACTOR_DIR, REPORTS_DIR, get_logger

log = get_logger("alpha191_cli")


def parse_range(spec: str) -> list[str]:
    a, b = spec.split("-")
    return [f"alpha{n:03d}" for n in range(int(a), int(b) + 1)]


def cmd_calc(args):
    from quantlab.factor.alpha191 import compute_alpha191
    if args.names:
        names = [n.strip() for n in args.names.split(",")]
    elif args.range:
        names = parse_range(args.range)
    else:
        names = None
    m = compute_alpha191(names=names, start=args.start, save=True)
    out = REPORTS_DIR / "alpha191_manifest.csv"
    m.to_csv(out, index=False, encoding="utf-8-sig")
    print(m.to_string(index=False))
    print(f"\nmanifest 已写入 {out}")


def cmd_ic(args):
    from quantlab.factor.quality import factor_summary
    names = sorted(d.name for d in FACTOR_DIR.iterdir()
                   if d.is_dir() and d.name.startswith("alpha")
                   and list(d.glob("part-*.parquet")))
    if not names:
        print("因子库中无 alpha* 因子，先运行 calc")
        return
    log.info(f"IC 评估 {len(names)} 个 alpha 因子（horizon=5/20）")
    rows = []
    for n in names:
        try:
            s5 = factor_summary(n, horizon=5)
            s20 = factor_summary(n, horizon=20)
            r5, r20 = s5.iloc[0], s20.iloc[0]
            rows.append({"factor": n, "ic5": r5["ic_mean"], "icir5": r5["icir"],
                         "win5": r5["ic_win_rate"], "ic20": r20["ic_mean"],
                         "icir20": r20["icir"], "n_days": r5["n_days"]})
            log.info(f"{n} ic5={r5['ic_mean']:+.4f} icir20={r20['icir']:+.3f}")
        except Exception as e:
            rows.append({"factor": n, "ic5": None, "icir5": None, "win5": None,
                         "ic20": None, "icir20": None, "n_days": 0})
            log.warning(f"{n} IC FAIL: {e}")
    df = pd.DataFrame(rows)
    df["abs_icir20"] = df["icir20"].abs()
    df = df.sort_values("abs_icir20", ascending=False)
    out = REPORTS_DIR / "alpha191_ic.csv"
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(df.head(30).to_string(index=False))
    print(f"\n共 {len(df)} 个因子，IC 明细已写入 {out}")


def cmd_report(args):
    icf = REPORTS_DIR / "alpha191_ic.csv"
    if not icf.exists():
        print("无 IC 结果，先运行 ic")
        return
    df = pd.read_csv(icf)
    eff = df[(df["ic5"].abs() > 0.02) & (df["icir20"].abs() > 0.3)]
    print(f"总因子 {len(df)}，显著（|IC5|>0.02 且 |ICIR20|>0.3）{len(eff)} 个：")
    cols = ["factor", "ic5", "icir5", "ic20", "icir20", "win5"]
    print(eff[cols].head(50).to_string(index=False))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("calc")
    c.add_argument("--range", help="如 1-50")
    c.add_argument("--names", help="逗号分隔因子名")
    c.add_argument("--start", default="2022-06-01")
    sub.add_parser("ic")
    sub.add_parser("report")
    args = ap.parse_args()
    {"calc": cmd_calc, "ic": cmd_ic, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
