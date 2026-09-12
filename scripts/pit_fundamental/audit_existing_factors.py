"""现有财务因子 PIT 健康审计（a-share-pit-fundamental-vintage-builder 首个落地脚本）

四项检查（对应 skill 的 Audit 路径）：
  A. finance_q 版本结构盘点 + _INCOME_LADDER ROWS 压制窗口行序风险
     （ORDER BY rp DESC 无第二排序键：同 rp 多披露版本时 ROWS 窗口可能把
      「同 rp 的更早版本」当压制源 → 更正版本被原始版本压制，行序敏感）
  B. T+1 vs 当日入账双口径差分：cfp_ttm / ocf_to_profit，
     月末调仓日全市场（2021-01 起，valuation_daily 覆盖期）
  C. SUE 族（sue_v2/sur/roe_chg 共用 _SUE_COMMON）8 期窗口 pub 顺序：
     窗口行 pub > 本期 pub 的 (code, rp) 占比 = 潜在弱泄漏暴露面
     （sue_gpoa 修复时加了 p.pub <= c.pub，SUE 族没有）
  D. restatement bias 抽样：latest-only vs PIT 在月末调仓日的 cfp_ttm 差异

只读：in-memory duckdb 直读 parquet；trade_calendar 经 READ_ONLY ATTACH。
输出：reports/pit_fundamental/existing_factors_audit.json + .md
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb

ROOT = Path(__file__).resolve().parents[2]
LAKE = os.environ.get("QUANTLAB_LAKE", str(ROOT / "data" / "lake" / "clean"))
FQ = f"{LAKE}/fundamental/finance_q"
VD = f"{LAKE}/fundamental/valuation_daily/part-*.parquet"
OUT = ROOT / "reports" / "pit_fundamental"

con = duckdb.connect()
con.execute(f"ATTACH '{ROOT / 'data' / 'quant.duckdb'}' AS ql (READ_ONLY)")
results: dict = {}


def month_end_dates() -> list:
    rows = con.execute("""
        SELECT MAX(trade_date) AS d
        FROM ql.trade_calendar
        WHERE trade_date >= DATE '2021-01-01'
        GROUP BY year(trade_date), month(trade_date)
        ORDER BY d
    """).fetchall()
    return [r[0] for r in rows]


def check_a() -> dict:
    """版本结构 + ROWS 压制窗口行序风险（income 表）"""
    total, multi = con.execute(f"""
        SELECT COUNT(*), SUM(n > 1) FROM (
            SELECT code, EndDate, COUNT(DISTINCT InfoPublDate) AS n
            FROM read_parquet('{FQ}/income/part-*.parquet', union_by_name=true)
            WHERE InfoPublDate IS NOT NULL AND InfoPublDate > EndDate
            GROUP BY code, EndDate
        )
    """).fetchone()

    # 同 rp 多版本组内：物理首行是否即最早披露（ROWS 窗口行为取决于此）
    first_ok, first_bad = con.execute(f"""
        WITH v AS (
            SELECT code, EndDate, InfoPublDate AS pub,
                   row_number() OVER () AS phys
            FROM read_parquet('{FQ}/income/part-*.parquet', union_by_name=true)
            WHERE InfoPublDate IS NOT NULL AND InfoPublDate > EndDate
        ), m AS (
            SELECT code, EndDate,
                   MIN(pub) AS first_pub,
                   arg_min(pub, phys) AS first_by_phys
            FROM v GROUP BY code, EndDate HAVING COUNT(*) > 1
        )
        SELECT
            SUM(CASE WHEN first_by_phys = first_pub THEN 1 ELSE 0 END),
            SUM(CASE WHEN first_by_phys != first_pub THEN 1 ELSE 0 END)
        FROM m
    """).fetchone()

    # ROWS 实现 vs 严格语义（MIN(pub) WHERE rp > 本行）的 kept 集合差
    kr, ks, only_strict = con.execute(f"""
        WITH base AS (
            SELECT code, EndDate AS rp, InfoPublDate AS pub
            FROM read_parquet('{FQ}/income/part-*.parquet', union_by_name=true)
            WHERE InfoPublDate IS NOT NULL AND InfoPublDate > EndDate
        ), rows_impl AS (
            SELECT *, MIN(pub) OVER (
                PARTITION BY code ORDER BY rp DESC
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ) AS later_rp_min_pub
            FROM base
        ), kept_rows AS (
            SELECT code, rp, pub FROM rows_impl
            WHERE later_rp_min_pub IS NULL OR later_rp_min_pub > pub
        ), kept_strict AS (
            SELECT b.code, b.rp, b.pub
            FROM base b
            WHERE (SELECT MIN(pub) FROM base p
                   WHERE p.code = b.code AND p.rp > b.rp)
                      IS NULL
               OR (SELECT MIN(pub) FROM base p
                   WHERE p.code = b.code AND p.rp > b.rp) > b.pub
        )
        SELECT
            (SELECT COUNT(*) FROM kept_rows),
            (SELECT COUNT(*) FROM kept_strict),
            (SELECT COUNT(*) FROM (
                SELECT code, rp, pub FROM kept_strict
                EXCEPT SELECT code, rp, pub FROM kept_rows))
    """).fetchone()
    risky = (first_bad or 0) > 0 or (only_strict or 0) > 0
    return {
        "income_vintage_rows": total,
        "income_rp_with_multiple_versions": multi or 0,
        "phys_first_row_is_earliest_pub": first_ok or 0,
        "phys_first_row_not_earliest_pub": first_bad or 0,
        "kept_rows_impl": kr or 0,
        "kept_strict_semantics": ks or 0,
        "kept_only_in_strict": only_strict or 0,
        "verdict": "warn" if risky else "pass",
    }


def check_b(month_ends: list) -> dict:
    """T+1 vs 当日入账：月末调仓日重建 cfp_ttm / ocf_to_profit 并差分"""
    ds = ",".join(f"DATE '{d}'" for d in month_ends)
    out = {}
    cases = {
        "cfp_ttm": ("g.ocf_ttm / NULLIF(v.total_mv, 0)",
                    "JOIN read_parquet('{VD}') v ON v.code = g.code "
                    "AND v.date = t.t0"),
        "ocf_to_profit": ("g.ocf_ttm / NULLIF(g.np_ttm, 0)", ""),
    }
    for fac, (expr, vjoin) in cases.items():
        vjoin = vjoin.replace("{VD}", VD)
        q = f"""
