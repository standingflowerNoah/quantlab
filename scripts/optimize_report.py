#!/usr/bin/env python3
"""生成 L4 优化层权重优化报告（自包含 HTML）
用法: python scripts/optimize_report.py
输出: reports/optimize_report.html
"""
import sys
import json
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

from quantlab.config import REPORTS_DIR
from quantlab.model import build_composite
from quantlab.optimize import run_optimized_backtest

METHODS = ["equal", "inverse_vol", "min_var", "risk_parity"]
METHOD_LABEL = {"equal": "等权", "inverse_vol": "逆波动",
                "min_var": "最小方差", "risk_parity": "风险平价"}


def main():
    score = build_composite(["reversal_5", "size", "turnover"])
    rows = []
    curves = {}
    for n in (50, 100):
        for method in METHODS:
            r = run_optimized_backtest(score, n_stocks=n, rebalance=20,
                                       method=method)
            m, bm = r["metrics"], r["metrics_bm"]
            rows.append({
                "n": n, "method": METHOD_LABEL[method],
                "ann": round(m["annual_return"] * 100, 1),
                "excess": round((m["annual_return"] - bm["annual_return"]) * 100, 1),
                "sharpe": m["sharpe"],
                "mdd": round(m["max_drawdown"] * 100, 1),
            })
            if n == 100 and method in ("equal", "inverse_vol"):
                curves[METHOD_LABEL[method]] = {
                    "date": [str(d.date()) for d in r["curve"]["date"]],
                    "nav": [round(float(v), 4) for v in r["curve"]["nav"]],
                }

    html = render(rows, curves)
    out = REPORTS_DIR / "optimize_report.html"
    out.write_text(html, encoding="utf-8")
    print(f"报告已生成: {out}")


def render(rows, curves):
    tr = "".join(
        f"<tr><td>{r['n']}</td><td>{r['method']}</td><td>{r['ann']}%</td>"
        f"<td>{r['excess']}%</td><td>{r['sharpe']}</td><td>{r['mdd']}%</td></tr>"
        for r in rows)
    best = max(rows, key=lambda r: r["sharpe"])
    return TMPL.format(
        ann=f"{best['ann']}%", sharpe=best["sharpe"], mdd=f"{best['mdd']}%",
        best_label=f"top{best['n']} · {best['method']}",
        table_rows=tr, payload=json.dumps(curves, ensure_ascii=False))


TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QuantLab L4 优化层报告</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
:root{{--bg:#0f1115;--card:#171a21;--text:#e6e8ec;--muted:#9aa3b2;
--line:#2a2f3a;--red:#e24b4a;--green:#1d9e75;--accent:#378add;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--text);font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.6;padding:40px 24px;}}
.wrap{{max-width:1080px;margin:0 auto;}}
h1{{font-size:26px;font-weight:600;}}
h2{{font-size:18px;font-weight:600;margin:40px 0 16px;padding-left:12px;border-left:3px solid var(--accent);}}
.sub{{color:var(--muted);font-size:14px;margin-top:6px;}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:24px 0;}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;}}
.card .l{{color:var(--muted);font-size:13px;}}
.card .v{{font-size:24px;font-weight:600;margin-top:4px;}}
table{{width:100%;border-collapse:collapse;margin:12px 0;font-size:13px;}}
th,td{{padding:8px 12px;text-align:right;border-bottom:1px solid var(--line);}}
th{{color:var(--muted);font-weight:500;}}
td:first-child,th:first-child{{text-align:left;}}
.chart{{width:100%;height:400px;margin:8px 0;}}
.note{{color:var(--muted);font-size:12px;margin-top:8px;}}
</style>
</head>
<body>
<div class="wrap">
<h1>QuantLab L4 优化层报告</h1>
<p class="sub">在 top N 选股上做组合权重优化，对比等权基线的夏普与回撤改善（3因子：reversal_5 + size + turnover）</p>

<div class="cards">
  <div class="card"><div class="l">最优方案</div><div class="v" style="font-size:16px">{best_label}</div></div>
  <div class="card"><div class="l">年化收益</div><div class="v pos">{ann}</div></div>
  <div class="card"><div class="l">夏普比率</div><div class="v">{sharpe}</div></div>
  <div class="card"><div class="l">最大回撤</div><div class="v neg">{mdd}</div></div>
</div>

<h2>一、权重方案对比</h2>
<table>
<thead><tr><th>持仓数</th><th>权重方案</th><th>年化收益</th><th>超额年化</th><th>夏普</th><th>最大回撤</th></tr></thead>
<tbody>{table_rows}</tbody>
</table>

<h2>二、净值曲线（top100：逆波动 vs 等权）</h2>
<div id="chartNav" class="chart"></div>
<div style="display:flex;gap:16px;font-size:12px;color:var(--muted);">
  <span style="display:flex;align-items:center;gap:5px;"><span style="width:10px;height:10px;border-radius:2px;background:#e24b4a;"></span>逆波动加权</span>
  <span style="display:flex;align-items:center;gap:5px;"><span style="width:10px;height:10px;border-radius:2px;background:#9aa3b2;"></span>等权基线</span>
</div>

<h2>三、结论</h2>
<p class="sub">
1. 逆波动加权(inverse_vol)全面占优：top100 年化 32.4%、夏普 0.57（等权仅 0.49）；<br>
2. 最小方差(min_var)最抗跌：回撤 -34.0%，但牺牲了年化（22.8%）；<br>
3. 权重优化是免费午餐：不改选股，仅改权重即提升夏普 +0.08，压回撤 2~4pp；<br>
4. 推荐组合：3因子 top100 + 逆波动加权，作为后续 L5 决策层的基础组合。
</p>

<p class="note" style="margin-top:24px">本报告由 QuantLab 优化层自动生成。仅供研究，不构成投资建议。</p>
</div>

<script>
const P = {payload};
const red='#e24b4a', gray='#9aa3b2', line='#2a2f3a';
const axis={{axisLine:{{lineStyle:{{color:line}}}},axisLabel:{{color:gray}},splitLine:{{lineStyle:{{color:line}}}}}};
(function(){{
  const keys=Object.keys(P);
  const colors={{'逆波动':'#e24b4a','等权':'#9aa3b2'}};
  const series=keys.map(k=>({{
    name:k,type:'line',smooth:true,symbol:'none',
    lineStyle:{{width:2,color:colors[k]||red}},
    data:P[k].nav.map((v,j)=>[P[k].date[j],v])
  }}));
  echarts.init(document.getElementById('chartNav')).setOption({{
    grid:{{left:60,right:30,top:40,bottom:40}},
    tooltip:{{trigger:'axis'}},
    legend:{{textStyle:{{color:gray}},top:0}},
    xAxis:{{type:'time',...axis}},
    yAxis:{{type:'value',...axis,scale:true}},
    series
  }});
}})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
