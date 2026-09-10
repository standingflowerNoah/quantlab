#!/usr/bin/env python3
"""Phase 1：Q1 基础事实 + G1 裁决（尾盘隔夜策略）
=================================================================
回答：无条件情形下，"尾盘买入→次日开盘卖出"能不能赚钱？差成本线多远？

口径：
- 主样本 = daily_segments（2022-2026，ashare_ex 近似，剔除停牌）
- 日度统计 = 逐日横截面等权均值的时间序列（正确口径，t 值按日序列算，
  避免把所有 (code,date) 当独立样本导致 t 值虚高）
- 成本线 = 15bp/往返（研究设计 §0）
- 2025+ 用 pseudo_daily 对照 exec（14:56 买）vs ref（收盘买，仅作上界）

输出：reports/overnight/q1_basic_facts.json + 控制台表格
"""
from __future__ import annotations

import glob
import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SEG_DIRS = {
    "db": ROOT / "data/lake/factor/overnight/daily_segments",
    "fsdb": ROOT / "data/lake/factor/overnight/daily_segments_fsdb",
}
PD_GLOB = str(ROOT / "data/lake/factor/overnight/pseudo_daily/*.parquet")
OUT = ROOT / "reports/overnight"
COST_BP = 15.0          # 往返成本（研究设计 §0）
TRADING_DAYS = 244.0