WITH ladder AS (
    SELECT c.code AS code, c.EndDate AS rp, c.InfoPublDate AS pub,
           c.NetOperateCashFlowTTM AS ocf_ttm,
           i.NPParentCompanyOwnersTTM AS np_ttm
    FROM read_parquet('{FQ}/cashflow/part-*.parquet', union_by_name=true) c
    JOIN read_parquet('{FQ}/income/part-*.parquet', union_by_name=true) i
      ON c.code = i.code AND c.EndDate = i.EndDate
        AND c.InfoPublDate = i.InfoPublDate
    WHERE i.InfoPublDate IS NOT NULL AND i.InfoPublDate > i.EndDate
      AND c.InfoPublDate IS NOT NULL AND c.InfoPublDate > c.EndDate
), cal AS (SELECT DISTINCT trade_date FROM ql.trade_calendar),
av AS (
    SELECT l.*,
           (SELECT MIN(t.trade_date) FROM cal t
            WHERE t.trade_date > l.pub) AS available_date
    FROM ladder l
), tgt AS (SELECT UNNEST([{ds}]) AS t0),
a AS (
    SELECT t.t0, g.code, {expr} AS value
    FROM tgt t
    JOIN av g ON g.pub <= t.t0
    {vjoin}
    WHERE ({expr}) IS NOT NULL AND isfinite({expr})
    QUALIFY ROW_NUMBER() OVER (PARTITION BY t.t0, g.code
                               ORDER BY g.pub DESC) = 1
), b AS (
    SELECT t.t0, g.code, {expr} AS value
    FROM tgt t
    JOIN av g ON g.available_date <= t.t0
    {vjoin}
    WHERE ({expr}) IS NOT NULL AND isfinite({expr})
    QUALIFY ROW_NUMBER() OVER (PARTITION BY t.t0, g.code
                               ORDER BY g.available_date DESC,
                               g.pub DESC) = 1
), cmp AS (
    SELECT COALESCE(a.t0, b.t0) AS t0, COALESCE(a.code, b.code) AS code,
           a.value AS va, b.value AS vb
    FROM a FULL OUTER JOIN b ON a.t0 = b.t0 AND a.code = b.code
    WHERE a.value IS DISTINCT FROM b.value
)
SELECT COUNT(*),
       SUM(CASE WHEN va IS NULL OR vb IS NULL THEN 1 ELSE 0 END),
       MAX(ABS(va - vb)),
       MAX(CASE WHEN COALESCE(ABS(va), ABS(vb)) > 0
                THEN ABS(COALESCE(va, 0) - COALESCE(vb, 0))
                     / COALESCE(ABS(va), ABS(vb)) END)
