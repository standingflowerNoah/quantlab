#!/usr/bin/env python3
"""生成五层系统总览报告（自包含 HTML）
整合 L1 数据 → L2 因子 → L3 模型 → L4 优化 → L5 决策 的完整成果。
用法: python scripts/overview_report.py
输出: reports/overview_report.html
"""
import sys
import json
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import pandas as pd

from quantlab.config import REPORTS_DIR
from quantlab.data.store import Store
from quantlab.factor import list_factors
from quantlab.model import build_composite, run_backtest
from quantlab.optimize import run_optimized_backtest
from quantlab.decision import load_state, generate_target


def l1_data(store):
    # 每张表对应的"最新日期"列（无日期列的表只查行数）
    tables = {
        "kline_daily": "date",
        "finance_snapshot": "report_date",
        "instruments": "updated_at",
        "trade_calendar": "trade_date",
        "daily_snapshot": "date",
        "index_kline": "date",
    }
    rows = []
    for t, date_col in tables.items():
        try:
            if date_col:
                df = store.q(
                    f"SELECT COUNT(*) n, MAX({date_col}) mx FROM {t}")
                rows.append({"table": t, "rows": int(df.iloc[0, 0]),
                             "latest": str(df.iloc[0, 1])[:10]})
            else:
                df = store.q(f"SELECT COUNT(*) n FROM {t}")
                rows.append({"table": t, "rows": int(df.iloc[0, 0]),
                             "latest": "-"})
        except Exception:
            rows.append({"table": t, "rows": 0, "latest": "-"})
    return rows


def main():
    store = Store()
    # 选股模型：池内精选（size+amihud 选 400 → dragon_net_20 精选 100，与决策层一致）
    from quantlab.model.pool_select import build_pool_refined_score
    factors = ["size", "amihud_20"]
    refine = ["dragon_net_20"]

    # L1
    data_rows = l1_data(store)
    # L2
    flist = list_factors()
    # L3
    score = build_pool_refined_score(
        build_composite(factors, universe="ashare_ex"),
        build_composite(refine, universe="ashare_ex"),
        pool_n=400)
    bt3 = run_backtest(score, n_stocks=100, rebalance=20)
    # L4
    bt4_eq = run_optimized_backtest(score, n_stocks=100, rebalance=20,
                                    method="equal")
    bt4_iv = run_optimized_backtest(score, n_stocks=100, rebalance=20,
                                    method="inverse_vol")
    # L5
    cur = load_state()
    tgt = generate_target(score)
    # 纸面组合跟踪（前向验证账本）
    from quantlab.decision.tracker import paper_nav, paper_summary
    track = paper_nav()
    tsum = paper_summary(track)

    html = render(data_rows, flist, bt3, bt4_eq, bt4_iv, cur, tgt, track, tsum)
    out = REPORTS_DIR / "overview_report.html"
    out.write_text(html, encoding="utf-8")
    print(f"报告已生成: {out}")


