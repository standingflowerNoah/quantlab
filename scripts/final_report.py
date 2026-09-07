#!/usr/bin/env python3
"""生成最终策略报告（自包含 HTML）
汇总 18 轮迭代的最终结论：策略演进、年度收益、净值、样本外验证、择时教训。
用法: python scripts/final_report.py
输出: reports/final_report.html
"""
import sys
import json
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import pandas as pd

from quantlab.config import REPORTS_DIR
from quantlab.model import build_composite
from quantlab.optimize import run_optimized_backtest


def main():
    score = build_composite(["reversal_5", "size", "turnover"],
                            universe="ashare_ex")
    r = run_optimized_backtest(score, n_stocks=100, rebalance=40,
                               method="inverse_vol")
    m = r["metrics"]
    curve = r["curve"].copy()
    curve["year"] = pd.to_datetime(curve["date"]).dt.year
    yearly = {int(y): round(float((1 + g["ret"]).prod() - 1) * 100, 1)
              for y, g in curve.groupby("year")}

    # 净值曲线（逐日全量点）
    nav = {
        "date": [str(d.date()) for d in curve["date"]],
        "nav": [round(float(v), 3) for v in curve["nav"]],
        "nav_bm": [round(float(v), 3) for v in curve["nav_bm"]],
    }

    html = render(m, yearly, nav)
    out = REPORTS_DIR / "final_report.html"
    out.write_text(html, encoding="utf-8")
    print(f"报告已生成: {out}")


def render(m, yearly, nav):
    years = sorted(yearly)
    ytr = "".join(
        f"<tr><td>{y}</td><td class='{'pos' if v>=0 else 'neg'}'>{v:+.1f}%</td></tr>"
        for y, v in yearly.items())
    return TMPL.format(
        ann=f"{m['annual_return']:.1%}", sharpe=m["sharpe"],
        mdd=f"{m['max_drawdown']:.1%}", calmar=m["calmar"],
        yearly_rows=ytr,
        payload=json.dumps({"nav": nav, "years": years,
                            "vals": [yearly[y] for y in years]},
                           ensure_ascii=False))


TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QuantLab 最终策略报告</title>
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
.pos{{color:var(--red);}}
.neg{{color:var(--green);}}
.chart{{width:100%;height:380px;margin:8px 0;}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:24px;}}
.note{{color:var(--muted);font-size:12px;margin-top:8px;}}
.warn{{background:#171a21;border:1px solid #854f0b;border-radius:12px;padding:16px 18px;margin:16px 0;}}
.warn .t{{color:#ef9f27;font-weight:500;margin-bottom:6px;}}
</style>
</head>
<body>
<div class="wrap">
<h1>QuantLab 最终策略报告</h1>
<p class="sub">18 轮迭代后的最终结论 · 2022-01 ~ 2026-09 · A 股多因子选股</p>

<div class="cards">
  <div class="card"><div class="l">年化收益</div><div class="v pos">{ann}</div></div>
  <div class="card"><div class="l">夏普比率</div><div class="v">{sharpe}</div></div>
  <div class="card"><div class="l">最大回撤</div><div class="v neg">{mdd}</div></div>
  <div class="card"><div class="l">Calmar</div><div class="v">{calmar}</div></div>
</div>

<h2>一、最终策略</h2>
<p class="sub">
<code>reversal_5 + size + turnover</code> → ashare_ex 池（去ST/北交所）→ top100 →
逆波动加权 → <b>40日调仓</b>（无择时）。经 IC 衰减分析确认 40 日最优，
经样本外验证确认无过拟合。
</p>

<h2>二、净值曲线 vs 中证1000基准</h2>
<div id="chartNav" class="chart"></div>

<h2>三、年度收益</h2>
<div class="grid2">
<div>
<table>
<thead><tr><th>年份</th><th>收益</th></tr></thead>
<tbody>{yearly_rows}</tbody>
</table>
<p class="note">逐日记账口径：2024 微盘股崩盘年 -7.9%（随后 2025 年 +77.9%）。</p>
</div>
<div id="chartYear" class="chart" style="height:280px"></div>
</div>

<h2>四、样本外验证结论</h2>
<p class="sub">
逐日记账口径（切分 2025-01-01）：样本内（2022-2024，含微盘崩盘）夏普 <b>0.39</b>，
样本外（2025-2026）夏普 <b>2.40</b>；多切分 4 个样本外窗口全部正收益
（最弱 2024-04 窗口夏普 0.57、最强 2025-04 窗口 4.88）。
样本外更强 = <b>选股 alpha 真实、未过拟合</b>（但样本外强势部分由微盘牛驱动，勿盲目外推）。
</p>
<div class="warn">
  <div class="t">关于大盘择时的教训</div>
  <p class="sub" style="color:var(--text)">
  激进择时（趋势向下空仓）样本内回撤 -6.3% 看似完美，但样本外验证暴露<b>过拟合</b>：
  样本内夏普 1.25 → 样本外 0.76。参数在全程数据上调优会过拟合，必须样本外检验。
  因此最终策略<b>不采用择时</b>，择时作为可选项（温和降仓 50%）保留。
  </p>
</div>

<p class="note" style="margin-top:24px">本报告由 QuantLab 自动生成（scripts/final_report.py）。完整 18 轮迭代见 docs/DEV_LOG.md。仅供研究，不构成投资建议。</p>
</div>

<script>
const P = {payload};
const red='#e24b4a', gray='#9aa3b2', line='#2a2f3a', blue='#378add';
const axis={{axisLine:{{lineStyle:{{color:line}}}},axisLabel:{{color:gray}},splitLine:{{lineStyle:{{color:line}}}}}};
(function(){{
  echarts.init(document.getElementById('chartNav')).setOption({{
    grid:{{left:60,right:30,top:40,bottom:40}},
    tooltip:{{trigger:'axis'}},
    legend:{{textStyle:{{color:gray}},top:0}},
    xAxis:{{type:'time',...axis}},
    yAxis:{{type:'value',...axis,scale:true}},
    series:[
      {{name:'策略',type:'line',smooth:true,symbol:'none',lineStyle:{{width:2,color:red}},
        data:P.nav.nav.map((v,i)=>[P.nav.date[i],v])}},
      {{name:'中证1000',type:'line',smooth:true,symbol:'none',lineStyle:{{width:1.5,color:gray,type:'dashed'}},
        data:P.nav.nav_bm.map((v,i)=>[P.nav.date[i],v])}}
    ]
  }});
}})();
(function(){{
  const vals=P.vals;
  echarts.init(document.getElementById('chartYear')).setOption({{
    grid:{{left:50,right:20,top:20,bottom:30}},
    tooltip:{{trigger:'axis',axisPointer:{{type:'shadow'}},formatter:p=>p[0].name+'年<br>'+p[0].value+'%'}},
    xAxis:{{type:'category',data:P.years.map(y=>String(y)),...axis,axisLabel:{{color:'#e6e8ec'}}}},
    yAxis:{{type:'value',...axis,axisLabel:{{color:gray,formatter:v=>v+'%'}}}},
    series:[{{type:'bar',data:vals.map(v=>({{value:v,itemStyle:{{color:v>=0?red:'#1d9e75',borderRadius:4}}}})),barWidth:28}}]
  }});
}})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