FROM cmp
"""
        r = con.execute(q).fetchone()
        out[fac] = {
            "diff_rows": r[0] or 0,
            "one_side_only_rows": r[1] or 0,
            "max_abs_diff": r[2],
            "max_rel_diff": r[3],
            "month_ends": len(month_ends),
        }
    return out


def check_c() -> dict:
    """SUE 族窗口 pub 顺序（双口径）

    kept 口径 = 因子真实语义：_SUE_COMMON 的压制剔除（later_rp_min_pub > pub）
    之后再取首次披露、按 rp 取前 8 期——理论性质：kept 行满足
    rp_p < rp_c ⟹ pub_p < pub_c（本期 rp 本身是 r_p 的 later-rp 行，
    kept 要求 later_rp_min_pub > pub_p ⟹ pub_c >= later_rp_min_pub > pub_p），
    期望坏行 = 0；非零则证明存在绕过压制的泄漏路径，须修因子。

    raw 口径 = 压制前首次披露面板（v1 审计误把此口径当因子窗口，
    5.50% 的"弱泄漏"即来自它）；它衡量的是压制逻辑守护的暴露面，
    仅作对照，不构成因子缺陷证据。
    """
    common = """
WITH inc AS (
    SELECT code, EndDate AS rp, InfoPublDate AS pub
    FROM read_parquet('{FQ}/income/part-*.parquet', union_by_name=true)
    WHERE InfoPublDate IS NOT NULL AND InfoPublDate > EndDate
){BODY}
""".replace("{FQ}", FQ)
    body_kept = """
, sup AS (
    SELECT *, MIN(pub) OVER (
        PARTITION BY code ORDER BY rp DESC
        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    ) AS later_rp_min_pub
    FROM inc
), kept AS (
    SELECT * FROM sup
    WHERE later_rp_min_pub IS NULL OR later_rp_min_pub > pub
), fp AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY code, rp ORDER BY pub) AS rk
    FROM kept
), seq AS (
    SELECT code, rp, pub,
           LAG(pub, 1) OVER w AS p1, LAG(pub, 2) OVER w AS p2,
           LAG(pub, 3) OVER w AS p3, LAG(pub, 4) OVER w AS p4,
           LAG(pub, 5) OVER w AS p5, LAG(pub, 6) OVER w AS p6,
           LAG(pub, 7) OVER w AS p7, LAG(pub, 8) OVER w AS p8
    FROM fp WHERE rk = 1
    WINDOW w AS (PARTITION BY code ORDER BY rp)
), bad AS (
    SELECT code, rp, pub,
           (CASE WHEN p1 > pub THEN 1 ELSE 0 END
          + CASE WHEN p2 > pub THEN 1 ELSE 0 END
          + CASE WHEN p3 > pub THEN 1 ELSE 0 END
          + CASE WHEN p4 > pub THEN 1 ELSE 0 END
          + CASE WHEN p5 > pub THEN 1 ELSE 0 END
          + CASE WHEN p6 > pub THEN 1 ELSE 0 END
          + CASE WHEN p7 > pub THEN 1 ELSE 0 END
          + CASE WHEN p8 > pub THEN 1 ELSE 0 END) AS n_bad
    FROM seq
)
SELECT COUNT(*) AS total_rows,
       SUM(CASE WHEN n_bad > 0 THEN 1 ELSE 0 END) AS rows_with_bad,
       SUM(n_bad) AS bad_cells
