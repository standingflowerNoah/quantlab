# -*- coding: utf-8 -*-
"""从 pull_factors.py 的原始 JSON 构建筛选报告。

用法:
  python panda_pull/build_report.py
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
            m[f"ic_mean_{p}"] = num(r.get("icMean"))
            m[f"rank_ic_{p}"] = num(r.get("rankIc"))
            m[f"ic_ir_{p}"] = num(r.get("icIr"))
            m[f"ic_std_{p}"] = num(r.get("icStd"))
            m[f"annual_{p}"] = num(r.get("annualizedReturn"))
            m[f"mdd_{p}"] = num(r.get("maximumDrawdown"))
            m[f"sharpe_{p}"] = num(r.get("sharpeRatio"))
            m[f"turn_{p}"] = num(r.get("turnoverRate"))
            m[f"start_{p}"] = r.get("startDate") or ""
            m[f"update_{p}"] = r.get("dataDate") or r.get("updateDate") or ""

    base_cols = ["factor_code", "factor_name", "category", "description"]
    metric_cols = []
    for p in PERIODS:
        metric_cols += [f"ic_mean_{p}", f"rank_ic_{p}", f"ic_ir_{p}", f"ic_std_{p}",
                        f"annual_{p}", f"mdd_{p}", f"sharpe_{p}", f"turn_{p}"]
    cols = base_cols + metric_cols

    def write_csv(path, rows):
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for m in rows:
                w.writerow({c: m.get(c, "") for c in cols})

    all_rows = sorted(merged.values(), key=lambda m: m["factor_code"])
    all_csv = os.path.join(OUT_DIR, "panda_factors_all.csv")
    write_csv(all_csv, all_rows)

    hits_by_period = {}
    for p in PERIODS:
        hits = [m for m in merged.values()
                if (m.get(f"ic_mean_{p}") or -9) >= THRESH]
        hits.sort(key=lambda m: -(m[f"ic_mean_{p}"] or 0))
        hits_by_period[p] = hits

    union_codes = []
    for p in PERIODS:
        for m in hits_by_period[p]:
            if m["factor_code"] not in union_codes:
                union_codes.append(m["factor_code"])
    union = [merged[c] for c in union_codes]
    union_csv = os.path.join(OUT_DIR, "panda_factors_ic_mean_ge_003_any_period.csv")
    write_csv(union_csv, union)

    default_hits = hits_by_period["ONE_YEAR"]
    default_csv = os.path.join(OUT_DIR, "panda_factors_ic_mean_ge_003_default_view.csv")
    write_csv(default_csv, default_hits)

    # ---------- Markdown 报告 ----------
    lines = []
    ap = lines.append
    ap("# PandaAI 因子中心：IC_MEAN >= 0.03 因子收集报告")
    ap("")
    ap(f"- 数据源：https://www.pandaaiquant.com/quantfactor-center （官方接口 `/pandaApi/factorCenter/getQuantFactorCenterData`）")
    ap(f"- 抓取时间：2026-09-13 01:30 左右；股票池：全A股（A）；共 **{len(merged)}** 个因子，4 个回测区间全量拉取")
    ap(f"- 门槛：IC_MEAN >= {THRESH}（官方口径，未做任何加工）")
    ap("")
    ap("## 各区间命中数概览")
    ap("")
    ap("| 回测区间 | 因子数 | IC_MEAN>=0.03 | 命中率 | 最高 IC_MEAN |")
    ap("|---|---|---|---|---|")
    for p in PERIODS:
        vals = [m[f"ic_mean_{p}"] for m in merged.values() if m.get(f"ic_mean_{p}") is not None]
        hi = max(vals) if vals else 0
        ap(f"| {PERIOD_CN[p]} | {len(vals)} | {len(hits_by_period[p])} | "
           f"{len(hits_by_period[p]) / max(len(vals), 1) * 100:.1f}% | {hi:.4f} |")
    ap("")
    ap(f"> 站点默认视图 = 全A股 + **近一年**。该口径下仅 **{len(default_hits)}** 个因子达标；"
       f"任一区间达标（去重）共 **{len(union)}** 个。")
    ap("")

    def table(p):
        ap(f"## {PERIOD_CN[p]}：IC_MEAN >= {THRESH}（{len(hits_by_period[p])} 个）")
        ap("")
        if not hits_by_period[p]:
            ap("（无）")
            ap("")
            return
        ap("| # | 因子 | 分类 | IC_MEAN | Rank_IC | IC_IR | IC_STD | 年化% | 最大回撤% | 夏普 | 换手% |")
        ap("|---|---|---|---|---|---|---|---|---|---|---|")
        for i, m in enumerate(hits_by_period[p], 1):
            ap(f"| {i} | {m['factor_name']} (`{m['factor_code']}`) | {m['category']} "
               f"| {fmt(m[f'ic_mean_{p}'])} | {fmt(m[f'rank_ic_{p}'])} | {fmt(m[f'ic_ir_{p}'])} "
               f"| {fmt(m[f'ic_std_{p}'])} | {fmt(m[f'annual_{p}'], 2)} | {fmt(m[f'mdd_{p}'], 2)} "
               f"| {fmt(m[f'sharpe_{p}'], 2)} | {fmt(m[f'turn_{p}'], 2)} |")
        ap("")

    for p in ["ONE_YEAR", "HALF_YEAR", "TWO_YEARS", "THREE_YEARS"]:
        table(p)

    ap("## 备注")
    ap("")
    ap("- 指标为站点展示口径：IC_MEAN=日度 IC 均值；Rank_IC=秩相关 IC；IC_IR=IC均值/IC标准差；"
       "年化/回撤/夏普/换手为站点给出的分层回测值（口径未公开，仅作参考）。")
    ap("- 官方 IC 数字未说明去噪与费用处理，且平台因子多为一价量短周期组合，直接与 QuantLab 湖内因子"
       "对比需先统一股票池与区间。")
    ap("- 原始数据：`panda_factors_all.json`（含 sourceType/inFactorPool 等全部字段）；"
       "宽表：`panda_factors_all.csv`（608 行 × 36 列，utf-8-sig 可直接开 Excel）。")
    ap("")

    md_path = os.path.join(OUT_DIR, "PANDA_FACTORS_REPORT.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[out] {all_csv}")
    print(f"[out] {union_csv} ({len(union)})")
    print(f"[out] {default_csv} ({len(default_hits)})")
    print(f"[out] {md_path}")
    for p in PERIODS:
        print(f"[stat] {PERIOD_CN[p]}: {len(hits_by_period[p])} 个达标")


if __name__ == "__main__":
    main()
