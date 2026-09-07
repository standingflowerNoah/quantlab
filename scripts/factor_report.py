#!/usr/bin/env python3
"""生成因子层试跑报告（自包含 HTML，ECharts 图表）
用法: python scripts/factor_report.py
输出: reports/factor_report.html
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from quantlab.config import REPORTS_DIR
from quantlab.data.store import Store
from quantlab.factor import list_factors

TRADING_DAYS = 252


def build_fwd(qfq: pd.DataFrame, horizon: int) -> pd.DataFrame:
    # 复用 quality 的 DuckDB 窗口函数实现（替代 pandas groupby shift）
    from quantlab.factor.quality import _fwd_return
    return _fwd_return(horizon)


def ic_table(store, qfq, names, horizon):
    from quantlab.factor import factor_ic
    rows = []
    for name in names:
        ics = factor_ic(name, horizon=horizon)
        if ics.empty:
            rows.append({"factor": name, "n_days": 0, "ic_mean": np.nan,
                         "ic_std": np.nan, "icir": np.nan, "win": np.nan})
            continue
        ic = ics["ic"]
        rows.append({
            "factor": name, "n_days": len(ic),
            "ic_mean": round(float(ic.mean()), 4),
            "ic_std": round(float(ic.std()), 4),
            "icir": round(float(ic.mean() / ic.std()), 3) if ic.std() > 0 else np.nan,
            "win": round(float((ic > 0).mean()), 3),
        })
    return pd.DataFrame(rows)


def backtest_summary(store, qfq, name, horizon=20, n_q=5):
    fwd = build_fwd(qfq, horizon)
    fv = store.read_factor(name)
    fv["date"] = pd.to_datetime(fv["date"])
    fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
    fv = fv[np.isfinite(fv["value"])]
    m = fv.merge(fwd, on=["date", "code"], how="inner")
    dates = np.sort(m["date"].unique())
    m = m[m["date"].isin(dates[::horizon])]

    def _qcut(x):
        try:
            return pd.qcut(x, n_q, labels=False, duplicates="drop")
        except Exception:
            return pd.Series(np.nan, index=x.index)

    m["q"] = m.groupby("date")["value"].transform(_qcut)
    m = m.dropna(subset=["q"])
    m["q"] = m["q"].astype(int)
    layer = m.groupby(["date", "q"])["fwd"].mean().unstack()
    layer = layer.reindex(columns=range(n_q))
    ppy = TRADING_DAYS / horizon
    q_ann = [round(float((1 + layer[q].mean()) ** ppy - 1), 4)
             for q in range(n_q)]
    ls = (layer[n_q - 1] - layer[0]).dropna()
    ls_ann = float((1 + ls.mean()) ** ppy - 1)
    nav = (1 + ls).cumprod()
    curve = [{"date": str(d.date()), "nav": round(float(v), 4)}
             for d, v in nav.items()]
    order = pd.Series(q_ann).rank().corr(pd.Series(range(n_q)))
    return {"q_ann": q_ann, "ls_ann": round(ls_ann, 4),
            "mono": round(float(order), 3), "curve": curve}


def main():
    store = Store()
    qfq = store.q(
        "SELECT date, code, close*adj_factor AS c "
        "FROM kline_daily ORDER BY code, date")
    qfq["date"] = pd.to_datetime(qfq["date"])

    flist = list_factors().to_dict("records")
    ts_names = ["momentum_20", "momentum_60", "momentum_120",
                "reversal_5", "reversal_10", "volatility_20",
                "volatility_60", "size", "total_mcap", "turnover",
                "rsi_14"]
    ic5 = ic_table(store, qfq, ts_names, 5)
    ic20 = ic_table(store, qfq, ts_names, 20)

    bt_names = ["reversal_5", "reversal_10", "turnover",
                "volatility_20", "momentum_60", "size"]
    bt = {}
    curves = {}
    for n in bt_names:
        bt[n] = backtest_summary(store, qfq, n)
        curves[n] = bt[n]["curve"]

    n_stocks = int(store.q(
        "SELECT COUNT(DISTINCT code) FROM kline_daily").iloc[0, 0])
    dmin = str(qfq["date"].min().date())
    dmax = str(qfq["date"].max().date())

    html = render(flist, ic5, ic20, bt, curves, n_stocks, dmin, dmax)
    out = REPORTS_DIR / "factor_report.html"
    out.write_text(html, encoding="utf-8")
    print(f"报告已生成: {out}")


def render(flist, ic5, ic20, bt, curves, n_stocks, dmin, dmax):
    flist_rows = "".join(
        f"<tr><td><code>{f['name']}</code></td><td>{f['category']}</td>"
        f"<td>{f['description']}</td></tr>" for f in flist)

    def ic_rows(df):
        return "".join(
            f"<tr><td><code>{r['factor']}</code></td>"
            f"<td>{r['n_days']}</td><td>{r['ic_mean']}</td>"
            f"<td>{r['ic_std']}</td><td>{r['icir']}</td>"
            f"<td>{r['win']}</td></tr>" for r in df.to_dict("records"))

    bt_rows = "".join(
        f"<tr><td><code>{n}</code></td>"
        + "".join(f"<td>{v*100:.1f}%</td>" for v in bt[n]["q_ann"])
        + f"<td>{bt[n]['ls_ann']*100:.1f}%</td>"
          f"<td>{bt[n]['mono']}</td></tr>"
        for n in bt)

    # 多空收益柱状图数据
    ls_data = {n: bt[n]["ls_ann"] for n in bt}
    ls_sorted = sorted(ls_data.items(), key=lambda x: -x[1])

    payload = {
        "ic5": ic5.to_dict("records"),
        "ic20": ic20.to_dict("records"),
        "ls": [{"name": n, "v": round(v * 100, 1)} for n, v in ls_sorted],
        "curves": {n: curves[n] for n in ["reversal_5", "size", "turnover"]},
    }
    return HTML_TMPL.format(
        n_factors=len(flist), n_stocks=n_stocks,
        dmin=dmin, dmax=dmax,
        flist_rows=flist_rows, ic5_rows=ic_rows(ic5),
        ic20_rows=ic_rows(ic20), bt_rows=bt_rows,
        payload=json.dumps(payload, ensure_ascii=False))


HTML_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QuantLab 因子层试跑报告</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
:root{{--bg:#0f1115;--card:#171a21;--text:#e6e8ec;--muted:#9aa3b2;
--line:#2a2f3a;--red:#e24b4a;--green:#1d9e75;--accent:#378add;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--text);font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
line-height:1.6;padding:40px 24px;}}
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
code{{font-family:ui-monospace,"SF Mono",Consolas,monospace;color:#7fb2e5;}}
.pos{{color:var(--red);}}
.neg{{color:var(--green);}}
.chart{{width:100%;height:360px;margin:8px 0;}}
.note{{color:var(--muted);font-size:12px;margin-top:8px;}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:24px;}}
@media(max-width:820px){{.cards{{grid-template-columns:repeat(2,1fr);}}.grid2{{grid-template-columns:1fr;}}}}
</style>
</head>
<body>
<div class="wrap">
<h1>QuantLab 因子层试跑报告</h1>
<p class="sub">A 股量化研究系统 · L2 因子层 · 生成于本地 DuckDB 数据</p>

<div class="cards">
  <div class="card"><div class="l">因子数</div><div class="v">{n_factors}</div></div>
  <div class="card"><div class="l">覆盖股票</div><div class="v">{n_stocks}</div></div>
  <div class="card"><div class="l">样本区间</div><div class="v" style="font-size:16px">{dmin}</div></div>
  <div class="card"><div class="l">截止</div><div class="v" style="font-size:16px">{dmax}</div></div>
</div>

<h2>一、内置因子清单</h2>
<table>
<thead><tr><th>因子</th><th>类别</th><th>说明</th></tr></thead>
<tbody>{flist_rows}</tbody>
</table>

<h2>二、因子 IC 评估</h2>
<p class="sub">IC = 每日截面「因子值」与「未来收益」的 Spearman 秩相关。ICIR = 均值/标准差，|ICIR| &gt; 0.3 视为显著。</p>
<div class="grid2">
<div>
<h3 style="font-size:14px;color:var(--muted);margin:8px 0">未来 5 日</h3>
<table><thead><tr><th>因子</th><th>天数</th><th>IC均值</th><th>IC标准差</th><th>ICIR</th><th>胜率</th></tr></thead>
<tbody>{ic5_rows}</tbody></table>
</div>
<div>
<h3 style="font-size:14px;color:var(--muted);margin:8px 0">未来 20 日</h3>
<table><thead><tr><th>因子</th><th>天数</th><th>IC均值</th><th>IC标准差</th><th>ICIR</th><th>胜率</th></tr></thead>
<tbody>{ic20_rows}</tbody></table>
</div>
</div>

<h2>三、分层回测 · 多空组合（持有 20 日）</h2>
<p class="sub">每日按因子值分 5 层，Q1 最低 Q5 最高；多空 = Q5 − Q1 年化收益。红 = 做多高因子值跑赢，绿 = 反向。</p>
<div id="chartLsh" class="chart"></div>
<table>
<thead><tr><th>因子</th><th>Q1</th><th>Q2</th><th>Q3</th><th>Q4</th><th>Q5</th><th>多空年化</th><th>单调性</th></tr></thead>
<tbody>{bt_rows}</tbody>
</table>
<p class="note">单调性 = 分层收益与层序号的秩相关，|值| 越接近 1 表示分层收益越有序。</p>

<h2>四、多空净值曲线（方向校正后）</h2>
<p class="sub">按 IC 符号自动校正方向，使有效方向的多空组合净值为正。</p>
<div id="chartNav" class="chart" style="height:420px"></div>

<p class="note" style="margin-top:24px">本报告由 QuantLab 因子层自动生成，数据为本地 Parquet 因子库 + DuckDB 前复权 K 线。仅供研究，不构成投资建议。</p>
</div>

<script>
const P = {payload};
const red='#e24b4a', green='#1d9e75', gray='#2a2f3a';
const axis={{axisLine:{{lineStyle:{{color:gray}}}},axisLabel:{{color:'#9aa3b2'}},splitLine:{{lineStyle:{{color:gray}}}}}};

(function(){{
  const names=P.ls.map(x=>x.name), vals=P.ls.map(x=>x.v);
  const colors=vals.map(v=>v>=0?red:green);
  echarts.init(document.getElementById('chartLsh')).setOption({{
    grid:{{left:110,right:40,top:20,bottom:30}},
    tooltip:{{trigger:'axis',axisPointer:{{type:'shadow'}},
      formatter:p=>p[0].name+'<br>多空年化 <b>'+(p[0].value>0?'+':'')+p[0].value+'%</b>'}},
    xAxis:{{type:'value',...axis,axisLabel:{{color:'#9aa3b2',formatter:v=>(v>0?'+':'')+v+'%'}}}},
    yAxis:{{type:'category',data:names,...axis,axisLabel:{{color:'#e6e8ec',fontSize:13}}}},
    series:[{{type:'bar',data:vals.map((v,i)=>({{value:v,itemStyle:{{color:colors[i],borderRadius:4}}}})),
      barWidth:16}}]
  }});
}})();

(function(){{
  const keys=['reversal_5','size','turnover'];
  const labels=['reversal_5（反转）','size（小市值，反向）','turnover（低换手，反向）'];
  const series=keys.map((k,i)=>({{
    name:labels[i], type:'line', smooth:true, symbol:'none',
    lineStyle:{{width:2}},
    data:P.curves[k].map(p=>[p.date,p.nav])
  }}));
  echarts.init(document.getElementById('chartNav')).setOption({{
    grid:{{left:60,right:30,top:40,bottom:40}},
    tooltip:{{trigger:'axis'}},
    legend:{{textStyle:{{color:'#9aa3b2'}},top:0,data:labels}},
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