FROM bad
"""
    body_raw = """
, fp AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY code, rp ORDER BY pub) AS rk
    FROM inc
), seq AS (
    SELECT code, rp, pub,
           LAG(pub, 1) OVER w AS p1, LAG(pub, 2) OVER w AS p2,
           LAG(pub, 3) OVER w AS p3, LAG(pub, 4) OVER w AS p4,
           LAG(pub, 5) OVER w AS p5, LAG(pub, 6) OVER w AS p6,
           LAG(pub, 7) OVER w AS p7, LAG(pub, 8) OVER w AS p8
    FROM fp WHERE rk = 1
    WINDOW w AS (PARTITION BY code ORDER BY rp)
), bad AS (
    SELECT code, rp, pub,
           (CASE WHEN p1 > pub THEN 1 ELSE 0 END
          + CASE WHEN p2 > pub THEN 1 ELSE 0 END
          + CASE WHEN p3 > pub THEN 1 ELSE 0 END
          + CASE WHEN p4 > pub THEN 1 ELSE 0 END
          + CASE WHEN p5 > pub THEN 1 ELSE 0 END
          + CASE WHEN p6 > pub THEN 1 ELSE 0 END
          + CASE WHEN p7 > pub THEN 1 ELSE 0 END
          + CASE WHEN p8 > pub THEN 1 ELSE 0 END) AS n_bad
    FROM seq
)
SELECT COUNT(*) AS total_rows,
       SUM(CASE WHEN n_bad > 0 THEN 1 ELSE 0 END) AS rows_with_bad,
       SUM(n_bad) AS bad_cells
FROM bad
"""
    def run(body: str) -> tuple:
        q = common.replace("{BODY}", body)
        return con.execute(q).fetchone()

    kt, kb, kc = run(body_kept)
    rt, rb, rc = run(body_raw)
    return {
        "kept_rows_total": kt or 0,
        "kept_rows_window_contains_later_pub": kb or 0,
        "kept_bad_cells": kc or 0,
        "kept_pct": (kb or 0) / max(kt or 1, 1),
        "kept_verdict": "pass" if (kb or 0) == 0 else "FAIL",
        "raw_rows_total": rt or 0,
        "raw_rows_window_contains_later_pub": rb or 0,
        "raw_bad_cells": rc or 0,
        "raw_pct": (rb or 0) / max(rt or 1, 1),
        "note": ("kept=因子真实语义（压制后），坏行必须为 0；"
                 "raw=压制前对照（压制逻辑守护的暴露面，非因子缺陷）"),
    }


def check_d(month_ends: list) -> dict:
    """restatement bias：latest-only vs PIT 的 cfp_ttm 差异"""
    ds = ",".join(f"DATE '{d}'" for d in month_ends)
    q = f"""
