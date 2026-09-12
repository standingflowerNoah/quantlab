"""L2 因子库数据源全景扫描（as-of 影响审计 · 第一步）

对 registry 全部因子提取 SQL 的数据来源，按「as-of 污染风险」分类：
  A. 已切 PIT 股本（share_capital_daily ASOF）      → 已根治
  B. 仍引用 as-of 表且用股本/状态列                  → 高危（应 FAIL）
  C. 引用 as-of 表但只用财务科目列                   → 中危（单点截面口径）
  D. read_parquet 直读                              → PIT 由数据源逐日保证
  E. 纯行情/事件表                                   → 干净

输出：reports/factor_source_scan_20260912.json + 控制台分档表
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

ASO_TABLES = {"finance_snapshot", "instruments", "index_members", "goodwill_snapshot"}
ASO_SHARE_COLS = {"total_shares", "float_shares", "total_share", "float_share",
                  "total_mv", "float_mv", "mcap_yi", "float_mcap_yi"}
ASO_STATE_COLS = {"is_st", "industry"}
PIT_SHARE_TABLE = "share_capital_daily"


def strip_sql_comments(sql: str) -> str:
    s = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"/\*.*?\*/", " ", s, flags=re.S)


def main() -> None:
    from quantlab.factor.registry import all_factors, get_factor

    rows = []
    for name in sorted(all_factors()):
        try:
            f = get_factor(name)
        except Exception:
            continue
        rec = {"factor": name, "category": getattr(f, "category", "?"),
               "kind": "python" if not hasattr(f, "_sql") else "sql"}
        if hasattr(f, "_sql"):
            try:
                sql, _ = f._sql()
            except Exception as e:
                rec["kind"] = "sql_error"
                rec["err"] = str(e)[:80]
                rows.append(rec)
                continue
            body = strip_sql_comments(sql).lower()
            tabs = sorted({t.lower() for t in
                           re.findall(r"(?:FROM|JOIN)\s+([a-zA-Z_]\w*)", sql, re.I)})
            pq = sorted({m.group(1) for m in
                         re.finditer(r"read_parquet\(\s*'([^']+)'", sql, re.I)})
            rec["tables"] = tabs
            rec["parquet"] = [p[-60:] for p in pq]
            hits_aso = sorted(set(tabs) & ASO_TABLES)
            rec["aso_tables"] = hits_aso
            share_cols = sorted({c for c in ASO_SHARE_COLS if c in body})
            state_cols = sorted({c for c in ASO_STATE_COLS if c in body})
            rec["aso_share_cols"] = share_cols
            rec["aso_state_cols"] = state_cols
            rec["uses_pit_share"] = PIT_SHARE_TABLE in tabs
            # 分档
            if rec["uses_pit_share"]:
                rec["band"] = "A"
            elif hits_aso and (share_cols or (state_cols and "instruments" in hits_aso)):
                rec["band"] = "B"
            elif hits_aso:
                rec["band"] = "C"
            elif pq:
                rec["band"] = "D"
            else:
                rec["band"] = "E"
        rows.append(rec)

    df = pd.DataFrame(rows)
    out = Path("reports/factor_source_scan_20260912.json")
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"registry 因子总数: {len(df)}\n")
    for band, label in [("A", "A 已切 PIT 股本（根治）"),
                        ("B", "B 仍引用 as-of 表+股本/状态列（高危）"),
                        ("C", "C 引用 as-of 表（财务科目/单点截面口径）"),
                        ("D", "D read_parquet 直读（数据源逐日保证）"),
                        ("E", "E 纯行情/事件表（干净）"),
                        ("?", "其他")]:
        sub = df[df["band"] == band]
        if len(sub) == 0:
            continue
        print(f"[{label}] {len(sub)} 个")
        for _, r in sub.iterrows():
            extra = ""
            if band in ("B", "C"):
                extra = f" tables={r.get('aso_tables')} share={r.get('aso_share_cols')} state={r.get('aso_state_cols')}"
            elif band == "A":
                pass
            print(f"   {r['factor']}{extra}")
        print()


if __name__ == "__main__":
    main()
