"""因子审查批量 CLI + HTML 报告
用法:
  python scripts/factor_audit.py all [--quick] [--force]   # 全量（registry deep + alpha191 light）
  python scripts/factor_audit.py --names size,dragon_net_20 [--force]
  python scripts/factor_audit.py report                    # 仅重新生成 HTML 报告
"""
from __future__ import annotations

import sys
import json
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd


def run_audit(args: list[str]):
    from quantlab.factor.audit import audit_all
    force = "--force" in args
    quick = "--quick" in args
    names = None
    if "--names" in args:
        i = args.index("--names")
        names = [s for s in args[i + 1].split(",") if s]
    df = audit_all(names=names, force=force, quick=quick)
    print()
    print("=== 审查汇总 ===")
    print(df["verdict"].value_counts().to_string())
    bad = df[df["verdict"] == "FAIL"]
    if len(bad):
        print("\nFAIL 明细:")
        print(bad[["factor", "issues"]].to_string(index=False))
    warn = df[df["verdict"] == "WARN"]
    if len(warn):
        print(f"\nWARN {len(warn)} 个（首个）:")
        print(warn[["factor", "issues"]].head(5).to_string(index=False))


def generate_report():
    from quantlab.factor.audit import load_all_records
    df = load_all_records()
    if df.empty:
        print("无审查记录")
        return

    df = df.sort_values(["verdict", "factor"], ascending=[True, True])
    counts = df["verdict"].value_counts().to_dict()
    n_total = len(df)

    cls_order = {"FAIL": 0, "WARN": 1, "PASS": 2}
    df["_o"] = df["verdict"].map(cls_order)
    df = df.sort_values(["_o", "factor"]).drop(columns="_o")

    rows_html = []
    for _, r in df.iterrows():
        ic5 = f"{r['ic5']:+.4f}" if pd.notna(r.get("ic5")) else "-"
        icir = f"{r['icir20']:+.2f}" if pd.notna(r.get("icir20")) else "-"
        cov = f"{int(r['codes_per_day_median']):,}" if pd.notna(r.get("codes_per_day_median")) else "-"
        rows_html.append(
            f"<tr class='v-{r['verdict'].lower()}'>"
            f"<td><code>{r['factor']}</code></td><td>{r['category']}</td>"
            f"<td><span class='badge {r['verdict'].lower()}'>{r['verdict']}</span></td>"
            f"<td>{r.get('n_rows') or '-':,}</td>"
            f"<td class='muted'>{r.get('latest_date') or '-'}</td>"
            f"<td>{cov}</td><td>{ic5}</td><td>{icir}</td>"
            f"<td class='muted'>{r['pit']}</td>"
            f"<td class='issues'>{r['issues'] or '-'}</td></tr>")

    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>因子审查报告</title>
<style>
body{{font-family:'Segoe UI','Microsoft YaHei',sans-serif;margin:0;background:#f6f8fa;color:#1f2328}}
.wrap{{max-width:1280px;margin:0 auto;padding:24px}}
h1{{font-size:22px}} h2{{font-size:16px;margin-top:28px}}
.cards{{display:flex;gap:14px;margin:16px 0}}
.card{{flex:1;background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:14px 18px}}
.card b{{font-size:26px;display:block}}
.card.pass b{{color:#1a7f37}} .card.warn b{{color:#9a6700}} .card.fail b{{color:#cf222e}}
table{{border-collapse:collapse;width:100%;background:#fff;font-size:12.5px}}
th,td{{border:1px solid #d0d7de;padding:5px 8px;text-align:left}}
th{{background:#f6f8fa;position:sticky;top:0}}
tr.v-fail{{background:#ffebe9}} tr.v-warn{{background:#fff8c5}}
.badge{{padding:1px 8px;border-radius:10px;font-weight:600;font-size:11px}}
.badge.pass{{background:#dafbe1;color:#1a7f37}}
.badge.warn{{background:#fff8c5;color:#9a6700}}
.badge.fail{{background:#ffebe9;color:#cf222e}}
.muted{{color:#656d76}} .issues{{font-size:11.5px;color:#57606a;max-width:340px}}
code{{background:#eff1f3;padding:1px 4px;border-radius:4px}}
.note{{background:#fff;border-left:4px solid #0969da;padding:10px 14px;font-size:13px;margin:14px 0}}
</style></head><body><div class="wrap">
<h1>因子审查报告</h1>
<div class="note">构建即审查：因子入库时自动执行（结构 / 分布 / 覆盖 / IC / PIT 穿越自检）。
<b>PIT 检验</b>把底层数据截断到历史时点 t0 重算因子并与全量版对比——任何不一致即判定穿越。
首次构建为 deep 全审；每日增量仅轻量复审，PIT/IC 结论复用。
生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
<div class="cards">
<div class="card pass"><b>{counts.get('PASS', 0)}</b>PASS</div>
<div class="card warn"><b>{counts.get('WARN', 0)}</b>WARN</div>
<div class="card fail"><b>{counts.get('FAIL', 0)}</b>FAIL</div>
<div class="card"><b>{n_total}</b>因子总数</div>
</div>
<h2>审查明细</h2>
<table>
<thead><tr><th>因子</th><th>类别</th><th>结论</th><th>行数</th><th>最新日期</th>
<th>截面覆盖中位</th><th>IC(5日)</th><th>ICIR(20日)</th><th>PIT</th><th>问题</th></tr></thead>
<tbody>{''.join(rows_html)}</tbody>
</table>
</div></body></html>"""
    out = Path("reports/factor_audit.html")
    out.write_text(html, encoding="utf-8")
    print(f"报告已生成: {out}（{n_total} 个因子）")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    if not args or args[0] == "all":
        run_audit(args)
        generate_report()
    elif args[0] == "report":
        generate_report()
    else:
        run_audit(args)
        generate_report()