WITH ladder AS (
    SELECT code, EndDate AS rp, InfoPublDate AS pub,
           NetOperateCashFlowTTM AS ocf_ttm
    FROM read_parquet('{FQ}/cashflow/part-*.parquet', union_by_name=true)
    WHERE InfoPublDate IS NOT NULL AND InfoPublDate > EndDate
), tgt AS (SELECT UNNEST([{ds}]) AS t0),
pit AS (
    -- PIT 正确口径：更正披露压制（later_rp_min_pub > pub）后 ASOF
    SELECT t.t0, g.code, g.ocf_ttm / NULLIF(v.total_mv, 0) AS value
    FROM tgt t
    JOIN ladder g ON g.pub <= t.t0
    JOIN read_parquet('{VD}') v ON v.code = g.code AND v.date = t.t0
    WHERE g.ocf_ttm IS NOT NULL AND v.total_mv > 0
      AND isfinite(g.ocf_ttm / v.total_mv)
      AND (SELECT MIN(pub) FROM ladder p
           WHERE p.code = g.code AND p.rp > g.rp AND p.pub <= t.t0)
             IS NULL   -- 无更晚报告期在 t0 前已披露（压制语义的 as-of 形式）
    QUALIFY ROW_NUMBER() OVER (PARTITION BY t.t0, g.code
                               ORDER BY g.pub DESC) = 1
), latest AS (
    -- latest-only 对照：不做压制，其余同 pit
    SELECT t.t0, g.code, g.ocf_ttm / NULLIF(v.total_mv, 0) AS value
    FROM tgt t
    JOIN ladder g ON g.pub <= t.t0
    JOIN read_parquet('{VD}') v ON v.code = g.code AND v.date = t.t0
    WHERE g.ocf_ttm IS NOT NULL AND v.total_mv > 0
      AND isfinite(g.ocf_ttm / v.total_mv)
    QUALIFY ROW_NUMBER() OVER (PARTITION BY t.t0, g.code
                               ORDER BY g.pub DESC) = 1
)
SELECT
    (SELECT COUNT(*) FROM (
        SELECT t0, code, value FROM latest
        EXCEPT SELECT t0, code, value FROM pit)) AS lo_only,
    (SELECT COUNT(*) FROM (
        SELECT t0, code, value FROM pit
        EXCEPT SELECT t0, code, value FROM latest)) AS pit_only,
    (SELECT COUNT(*) FROM latest) AS n_latest,
    (SELECT COUNT(*) FROM pit) AS n_pit,
    (SELECT MAX(ABS(l.value - p.value)) FROM latest l
        JOIN pit p ON l.t0 = p.t0 AND l.code = p.code
        WHERE l.value IS DISTINCT FROM p.value) AS max_abs,
    (SELECT AVG(ABS(l.value - p.value)) FROM latest l
        JOIN pit p ON l.t0 = p.t0 AND l.code = p.code
        WHERE l.value IS DISTINCT FROM p.value) AS mean_abs