def daily_stats(s: pd.Series, weights: pd.Series | None = None) -> dict:
    """s = 逐日横截面等权均值序列（单位：小数）"""
    s = s.dropna()
    if len(s) < 5:
        return {}
    mu = float(s.mean())
    sd = float(s.std(ddof=1))
    n = int(len(s))
    se = sd / np.sqrt(n)
    return {
        "days": n,
        "mean_bp": mu * 1e4,
        "median_bp": float(s.median()) * 1e4,
        "std_bp": sd * 1e4,
        "t_stat": mu / se if se > 0 else np.nan,
        "win_rate": float((s > 0).mean()),
        "ann_pct": mu * TRADING_DAYS * 100,
        "ann_net_pct": (mu - COST_BP / 1e4) * TRADING_DAYS * 100,
    }


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--seg", choices=["db", "fsdb"], default="fsdb")
    args = ap.parse_args()
    seg_glob = str(SEG_DIRS[args.seg] / "*.parquet")
    OUT.mkdir(parents=True, exist_ok=True)

    # ── 1. 日线口径：全期无条件（Phase 0a 产物；缺失时跳过，只出分钟口径）──
    seg = None
    try:
        seg = pd.concat([pd.read_parquet(f) for f in sorted(glob.glob(seg_glob))],
                        ignore_index=True)
    except FileNotFoundError:
        print("[daily] daily_segments 缺失（Phase 0a 未跑），跳过长样本统计")

    res: dict = {"daily": {}, "daily_by_year": {}, "exec_2025": {}}

    u = None
    if seg is not None:
        seg["year"] = seg.date.astype(str).str[:4]
        u = seg[seg.in_ex & ~seg.halted].copy()
        if u.empty:
            print("[daily] in_ex 全为 False（Phase 0a 产物为旧版），跳过长样本统计")
            u = None
            seg = None

    if seg is not None and u is not None:
        print(f"[daily] rows={len(seg):,} universe rows={len(u):,} "
              f"{u.date.min()} → {u.date.max()}")

        # 全期（逐日等权横截面均值）
        for name, col, sub in [
            ("overnight", "overnight_ret", u),
            ("intraday", "intraday_ret", u),
            ("total", "total_ret", u),
            ("overnight_ex_limit", "overnight_ret", u[~u.close_at_limit & ~u.open_at_limit]),
        ]:
            ts = sub.groupby("date")[col].mean()
            res["daily"][name] = daily_stats(ts)

        # 分年度
        for yr, g in u.groupby("year"):
            ts = g.groupby("date")["overnight_ret"].mean()
            res["daily_by_year"][yr] = daily_stats(ts)

        # 截面分布（全样本 pool，仅作形态参考）
        res["daily"]["cross_section_pool"] = {
            "n": int(u.overnight_ret.notna().sum()),
            "q05_bp": float(u.overnight_ret.quantile(0.05) * 1e4),
            "q25_bp": float(u.overnight_ret.quantile(0.25) * 1e4),
            "q50_bp": float(u.overnight_ret.median() * 1e4),
            "q75_bp": float(u.overnight_ret.quantile(0.75) * 1e4),
            "q95_bp": float(u.overnight_ret.quantile(0.95) * 1e4),
            "share_abs_gt_9pct": float((u.overnight_ret.abs() > 0.09).mean()),
        }

        # 可投资性：开盘涨停（卖不出）/ 收盘涨停（14:56 买不进）占比
        res["daily"]["tradability"] = {
            "close_at_limit_share": float(u.close_at_limit.mean()),
            "open_at_limit_share": float(u.open_at_limit.mean()),
            "halted_share_all": float(seg.halted.mean()),
        }

    # ── 2. 分钟口径（2025+）：14:56 执行 vs 收盘 ──────────────────
    try:
        pdm = pd.concat([pd.read_parquet(f) for f in sorted(glob.glob(PD_GLOB))],
                        ignore_index=True)
        pdm["year"] = pdm.date.astype(str).str[:4]
        # universe 代理：剔除北交所（4/8/920 前缀）；ST 与次新无法在分钟层识别
        no_bj = ~pdm.code.str.startswith(("4", "8")) & ~pdm.code.str.startswith("920")
        pdm = pdm[no_bj].copy()
        if seg is not None:
            uni_keys = u[["code", "date"]].drop_duplicates()
            pdm = pdm.merge(uni_keys, on=["code", "date"], how="inner")
        print(f"[minutes] rows={len(pdm):,} codes={pdm.code.nunique()} "
              f"{pdm.date.min()} → {pdm.date.max()}")
        # 剔除卖出日开盘涨停（卖不出）的样本用于主口径
        ts_exec = pdm.groupby("date")["exec_ret"].mean()
        ts_ref = pdm.groupby("date")["ref_ret"].mean()
        ts_raw = pdm.groupby("date")["exec_raw"].mean()
        res["exec_2025"]["exec_1456_to_open"] = daily_stats(ts_exec)
        res["exec_2025"]["ref_close_to_open"] = daily_stats(ts_ref)
        res["exec_2025"]["exec_raw_no_divadj"] = daily_stats(ts_raw)
        res["exec_2025"]["exec_minus_ref_bp"] = float(
            (ts_exec.mean() - ts_ref.mean()) * 1e4)
        res["exec_2025"]["tail_930_1456"] = daily_stats(pdm.groupby("date")["tail_ret_930_1456"].mean())
        res["exec_2025"]["intraday_eod"] = daily_stats(pdm.groupby("date")["intraday_eod"].mean())
        for yr, g in pdm.groupby("year"):
            res["exec_2025"][f"exec_{yr}"] = daily_stats(g.groupby("date")["exec_ret"].mean())
    except FileNotFoundError as e:
        res["exec_2025"]["error"] = str(e)

    # ── 3. G1 裁决 ───────────────────────────────────────────────
    d = res["daily"].get("overnight", {})
    exec_s = res["exec_2025"].get("exec_1456_to_open", {})
    exec_mean = exec_s.get("mean_bp")
    g1 = {
        "criterion": "无条件毛隔夜段 > +5bp/日",
        "daily_close_gross_bp": d.get("mean_bp"),
        "exec_1456_gross_bp": exec_mean,
        "verdict": "PASS" if (exec_mean or -99) > 5 else "FAIL",
        "note": "exec 口径为实际可执行收益（14:56 买、次日开盘卖，含除权调整）",
    }
    res["G1"] = g1

    (OUT / f"q1_basic_facts_{args.seg}.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2, default=float), encoding="utf-8")

    # ── 4. 控制台表格 ────────────────────────────────────────────
    def fmt(t: dict) -> str:
        if not t:
            return "  (no data)"
        return (f"  days={t['days']:>4d}  mean={t['mean_bp']:>7.2f}bp  "
                f"t={t['t_stat']:>6.2f}  win={t['win_rate']:>5.1%}  "
                f"ann={t['ann_pct']:>7.2f}%  ann_net={t['ann_net_pct']:>8.2f}%")

    print("\n=== 全期（2022-2026，日线口径，ashare_ex）===")
    for k in ["overnight", "intraday", "total", "overnight_ex_limit"]:
        print(f"{k:>20s}{fmt(res['daily'].get(k, {}))}")
    print("\n=== 分年度 overnight ===")
    for yr in sorted(res["daily_by_year"]):
        print(f"{yr:>20s}{fmt(res['daily_by_year'][yr])}")
    print("\n=== 分钟口径 2025+（14:56 执行）===")
    for k, v in res["exec_2025"].items():
        if isinstance(v, dict) and v:
            print(f"{k:>20s}{fmt(v)}")
    print(f"\n  exec - ref = {res['exec_2025'].get('exec_minus_ref_bp', float('nan')):.2f} bp/日")
    print(f"\n=== G1: {g1['verdict']} ===")
    dc = g1["daily_close_gross_bp"]
    em = "n/a" if exec_mean is None else f"{exec_mean:.2f}bp/日"
    print(f"  close 口径 {'n/a' if dc is None else f'{dc:.2f}bp/日'} | "
          f"exec 口径 {em}  （阈值 +5bp）")


if __name__ == "__main__":
    main()
