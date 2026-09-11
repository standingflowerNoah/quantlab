"""复权因子对账：fsdb `复权` 表 vs 现有 dividend_events + kline_daily.adj_factor
================================================================================
用户裁决（2026-09-11）："对账先"——先做对账，通过后才考虑作为第二源。

对账内容：
  1. 除权事件日（ex_date）双侧匹配率
  2. 字段映射：fsdb div/give/trans  vs  lake fenhong/songzhuangu
  3. 单次乘数：fsdb `mult`  vs  lake 公式 B/P_prev（注意方向可能互为倒数）
  4. 累计因子：fsdb `cum` 内部自洽性（cum_t = ∏ mult_i）
  5. 与 kline_daily.adj_factor（前复权因子）的关系

用法：
    python scripts/reconcile_fsdb_adj.py [样本数, 默认60]
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quantlab.data.store import Store                     # noqa: E402
from quantlab.data.sources import fsdb_source as fs       # noqa: E402

N_SAMPLE = int(sys.argv[1]) if len(sys.argv) > 1 else 60
random.seed(20260911)

store = Store(readonly=True)

# ── 取样：固定含 600519/000001，其余随机（覆盖沪深主板/创业/科创/北交所）──
all_codes = store.q(
    "SELECT DISTINCT code FROM dividend_events ORDER BY code")["code"].tolist()
fixed = [c for c in ("600519", "000001") if c in set(all_codes)]
pool = [c for c in all_codes if c not in set(fixed)]
sample = fixed + random.sample(pool, min(N_SAMPLE - len(fixed), len(pool)))
print(f"对账样本 {len(sample)} 只（含 {fixed}）\n", flush=True)

lake = store.q(
    "SELECT code, date, fenhong, songzhuangu, peigu, peigujia "
    "FROM dividend_events WHERE code IN (%s) ORDER BY code, date"
    % ",".join("'" + c + "'" for c in sample))
lake["date"] = pd.to_datetime(lake["date"])

rows, summary = [], []
for i, code in enumerate(sample, 1):
    try:
        fa = fs.adj_factors(code)
    except Exception as e:                                # noqa: BLE001
        print(f"  [{i}/{len(sample)}] {code} fsdb 取数失败: {e}", flush=True)
        continue
    if fa is None or fa.empty or "ex_date" not in fa.columns:
        n_l0 = int((lake["code"] == code).sum())
        summary.append({"code": code, "fsdb_events": 0, "lake_events": n_l0,
                        "matched": 0, "only_fsdb": 0, "only_lake": n_l0,
                        "div_ratio_med": None, "give_ratio_med": None})
        print(f"  [{i}/{len(sample)}] {code} fsdb 无复权记录"
              f"（lake 有 {n_l0} 条）← 记入孤例", flush=True)
        continue
    lk = lake[lake["code"] == code].copy()

    n_f, n_l = len(fa), len(lk)
    fset = set(fa["ex_date"])
    lset = set(lk["date"])
    inter = fset & lset
    only_f, only_l = fset - lset, lset - fset

    # 字段映射（仅匹配日）
    ratios = {"div/10fenhong": [], "give/songzhuan": [], "mult_inv": []}
    for _, r in fa.iterrows():
        m = lk[lk["date"] == r["ex_date"]]
        if m.empty:
            continue
        l = m.iloc[0]
        if l["fenhong"]:
            ratios["div/10fenhong"].append(float(r["div"]) / (float(l["fenhong"]) / 10.0))
        if l["songzhuangu"]:
            ratios["give/songzhuan"].append(
                (float(r["give"]) + float(r["trans"])) / float(l["songzhuangu"]))
        if r["mult"] and r["mult"] != 0:
            ratios["mult_inv"].append(1.0 / float(r["mult"]))

    summary.append({
        "code": code, "fsdb_events": n_f, "lake_events": n_l,
        "matched": len(inter), "only_fsdb": len(only_f), "only_lake": len(only_l),
        "div_ratio_med": (pd.Series(ratios["div/10fenhong"]).median()
                          if ratios["div/10fenhong"] else None),
        "give_ratio_med": (pd.Series(ratios["give/songzhuan"]).median()
                           if ratios["give/songzhuan"] else None),
    })
    if i % 15 == 0:
        print(f"  ...{i}/{len(sample)}", flush=True)

sm = pd.DataFrame(summary)
tot_f, tot_l = sm.fsdb_events.sum(), sm.lake_events.sum()
tot_m = sm.matched.sum()
print("\n" + "=" * 78)
print("① 事件日匹配率")
print("=" * 78)
print(f"  fsdb 事件 {tot_f}   lake 事件 {tot_l}   双侧匹配 {tot_m}")
print(f"  fsdb 独有 {sm.only_fsdb.sum()}（{(sm.only_fsdb.sum()/max(tot_f,1)):.1%}）"
      f"   lake 独有 {sm.only_lake.sum()}（{(sm.only_lake.sum()/max(tot_l,1)):.1%}）")
print(f"  按股匹配率（中位）：{(sm.matched / sm.fsdb_events.clip(lower=1)).median():.1%}")
print(f"  有事件股占比：fsdb {(sm.fsdb_events>0).mean():.1%}  "
      f"lake {(sm.lake_events>0).mean():.1%}")

print()
print("=" * 78)
print("② 字段映射（匹配日上的比值，期望≈1.0）")
print("=" * 78)
dr = sm.div_ratio_med.dropna()
gr = sm.give_ratio_med.dropna()
print(f"  fsdb.div / (lake.fenhong/10) : 中位 {dr.median():.4f}  "
      f"p10 {dr.quantile(.1):.4f}  p90 {dr.quantile(.9):.4f}  n={len(dr)}" if len(dr)
      else "  无可比样本")
print(f"  (fsdb.give+trans) / lake.songzhuangu : 中位 {gr.median():.4f}  n={len(gr)}"
      if len(gr) else "  无可比样本")

print()
print("=" * 78)
print("③ 单次乘数方向与量级（600519 明细）")
print("=" * 78)
fa = fs.adj_factors("600519")
lk = lake[lake["code"] == "600519"].copy()
mg = fa.merge(lk, left_on="ex_date", right_on="date", how="left")
print(mg[["ex_date", "div", "give", "trans", "mult", "cum",
          "fenhong", "songzhuangu", "peigu"]].tail(8).to_string(index=False))

print()
print("=" * 78)
print("④ fsdb cum 内部自洽性（cum_t 是否 = ∏ mult_i）")
print("=" * 78)
errs = []
for code in sample[:25]:
    try:
        fa = fs.adj_factors(code)
    except Exception:                                     # noqa: BLE001
        continue
    if len(fa) < 3:
        continue
    fa = fa.sort_values("ex_date").reset_index(drop=True)
    cum_calc = fa["mult"].cumprod()
    err = (cum_calc / fa["cum"] - 1).abs().max()
    errs.append(err)
print(f"  抽样 {len(errs)} 只，|∏mult / cum − 1| 最大值 中位 "
      f"{pd.Series(errs).median():.4f}  最大 {pd.Series(errs).max():.4f}"
      if errs else "  样本不足")

# 落报告
out = Path("reports") / "fsdb复权对账"
out.mkdir(parents=True, exist_ok=True)
sm.to_csv(out / "summary.csv", index=False, encoding="utf-8-sig")
mg.to_csv(out / "detail_600519.csv", index=False, encoding="utf-8-sig")
print(f"\n明细已落 reports/fsdb复权对账/（summary.csv, detail_600519.csv）")