"""
    r = con.execute(q).fetchone()
    return {
        "latest_only_rows": r[0] or 0,
        "pit_only_rows": r[1] or 0,
        "n_latest": r[2] or 0,
        "n_pit": r[3] or 0,
        "max_abs_diff": r[4],
        "mean_abs_diff": r[5],
        "note": ("latest-only（不做更正披露压制）vs PIT（压制）在月末调仓日"
                 "的差异暴露面；即压制逻辑守护的行数"),
    }


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["A", "B", "C", "D"],
                    help="只跑单项检查，结果写独立文件（不覆盖全量报告）")
    args = ap.parse_args()

    if args.only == "C":
        OUT.mkdir(parents=True, exist_ok=True)
        print("[audit] C(v2). SUE 族窗口 pub 顺序（kept 因子真实语义 + raw 对照）...",
              flush=True)
        c = check_c()
        (OUT / "existing_factors_audit_C_v2.json").write_text(
            json.dumps(c, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        print(json.dumps(c, ensure_ascii=False, indent=2), flush=True)
        return

    OUT.mkdir(parents=True, exist_ok=True)
    me = month_end_dates()
    print(f"[audit] 月末调仓日 {len(me)} 个（{me[0]} ~ {me[-1]}）", flush=True)

    print("[audit] A. 版本结构 + ROWS 压制窗口行序风险 ...", flush=True)
    results["A_version_structure"] = check_a()

    print("[audit] B. T+1 vs 当日入账差分（cfp_ttm / ocf_to_profit）...",
          flush=True)
    results["B_t1_vs_same_day"] = check_b(me)

    print("[audit] C. SUE 族窗口 pub 顺序 ...", flush=True)
    results["C_sue_window_pub_order"] = check_c()

    print("[audit] D. restatement bias（latest-only vs PIT）...", flush=True)
    results["D_restatement_bias"] = check_d(me)

    (OUT / "existing_factors_audit.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")

    a = results["A_version_structure"]
    b = results["B_t1_vs_same_day"]
    c = results["C_sue_window_pub_order"]
    d = results["D_restatement_bias"]
    lines = [
        "# 现有财务因子 PIT 健康审计",
        "",
        "审计者：a-share-pit-fundamental-vintage-builder（Audit 路径，首个落地脚本）",
        f"范围：finance_q 全 vintage + 月末调仓日 {len(me)} 个"
        f"（{me[0]} ~ {me[-1]}）全市场，只读",
        "",
        "## A. 版本结构与 ROWS 压制窗口（income 表）",
        f"- vintage 行 {a['income_vintage_rows']}，同 rp 多版本组 "
        f"{a['income_rp_with_multiple_versions']}",
        f"- 组内物理首行 = 最早披露：{a['phys_first_row_is_earliest_pub']}；"
        f"≠：{a['phys_first_row_not_earliest_pub']}",
        f"- ROWS 实现 kept {a['kept_rows_impl']} 行 vs 严格语义 "
        f"{a['kept_strict_semantics']} 行（严格语义多保留 "
        f"{a['kept_only_in_strict']} 行）",
        f"- **判定：{a['verdict'].upper()}**",
        "",
        "## B. T+1 vs 当日入账（月末调仓日全市场）",
    ]
    for fac, dd in b.items():
        lines.append(
            f"- {fac}: 差异行 {dd['diff_rows']}（单侧 {dd['one_side_only_rows']}），"
            f"max_abs={dd['max_abs_diff']}, max_rel={dd['max_rel_diff']}")
    lines += [
        "",
        "## C. SUE 族窗口 pub 顺序（v2 双口径）",
        f"- kept（因子真实语义，压制后）：坏行 {c['kept_rows_window_contains_later_pub']}"
        f" / {c['kept_rows_total']}（{c['kept_pct']:.4%}）→ **{c['kept_verdict']}**",
        f"- raw（压制前对照）：坏行 {c['raw_rows_window_contains_later_pub']}"
        f" / {c['raw_rows_total']}（{c['raw_pct']:.4%}）——压制逻辑守护的暴露面",
        f"- **判定：因子窗口 {c['kept_verdict'].upper()}**",
        "",
        "## D. restatement bias（latest-only 无压制 vs PIT 有压制，cfp_ttm）",
        f"- latest-only 行 {d['n_latest']} / PIT 行 {d['n_pit']}；"
        f"仅 latest 有 {d['latest_only_rows']}，仅 PIT 有 {d['pit_only_rows']}",
        f"- 值差异：mean_abs={d['mean_abs_diff']}, max_abs={d['max_abs_diff']}",
        "",
        "## 结论",
        "三项检查直接对应 factor/library/fundamental.py 的三类实现"
        "（INCOME_LADDER / FIN_LADDER / SUE_COMMON）；数字为判定证据，"
        "修复建议见审计对话与 JSON。",
    ]
    (OUT / "existing_factors_audit.md").write_text(
        "\n".join(lines), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2, default=str),
          flush=True)


if __name__ == "__main__":
    main()
