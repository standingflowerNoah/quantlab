# -*- coding: utf-8 -*-
"""从 panda_factors_all.json 筛选 |Rank_IC| > 0.03 的因子（各区间 + 并集）。

用法:
  python panda_pull/filter_rankic.py
"""
import csv
import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IN_JSON = os.path.join(BASE_DIR, "reports", "pandaaiquant_factor_center",
                       "panda_factors_all.json")
OUT_DIR = os.path.dirname(IN_JSON)
PERIODS = ["ONE_YEAR", "HALF_YEAR", "TWO_YEARS", "THREE_YEARS"]
PERIOD_CN = {"HALF_YEAR": "近半年", "ONE_YEAR": "近一年", "TWO_YEARS": "近两年",
             "THREE_YEARS": "近三年"}
THRESH = 0.03


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fmt(x, nd=4):
    v = num(x)
    return "" if v is None else f"{v:.{nd}f}"


def main():
    with open(IN_JSON, encoding="utf-8") as f:
        raw = json.load(f)
    all_data = raw["data"]

    merged = {}
    for p in PERIODS:
        for r in all_data[p]:
            k = r.get("factorCode") or r.get("name")
            if k is None:
                continue
            m = merged.setdefault(k, {
                "factor_code": k,
                "factor_name": r.get("factorName") or r.get("name") or k,
                "category": r.get("categoryName") or "",
                "description": (r.get("description") or "").strip(),
            })
            m[f"rank_ic_{p}"] = num(r.get("rankIc"))
            m[f"ic_mean_{p}"] = num(r.get("icMean"))
            m[f"ic_ir_{p}"] = num(r.get("icIr"))
            m[f"ic_std_{p}"] = num(r.get("icStd"))
            m[f"annual_{p}"] = num(r.get("annualizedReturn"))
            m[f"mdd_{p}"] = num(r.get("maximumDrawdown"))
            m[f"sharpe_{p}"] = num(r.get("sharpeRatio"))
            m[f"turn_{p}"] = num(r.get("turnoverRate"))
            m[f"update_{p}"] = r.get("dataDate") or r.get("updateDate") or ""

    base_cols = ["factor_code", "factor_name", "category", "description"]
    metric_cols = []
    for p in PERIODS:
        metric_cols += [f"rank_ic_{p}", f"ic_mean_{p}", f"ic_ir_{p}", f"ic_std_{p}",
                        f"annual_{p}", f"mdd_{p}", f"sharpe_{p}", f"turn_{p}"]
    cols = base_cols + metric_cols

    def write_csv(path, rows):
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for m in rows:
                w.writerow({c: m.get(c, "") for c in cols})

    hits_by_period = {}
    for p in PERIODS:
        hits = [m for m in merged.values()
                if m.get(f"rank_ic_{p}") is not None
                and abs(m[f"rank_ic_{p}"]) > THRESH]
        hits.sort(key=lambda m: -abs(m[f"rank_ic_{p}"]))
        hits_by_period[p] = hits

    union_codes = []
    for p in PERIODS:
        for m in hits_by_period[p]:
            if m["factor_code"] not in union_codes:
                union_codes.append(m["factor_code"])
    union = [merged[c] for c in union_codes]
    write_csv(os.path.join(OUT_DIR, "panda_factors_rank_ic_abs_gt_003_any_period.csv"), union)

    default_hits = hits_by_period["ONE_YEAR"]
    write_csv(os.path.join(OUT_DIR, "panda_factors_rank_ic_abs_gt_003_default_view.csv"),
              default_hits)

    lines = []
    ap = lines.append
    ap("# PandaAI 因子中心：|Rank_IC| > 0.03 因子筛选报告")
    ap("")
    ap("- 数据源：`panda_factors_all.json`（2026-09-13 拉取，全A股，608 因子 × 4 区间）")
    ap(f"- 门槛：**|Rank_IC| > {THRESH}**（取绝对值；Rank_IC<0 的因子为反向方向，已在表中保留符号）")
    ap("")
    ap("## 各区间命中数概览")
    ap("")
    ap("| 回测区间 | 因子数 | \\|Rank_IC\\|>0.03 | 命中率 | 正向/负向 | 最高 \\|Rank_IC\\| |")
    ap("|---|---|---|---|---|---|")
    for p in PERIODS:
        vals = [m[f"rank_ic_{p}"] for m in merged.values() if m.get(f"rank_ic_{p}") is not None]
        hi = max((abs(v) for v in vals), default=0)
        pos = sum(1 for m in hits_by_period[p] if m[f"rank_ic_{p}"] > 0)
        neg = len(hits_by_period[p]) - pos
        ap(f"| {PERIOD_CN[p]} | {len(vals)} | {len(hits_by_period[p])} | "
           f"{len(hits_by_period[p]) / max(len(vals), 1) * 100:.1f}% | {pos}/{neg} | {hi:.4f} |")
    ap("")
    ap(f"> 站点默认视图 = 全A股 + 近一年：**{len(default_hits)}** 个达标；"
       f"任一区间达标（去重）共 **{len(union)}** 个。")
    ap("")

    for p in PERIODS:
        hits = hits_by_period[p]
        ap(f"## {PERIOD_CN[p]}：|Rank_IC| > {THRESH}（{len(hits)} 个）")
        ap("")
        if not hits:
            ap("（无）")
            ap("")
            continue
        ap("| # | 因子 | 分类 | Rank_IC | IC_MEAN | IC_IR | IC_STD | 年化% | 回撤% | 夏普 | 换手% | 方向 |")
        ap("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for i, m in enumerate(hits, 1):
            ric = m[f"rank_ic_{p}"]
            d = "正向" if ric > 0 else "**反向**"
            ap(f"| {i} | {m['factor_name']} (`{m['factor_code']}`) | {m['category']} "
               f"| {fmt(ric)} | {fmt(m[f'ic_mean_{p}'])} | {fmt(m[f'ic_ir_{p}'])} "
               f"| {fmt(m[f'ic_std_{p}'])} | {fmt(m[f'annual_{p}'], 2)} | {fmt(m[f'mdd_{p}'], 2)} "
               f"| {fmt(m[f'sharpe_{p}'], 2)} | {fmt(m[f'turn_{p}'], 2)} | {d} |")
        ap("")

    ap("## 备注")
    ap("")
    ap("- Rank_IC 为站点口径（日度秩相关 IC 均值），与 QuantLab 湖内 rank IC 口径可能不同（未公开平滑/去噪细节）。")
    ap("- 反向因子（Rank_IC<0）在站点分层回测中按原始方向计算年化（大概率做空方向），直接多头使用需反向打分。")
    ap("- 与 IC_MEAN>=0.03 结果对照：任一区间 |Rank_IC|>0.03 共 "
       f"{len(union)} 个（IC_MEAN 口径为 44 个）。")
    ap("")

    md_path = os.path.join(OUT_DIR, "PANDA_RANK_IC_REPORT.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[out] union: {len(union)} | default(ONE_YEAR): {len(default_hits)}")
    print(f"[out] {md_path}")
    for p in PERIODS:
        print(f"[stat] {PERIOD_CN[p]}: {len(hits_by_period[p])} 个 |Rank_IC|>{THRESH}")


if __name__ == "__main__":
    main()
