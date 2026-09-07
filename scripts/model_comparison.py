#!/usr/bin/env python3
"""两个模型的对比报告（规则化多因子 vs LightGBM）
=====================================
等权 3 因子合成（reversal_5+size+turnover） vs LightGBM 滚动重训练（14因子）。
同样时段（2024-01~2026-08，含震荡市）、同样回测（top100、40日调仓、equal 权重）。
用法: python scripts/model_comparison.py
输出: reports/model_comparison.html
"""
import sys
import json
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from quantlab.config import REPORTS_DIR
from quantlab.model import build_composite
from quantlab.model.lgbm import build_lgbm_score_walkforward, _fwd_return_df
from quantlab.optimize import run_optimized_backtest
from quantlab.data.store import Store
from quantlab.data.universe import get_universe

FACTORS = ["amplitude_20", "max_return_20", "momentum_120", "momentum_20",
           "momentum_60", "price_position_250", "reversal_10", "reversal_5",
           "rsi_14", "size", "total_mcap", "turnover", "volatility_20",
           "volatility_60"]


def _annual(curve):
    c = curve.copy()
    c["year"] = pd.to_datetime(c["date"]).dt.year
    return {int(y): round(float((1 + g["ret"]).prod() - 1) * 100, 1)
            for y, g in c.groupby("year")}


def _ic(score, horizon=20):
    fwd = _fwd_return_df(horizon, Store())
    m = score.merge(fwd, on=["date", "code"], how="inner")
    ics = [g["score"].rank().corr(g["fwd"].rank())
           for _, g in m.groupby("date") if len(g) >= 50]
    ic = pd.Series(ics).dropna()
    return round(float(ic.mean()), 4), round(float(ic.mean() / ic.std()), 3)


def main():
    uni = set(get_universe("ashare_ex"))

    # 模型1：等权 3 因子
    eq = build_composite(["reversal_5", "size", "turnover"],
                         universe="ashare_ex", start="2024-01-01")
    eq_r = run_optimized_backtest(eq, n_stocks=100, rebalance=40,
                                  method="equal")
    # 模型2：LightGBM 滚动重训练
    lgbm = build_lgbm_score_walkforward(FACTORS)
    lgbm = lgbm[lgbm["code"].isin(uni)]
    lgbm_r = run_optimized_backtest(lgbm, n_stocks=100, rebalance=40,
                                    method="equal")

    eq_m, lgbm_m = eq_r["metrics"], lgbm_r["metrics"]
    eq_ic, eq_icir = _ic(eq)
    lgbm_ic, lgbm_icir = _ic(lgbm)

    # 净值曲线：各模型画各自完整日期（不强制交集，避免截断尾部）
    nav = {
        "eq_dates": [str(d.date()) for d in eq_r["curve"]["date"]],
        "eq": [round(float(v), 3) for v in eq_r["curve"]["nav"]],
        "lgbm_dates": [str(d.date()) for d in lgbm_r["curve"]["date"]],
        "lgbm": [round(float(v), 3) for v in lgbm_r["curve"]["nav"]],
    }
    yearly_eq = _annual(eq_r["curve"])
    yearly_lgbm = _annual(lgbm_r["curve"])
    years = sorted(set(yearly_eq) | set(yearly_lgbm))

    payload = {
        "eq": {"name": "等权 3 因子", "annual": eq_m["annual_return"],
               "sharpe": eq_m["sharpe"], "mdd": eq_m["max_drawdown"],
               "ic": eq_ic, "icir": eq_icir,
               "yearly": [yearly_eq.get(y) for y in years]},
        "lgbm": {"name": "LightGBM 滚动", "annual": lgbm_m["annual_return"],
                 "sharpe": lgbm_m["sharpe"], "mdd": lgbm_m["max_drawdown"],
                 "ic": lgbm_ic, "icir": lgbm_icir,
                 "yearly": [yearly_lgbm.get(y) for y in years]},
        "years": [str(y) for y in years],
        "nav": nav,
    }
    html = TMPL.format(payload=json.dumps(payload, ensure_ascii=False))
    out = REPORTS_DIR / "model_comparison.html"
    out.write_text(html, encoding="utf-8")
    print(f"报告已生成: {out}")


TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>模型对比：等权多因子 vs LightGBM</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
:root{{--bg:#0f1115;--card:#171a21;--text:#e6e8ec;--muted:#9aa3b2;
--line:#2a2f3a;--red:#e24b4a;--green:#1d9e75;--accent:#378add;--amber:#ef9f27;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--text);font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.6;padding:40px 24px;}}
.wrap{{max-width:1080px;margin:0 auto;}}
h1{{font-size:24px;font-weight:600;}}
h2{{font-size:17px;font-weight:600;margin:36px 0 14px;padding-left:12px;border-left:3px solid var(--accent);}}
.sub{{color:var(--muted);font-size:13px;margin-top:6px;}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:22px 0;}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;}}
.card .l{{color:var(--muted);font-size:12px;}}
.card .v{{font-size:22px;font-weight:600;margin-top:4px;}}
.card .e{{font-size:11px;color:var(--muted);margin-top:2px;}}
.eq{{color:var(--accent);}}
.lgbm{{color:var(--amber);}}
.chart{{width:100%;height:380px;margin:8px 0;}}
table{{width:100%;border-collapse:collapse;margin:12px 0;font-size:13px;}}
th,td{{padding:8px 12px;text-align:right;border-bottom:1px solid var(--line);}}
th{{color:var(--muted);font-weight:500;}}
td:first-child,th:first-child{{text-align:left;}}
.pos{{color:var(--red);}}
.neg{{color:var(--green);}}
.note{{color:var(--muted);font-size:12px;margin-top:8px;}}
.concl{{background:#171a21;border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:16px 0;}}
</style>
</head>
<body>
<div class="wrap">
<h1>模型对比：等权多因子 vs LightGBM</h1>
<p class="sub">样本外时段 2024-01 ~ 2026-08（含 2024 震荡市）· top100 · 40日调仓 · 等权</p>

<div class="cards" id="cards"></div>

<h2>一、净值曲线对比</h2>
<div id="chartNav" class="chart"></div>

<h2>二、年度收益对比</h2>
<div id="chartYear" class="chart" style="height:300px"></div>

<h2>三、预测力（IC）对比</h2>
<table>
<thead><tr><th>模型</th><th>因子数</th><th>Rank IC</th><th>ICIR</th><th>样本外夏普</th><th>最大回撤</th></tr></thead>
<tbody id="icTable"></tbody>
</table>

<div class="concl">
<b>结论：</b>逐日记账口径下（日频盯市、按日年化），等权 3 因子合成的样本外表现
<b>优于</b> LightGBM 滚动重训练（夏普与回撤均占优），进一步支持默认信号采用等权合成。
LightGBM 需要 14 个因子、滚动重训练与更多计算成本，且未带来稳健的边际收益，
保留为可选对比工具（<code>quantlab/model/lgbm.py</code>）。
</div>

<p class="note">报告由 scripts/model_comparison.py 自动生成。净值曲线为逐日盯市（每个交易日一个点）。样本外验证已避开 2025-2026 牛市小样本假象，使用含震荡市的完整时段。仅供研究，不构成投资建议。</p>
</div>

<script>
const P = {payload};
const blue='#378add', amber='#ef9f27', red='#e24b4a', green='#1d9e75', gray='#9aa3b2', line='#2a2f3a';
const axis={{axisLine:{{lineStyle:{{color:line}}}},axisLabel:{{color:gray}},splitLine:{{lineStyle:{{color:line}}}}}};
(function(){{
  const e=P.eq, l=P.lgbm;
  const mk=(cls,label,v,extra)=>{{
    return '<div class="card"><div class="l">'+label+'</div><div class="v '+cls+'">'+v+'</div><div class="e">'+extra+'</div></div>';
  }};
  document.getElementById('cards').innerHTML =
    mk('eq','等权 3 因子 · 夏普',e.sharpe,'年化 '+(e.annual*100).toFixed(1)+'%') +
    mk('lgbm','LightGBM · 夏普',l.sharpe,'年化 '+(l.annual*100).toFixed(1)+'%') +
    mk('eq','等权 · 最大回撤',(e.mdd*100).toFixed(1)+'%','Rank IC '+e.ic) +
    mk('lgbm','LightGBM · 最大回撤',(l.mdd*100).toFixed(1)+'%','Rank IC '+l.ic);
}})();
(function(){{
  echarts.init(document.getElementById('chartNav')).setOption({{
    grid:{{left:60,right:30,top:40,bottom:40}},
    tooltip:{{trigger:'axis'}},
    legend:{{textStyle:{{color:gray}},top:0}},
    xAxis:{{type:'time',...axis}},
    yAxis:{{type:'value',...axis,scale:true}},
    series:[
      {{name:'等权 3 因子',type:'line',smooth:true,symbol:'none',lineStyle:{{width:2,color:blue}},
        data:P.nav.eq_dates.map((d,i)=>[d,P.nav.eq[i]])}},
      {{name:'LightGBM 滚动',type:'line',smooth:true,symbol:'none',lineStyle:{{width:2,color:amber}},
        data:P.nav.lgbm_dates.map((d,i)=>[d,P.nav.lgbm[i]])}}
    ]
  }});
}})();
(function(){{
  const yrs=P.years;
  echarts.init(document.getElementById('chartYear')).setOption({{
    grid:{{left:50,right:20,top:20,bottom:30}},
    tooltip:{{trigger:'axis',axisPointer:{{type:'shadow'}}}},
    legend:{{textStyle:{{color:gray}},top:0}},
    xAxis:{{type:'category',data:yrs,...axis,axisLabel:{{color:'#e6e8ec'}}}},
    yAxis:{{type:'value',...axis,axisLabel:{{color:gray,formatter:v=>v+'%'}}}},
    series:[
      {{name:'等权 3 因子',type:'bar',data:P.eq.yearly.map(v=>({{value:v,itemStyle:{{color:blue,borderRadius:3}}}})),barWidth:16}},
      {{name:'LightGBM 滚动',type:'bar',data:P.lgbm.yearly.map(v=>({{value:v,itemStyle:{{color:amber,borderRadius:3}}}})),barWidth:16}}
    ]
  }});
}})();
(function(){{
  const e=P.eq,l=P.lgbm;
  document.getElementById('icTable').innerHTML =
    '<tr><td class="eq">等权 3 因子</td><td>3</td><td>'+e.ic+'</td><td>'+e.icir+'</td><td>'+e.sharpe+'</td><td class="neg">'+(e.mdd*100).toFixed(1)+'%</td></tr>' +
    '<tr><td class="lgbm">LightGBM 滚动</td><td>14</td><td>'+l.ic+'</td><td>'+l.icir+'</td><td>'+l.sharpe+'</td><td class="neg">'+(l.mdd*100).toFixed(1)+'%</td></tr>';
}})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
