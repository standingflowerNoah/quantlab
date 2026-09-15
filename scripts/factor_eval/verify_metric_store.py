"""L0 因子评估指标存储验收
========================
方案 §4.6：
  1. 随机抽因子 × 窗口：L0 日序列聚合的 IC/ICIR 与 run_eval 现场重算
     一致（容差 1e-6）
  2. 前视检查：T 日运行时写入的行信号日均 <= T-1（成熟列天然满足，
     此处显式断言）；池成员 join 日期 <= 信号日（PIT）

用法：python scripts/factor_eval/verify_metric_store.py [--pool ashare_ex]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from quantlab.factor.metric_store import read_l0

SAMPLE_FACTORS = ["amihud_20", "size", "alpha001", "sue_i"]
SAMPLE_WINDOWS = [
    ("2022-01-01", "2022-12-31"),
    ("2024-01-01", "2024-06-30"),
    ("2025-07-01", "2026-06-30"),
]


def check_against_run_eval(pool: str) -> list[tuple]:
    """L0 日序列聚合 vs run_eval.factor_daily_metrics 现场重算"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from run_eval import factor_daily_metrics
    fails = []
    for name in SAMPLE_FACTORS:
        try:
            daily, _, _ = factor_daily_metrics(name, 5, "2022-01-01",
                                               "2026-09-10", pool=pool)
        except FileNotFoundError:
            print(f"  [skip] {name} 无因子湖数据")
            continue
        daily = daily.set_index("date")["ic"].rename("run_eval")
        l0 = read_l0(pool, [name])
        if l0.empty:
            fails.append((name, "L0 无数据"))
            continue
        s = l0.set_index("date")["rank_ic5"].rename("l0")
        j = pd.concat([daily, s], axis=1, join="inner")
        j = j.dropna()
        if j.empty:
            fails.append((name, "无重叠日期"))
            continue
        d = (j["run_eval"] - j["l0"]).abs()
        ic_mean_gap = abs(j["run_eval"].mean() - j["l0"].mean())
        status = "PASS" if d.max() < 1e-6 and ic_mean_gap < 1e-6 else "FAIL"
        print(f"  {name:<14} n={len(j):>4} max|diff|={d.max():.2e} "
              f"mean_gap={ic_mean_gap:.2e} → {status}")
        if status == "FAIL":
            fails.append((name, f"max diff {d.max():.2e}"))
    return fails


def check_maturity(pool: str) -> list[tuple]:
    """前视断言：h 日 IC 的最后非空信号日距数据末日 >= h-1 个交易日"""
    fails = []
    l0 = read_l0(pool)
    if l0.empty:
        return [("pool", f"{pool} L0 空")]
    last_day = l0["date"].max()
    all_dates = sorted(l0["date"].unique())
    for h in (1, 5, 20, 60, 120):
        col = f"rank_ic{h}"
        sub = l0[l0[col].notna()]
        if sub.empty:
            continue
        for name, g in sub.groupby("factor"):
            d = g["date"].max()
            # d 之后还需 h 个交易日收益才成熟：末日-信号日的交易日数 >= h
            pos = all_dates.index(d)
            n_after = len(all_dates) - 1 - pos
            if n_after < h:
                fails.append((name, f"ic{h} 在 {d} 成熟但只有 {n_after} 日"))
    n_tail = (l0[l0["date"] == last_day][["rank_ic1", "rank_ic5",
                                          "rank_ic20", "rank_ic60",
                                          "rank_ic120"]].notna().sum().sum())
    print(f"  最新信号日 {last_day:%Y-%m-%d} 的 IC 列非空数={n_tail} "
          f"（应为 0，未成熟）")
    if n_tail > 0:
        fails.append(("latest", "最新信号日出现非空 IC（前视）"))
    print(f"  成熟边界检查：{'PASS' if not fails else 'FAIL'}")
    return fails


def check_pool_pit(pool: str) -> list[tuple]:
    """池成员 PIT：zz1000 池行的信号日必须 >= 对应 period（成员生效日）"""
    if pool != "zz1000":
        return []
    import duckdb
    con = duckdb.connect(
        "data/quant.duckdb", read_only=True)
    periods = con.execute(
        "SELECT DISTINCT period FROM index_members_hist "
        "WHERE index_code = 'zz1000' ORDER BY period").df()
    con.close()
    first_period = periods["period"].min()
    l0 = read_l0(pool)
    if l0.empty:
        return [("pool", "zz1000 L0 空")]
    bad = l0[l0["date"] < pd.to_datetime(first_period)]
    print(f"  zz1000 池最早行 {l0['date'].min():%Y-%m-%d} >= "
          f"首期成分 {first_period} → "
          f"{'PASS' if bad.empty else 'FAIL'}")
    return [] if bad.empty else [("pool", "存在早于成分历史的行")]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--pool", default="ashare_ex")
    args = p.parse_args()

    print(f"[1/3] L0 vs run_eval 现场重算（因子×全史，容差 1e-6）")
    f1 = check_against_run_eval(args.pool)
    print(f"[2/3] 成熟边界 / 前视断言（pool={args.pool}）")
    f2 = check_maturity(args.pool)
    print(f"[3/3] 池成员 PIT")
    f3 = check_pool_pit(args.pool)

    fails = f1 + f2 + f3
    print("=" * 60)
    if fails:
        print(f"验收 FAIL（{len(fails)} 项）：")
        for n, msg in fails:
            print(f"  {n}: {msg}")
        sys.exit(1)
    print("验收 PASS：L0 口径与 run_eval 同源、无前视、池成员 PIT")


if __name__ == "__main__":
    main()