def render(data_rows, flist, bt3, bt4_eq, bt4_iv, cur, tgt, track, tsum):
    data_tr = "".join(
        f"<tr><td><code>{r['table']}</code></td><td>{r['rows']:,}</td>"
        f"<td>{r['latest']}</td></tr>" for r in data_rows)
    flist_tr = "".join(
        f"<tr><td><code>{f['name']}</code></td><td>{f['category']}</td>"
        f"<td>{f['description']}</td></tr>" for f in flist.to_dict("records"))

    m3, bm3 = bt3["metrics"], bt3["metrics_bm"]
    m4e, m4i = bt4_eq["metrics"], bt4_iv["metrics"]

    curve = {
        "date": [str(d.date()) for d in bt4_iv["curve"]["date"]],
        "nav_opt": [round(float(v), 4) for v in bt4_iv["curve"]["nav"]],
        "nav_bm": [round(float(v), 4) for v in bt4_iv["curve"]["nav_bm"]],
    }
    # 持仓 top10
    top10 = tgt.head(10)[["code", "name", "weight"]]
    top10_tr = "".join(
        f"<tr><td><code>{r['code']}</code></td><td>{r['name']}</td>"
        f"<td>{r['weight']:.2%}</td></tr>" for r in top10.to_dict("records"))

    # 纸面组合跟踪板块
    if tsum["n_days"] == 0:
        track_section = (
            f'<h2>六、纸面组合跟踪（前向验证）</h2>'
            f'<p class="sub">已记录 {tsum["n_snapshots"]} 期信号快照，'
            f'纸面净值自第 2 期快照的前向交易日开始逐日累积。</p>')
        track_payload = {}
    else:
        cum = tsum["cum_return"]
        track_section = (
            '<h2>六、纸面组合跟踪（前向验证）</h2>'
            '<p class="sub">信号快照记录于每日流水线，逐日盯市，'
            '与回测独立的前向验证账本。</p>'
            '<div class="cards">'
            f'<div class="card"><div class="l">信号快照期数</div><div class="v">{tsum["n_snapshots"]}</div></div>'
            f'<div class="card"><div class="l">已实现交易日</div><div class="v">{tsum["n_days"]}</div></div>'
            f'<div class="card"><div class="l">累计收益</div><div class="v {"pos" if cum>=0 else "neg"}">{cum:+.2%}</div></div>'
            f'<div class="card"><div class="l">跟踪区间</div><div class="v" style="font-size:15px">{tsum["start"]} ~ {tsum["end"]}</div></div>'
            '</div>'
            '<div id="chartTrack" class="chart"></div>')
        track_payload = {
            "track_date": [str(d.date()) for d in track["date"]],
            "track_nav": [round(float(v), 4) for v in track["nav"]],
        }

    return TMPL.format(
        n_factors=len(flist), n_holdings=len(tgt),
        data_tr=data_tr, flist_tr=flist_tr,
        m3_ann=f"{m3['annual_return']:.1%}", m3_sharpe=m3["sharpe"],
        m4e_ann=f"{m4e['annual_return']:.1%}", m4e_sharpe=m4e["sharpe"],
        m4i_ann=f"{m4i['annual_return']:.1%}", m4i_sharpe=m4i["sharpe"],
        m4i_mdd=f"{m4i['max_drawdown']:.1%}",
        bm_ann=f"{bm3['annual_return']:.1%}",
        cur_n=len(cur), tgt_n=len(tgt),
        top10_tr=top10_tr, track_section=track_section,
        payload=json.dumps({**curve, **track_payload}, ensure_ascii=False))


TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QuantLab 五层系统总览报告</title>
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
code{{font-family:ui-monospace,Consolas,monospace;color:#7fb2e5;}}
.chart{{width:100%;height:380px;margin:8px 0;}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:24px;}}
.note{{color:var(--muted);font-size:12px;margin-top:8px;}}
.layer{{display:inline-block;background:#171a21;border:1px solid #2a2f3a;border-radius:6px;
padding:2px 10px;font-size:12px;color:#7fb2e5;margin-bottom:16px;}}
</style>
</head>
<body>
<div class="wrap">
<h1>QuantLab 五层系统总览报告</h1>
<p class="sub">A 股量化研究系统 · L1 数据 → L2 因子 → L3 模型 → L4 优化 → L5 决策（2022-01 ~ 2026-09）</p>

<div class="cards">
  <div class="card"><div class="l">因子数</div><div class="v">{n_factors}</div></div>
  <div class="card"><div class="l">最优组合年化</div><div class="v pos">{m4i_ann}</div></div>
  <div class="card"><div class="l">最优夏普</div><div class="v">{m4i_sharpe}</div></div>
  <div class="card"><div class="l">当前持仓</div><div class="v">{tgt_n} 只</div></div>
</div>

<span class="layer">L1 数据层</span>
<h2>一、数据水位</h2>
<table>
<thead><tr><th>表</th><th>行数</th><th>最新日期</th></tr></thead>
<tbody>{data_tr}</tbody>
</table>

<span class="layer">L2 因子层</span>
<h2>二、内置因子（{n_factors} 个）</h2>
<table>
<thead><tr><th>因子</th><th>类别</th><th>说明</th></tr></thead>
<tbody>{flist_tr}</tbody>
</table>
<p class="sub">实测 IC：reversal 正（+0.06），turnover/volatility/momentum/size 负，均符合 A 股异象。</p>

<span class="layer">L3 模型层</span>
<h2>三、选股回测（3因子 top100 等权，20日调仓）</h2>
<div class="grid2">
  <div class="card"><div class="l">组合年化</div><div class="v pos">{m3_ann}</div></div>
  <div class="card"><div class="l">组合夏普</div><div class="v">{m3_sharpe}</div></div>
  <div class="card"><div class="l">基准年化</div><div class="v neg">{bm_ann}</div></div>
</div>

<span class="layer">L4 优化层</span>
<h2>四、权重优化（逆波动 vs 等权）</h2>
<div class="cards">
  <div class="card"><div class="l">等权年化</div><div class="v">{m4e_ann}</div></div>
  <div class="card"><div class="l">等权夏普</div><div class="v">{m4e_sharpe}</div></div>
  <div class="card"><div class="l">逆波动年化</div><div class="v pos">{m4i_ann}</div></div>
  <div class="card"><div class="l">逆波动夏普</div><div class="v pos">{m4i_sharpe}</div></div>
</div>
<div id="chartNav" class="chart"></div>

<span class="layer">L5 决策层</span>
<h2>五、当前目标持仓 Top10</h2>
<p class="sub">3因子 top100 + 逆波动加权 + 个股/行业风控，共 {tgt_n} 只。</p>
<table>
<thead><tr><th>代码</th><th>名称</th><th>权重</th></tr></thead>
<tbody>{top10_tr}</tbody>
</table>

{track_section}

<p class="note" style="margin-top:24px">本报告由 QuantLab 五层流水线自动生成（scripts/overview_report.py）。仅供研究，不构成投资建议。</p>
</div>

<script>
const P = {payload};
const red='#e24b4a', gray='#9aa3b2', line='#2a2f3a';
const axis={{axisLine:{{lineStyle:{{color:line}}}},axisLabel:{{color:gray}},splitLine:{{lineStyle:{{color:line}}}}}};
(function(){{
  echarts.init(document.getElementById('chartNav')).setOption({{
    grid:{{left:60,right:30,top:40,bottom:40}},
    tooltip:{{trigger:'axis'}},
    legend:{{textStyle:{{color:gray}},top:0}},
    xAxis:{{type:'time',...axis}},
    yAxis:{{type:'value',...axis,scale:true}},
    series:[
      {{name:'逆波动组合',type:'line',smooth:true,symbol:'none',
        lineStyle:{{width:2,color:red}},
        data:P.nav_opt.map((v,i)=>[P.date[i],v])}},
      {{name:'中证1000',type:'line',smooth:true,symbol:'none',
        lineStyle:{{width:1.5,color:gray,type:'dashed'}},
        data:P.nav_bm.map((v,i)=>[P.date[i],v])}}
    ]
  }});
}})();
(function(){{
  if(!P.track_nav || !P.track_nav.length) return;
  echarts.init(document.getElementById('chartTrack')).setOption({{
    grid:{{left:60,right:30,top:30,bottom:40}},
    tooltip:{{trigger:'axis'}},
    xAxis:{{type:'time',...axis}},
    yAxis:{{type:'value',...axis,scale:true}},
    series:[{{name:'纸面组合',type:'line',smooth:true,symbol:'none',
      areaStyle:{{opacity:0.15}},lineStyle:{{width:2,color:red}},
      data:P.track_nav.map((v,i)=>[P.track_date[i],v])}}]
  }});
}})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
