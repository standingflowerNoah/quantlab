#!/usr/bin/env python3
"""国泰君安 Alpha191 研究总报告
=====================================
汇总：公式实现与健全性检验 / 全量 IC / 去冗余 / 组合回测 / 融合终验。
读取 reports/ 下各过程数据文件，生成自包含 HTML。
输出: reports/alpha191_report.html
"""
import json
import warnings
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import pandas as pd

from quantlab.config import REPORTS_DIR


def main():
    man = pd.read_csv(REPORTS_DIR / "alpha191_manifest.csv")
    ic = pd.read_csv(REPORTS_DIR / "alpha191_ic.csv")
    model = json.load(open(REPORTS_DIR / "alpha191_model.json", encoding="utf-8"))
    fusion = json.load(open(REPORTS_DIR / "alpha191_fusion.json", encoding="utf-8"))

    n_ok = int((man["status"] == "ok").sum())
    n_bad = len(man) - n_ok
    bad_list = man[man["status"] != "ok"]["name"].tolist()

    # IC 统计
    n_eval = len(ic)
    n_50 = int((ic["abs_icir20"] > 0.5).sum())
    n_30 = int((ic["abs_icir20"] > 0.3).sum())
    pos_ratio = float((ic["ic5"] > 0).mean())
    top30 = ic.nlargest(30, "abs_icir20")

    ic_tr = "".join(
        f"<tr><td><code>{r['factor']}</code></td>"
        f"<td class='{'pos' if r['ic5'] >= 0 else 'neg'}'>{r['ic5']:+.4f}</td>"
        f"<td class='{'pos' if r['icir5'] >= 0 else 'neg'}'>{r['icir5']:+.3f}</td>"
        f"<td class='{'pos' if r['ic20'] >= 0 else 'neg'}'>{r['ic20']:+.4f}</td>"
        f"<td class='{'pos' if r['icir20'] >= 0 else 'neg'}'>{r['icir20']:+.3f}</td>"
        f"<td>{r['win5']:.0%}</td><td>{int(r['n_days'])}</td></tr>"
        for _, r in top30.iterrows())

    # 模型对比（model.json：top10/top20/融合/对照）
    MODEL_CN = {
        "a191_top10": "Alpha191 top10 等权",
        "a191_top20": "Alpha191 top20 等权",
        "a191_20_size_amihud": "Alpha191 top20 + size + amihud",
        "size_amihud_ref": "size + amihud_20（对照）",
    }
    model_tr = ""
    for k in ["a191_top10", "a191_top20", "a191_20_size_amihud", "size_amihud_ref"]:
        v = model[k]
        m, e = v["metrics"], v["excess"]
        best = k == "size_amihud_ref"
        model_tr += (
            f"<tr class='{'best' if best else ''}'>"
            f"<td>{MODEL_CN[k]}</td>"
            f"<td class='pos'>{m['annual_return']:+.1%}</td><td>{m['sharpe']}</td>"
            f"<td class='neg'>{m['max_drawdown']:.1%}</td>"
            f"<td class='pos'>{e['excess_annual']:+.1%}</td><td>{e['information_ratio']}</td>"
            f"<td>{v['turnover']:.1%}</td></tr>")

    # 融合终验
    FUSE_CN = {
        "a191_top20_nocost": "E1 · Alpha191 top20（零交易成本）",
        "mix_w0.6": "E2 · 加权融合 w=0.6",
        "mix_w0.7": "E2 · 加权融合 w=0.7",
        "mix_w0.8": "E2 · 加权融合 w=0.8",
        "pool_refine_400": "E3 · 池内精选（400池→100）",
        "size_amihud_ref": "size + amihud_20（对照）",
    }
    fuse_tr = ""
    for k in ["a191_top20_nocost", "mix_w0.6", "mix_w0.7", "mix_w0.8",
              "pool_refine_400", "size_amihud_ref"]:
        v = fusion[k]
        m, e = v["metrics"], v["excess"]
        best = k == "size_amihud_ref"
        fuse_tr += (
            f"<tr class='{'best' if best else ''}'>"
            f"<td>{FUSE_CN[k]}</td>"
            f"<td class='pos'>{m['annual_return']:+.1%}</td><td>{m['sharpe']}</td>"
            f"<td>{e['information_ratio']}</td>"
            f"<td>{v['turnover']:.1%}</td></tr>")

    curve = {
        "date": model["size_amihud_ref"]["date"],
        "nav_sa": model["size_amihud_ref"]["nav"],
        "nav_a191": model["a191_top20"]["nav"],
    }

    html = TMPL.format(
        n_ok=n_ok, n_bad=n_bad, bad_list=", ".join(bad_list),
        n_eval=n_eval, n_50=n_50, n_30=n_30,
        pos_ratio=f"{pos_ratio:.0%}",
        ic_tr=ic_tr, model_tr=model_tr, fuse_tr=fuse_tr,
        payload=json.dumps(curve, ensure_ascii=False))

    out = REPORTS_DIR / "alpha191_report.html"
    out.write_text(html, encoding="utf-8")
    print(f"报告已生成: {out}")


TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>国泰君安 Alpha191 研究总报告</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
:root{{--bg:#f7f8fa;--card:#fff;--text:#1f2430;--muted:#7a8194;--line:#e6e8ee;
--red:#d8463e;--green:#1d9e75;--accent:#2563eb;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--text);font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.65;padding:36px 20px;}}
.wrap{{max-width:1080px;margin:0 auto;}}
h1{{font-size:24px;font-weight:700;}}
h2{{font-size:16.5px;font-weight:600;margin:32px 0 12px;padding-left:11px;border-left:3px solid var(--accent);}}
.sub{{color:var(--muted);font-size:13px;margin-top:5px;}}
.cards{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:20px 0;}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;}}
.card .l{{color:var(--muted);font-size:12px;}}
.card .v{{font-size:21px;font-weight:700;margin-top:3px;}}
table{{width:100%;border-collapse:collapse;margin:10px 0;font-size:12.5px;background:var(--card);}}
th,td{{padding:6px 10px;text-align:right;border-bottom:1px solid var(--line);}}
th{{color:var(--muted);font-weight:600;background:#fbfbfd;}}
td:first-child,th:first-child{{text-align:left;}}
code{{font-family:ui-monospace,Consolas,monospace;color:#1d4ed8;font-size:12px;}}
.pos{{color:var(--red);}} .neg{{color:var(--green);}}
.chart{{width:100%;height:400px;margin:8px 0;background:var(--card);border:1px solid var(--line);border-radius:12px;}}
tr.best td{{background:#fff4e6;font-weight:600;}}
.note{{color:var(--muted);font-size:12px;margin-top:10px;}}
.callout{{background:#fff8f0;border:1px solid #f0d9b5;border-radius:10px;padding:14px 16px;font-size:13.5px;margin:14px 0;}}
.win{{background:#f0f9f4;border:1px solid #bfe3cd;}}
</style>
</head>
<body>
<div class="wrap">
<h1>国泰君安 Alpha191 研究总报告</h1>
<p class="sub">《基于短周期价量特征的多因子选股体系》191 因子 · 宽表算子引擎实现 · 全量 IC / 去冗余 / 组合与融合检验 · 2022-07 ~ 2026-09</p>

<div class="cards">
  <div class="card"><div class="l">公式转译</div><div class="v">191</div></div>
  <div class="card"><div class="l">落库+IC 通过</div><div class="v">{n_ok}</div></div>
  <div class="card"><div class="l">|ICIR20|&gt;0.5</div><div class="v pos">{n_50}</div></div>
  <div class="card"><div class="l">去冗余代表</div><div class="v">72</div></div>
  <div class="card"><div class="l">对照模型年化</div><div class="v pos">+37.5%</div></div>
</div>

<h2>一、公式实现与健全性检验</h2>
<p class="sub">公式依据公开研报版本转译（high/mid 置信度标注于代码）；每个因子计算后经健全性检验
（非全 NaN / 非常数截面 / 有效率），退化因子拦截不落库。</p>
<p class="sub"><strong>{n_ok}</strong> 个通过并落库（约 9 亿行，7.7GB）；<strong>{n_bad}</strong> 个拦截：
<code>{bad_list}</code>（多为流传版本本身退化或转译无把握，详见 alpha191_manifest.csv）。</p>

<h2>二、全量 IC 检验（{n_eval} 因子 · Top 30 按 |ICIR20|）</h2>
<p class="sub">正 IC 占比 {pos_ratio}；|ICIR20|&gt;0.5 共 <strong>{n_50}</strong> 个、&gt;0.3 共 <strong>{n_30}</strong> 个——
强因子集中于「量价背离」族（价量协方差/相关性）。</p>
<table>
<thead><tr><th>因子</th><th>IC(5日)</th><th>ICIR(5日)</th><th>IC(20日)</th><th>ICIR(20日)</th><th>IC&gt;0占比</th><th>天数</th></tr></thead>
<tbody>{ic_tr}</tbody>
</table>
<p class="note">注意：mid 置信度批量转译存在等价公式（如 alpha126≡145），已按 IC 一致性聚为 10 组，去冗余后 72 个独立代表（见 alpha191_representatives.csv）。</p>

<h2>三、组合回测（去冗余 top 因子 · rank 等权 · top100 · 20日调仓 · 扣成本）</h2>
<table>
<thead><tr><th>模型</th><th>年化</th><th>夏普</th><th>最大回撤</th><th>超额年化</th><th>信息比</th><th>换手</th></tr></thead>
<tbody>{model_tr}</tbody>
</table>

<h2>四、融合方式终验</h2>
<table>
<thead><tr><th>配置</th><th>年化</th><th>夏普</th><th>信息比</th><th>换手</th></tr></thead>
<tbody>{fuse_tr}</tbody>
</table>

<div class="callout win">
<strong>核心结论（三层验证闭环）：</strong>① <strong>信号层有效</strong>——72 个独立代表因子 ICIR 最高 0.81、
胜率 80%，量价背离信号真实存在；② <strong>组合层跑输</strong>——纯组合年化仅 11~13%、任何融合方式（加权/池内精选）
均拖累 size+amihud（37.5%）；③ <strong>归因清晰</strong>——零成本对照证明交易成本仅解释 ~5pp，
根本原因是 A 股「小市值+低流动性」风险溢价的幅度远大于短周期量价 alpha。
</div>
<div class="callout">
<strong>定位建议：</strong>Alpha191 全套（算子引擎 + 191 公式 + 180 因子库 + 检验流水线）作为<strong>信号资产留档</strong>：
适用场景为小市值溢价衰减期的备选信号、持仓内部微调、或未来接入低成本执行通道后的独立策略。
生产模型维持 <code>size + amihud_20</code> 不变。
</div>

<h2>五、净值曲线（size+amihud vs Alpha191 top20）</h2>
<div id="chart" class="chart"></div>

<p class="note" style="margin-top:24px">本报告由 scripts/alpha191_report.py 生成。公式转译自公开研报版本，个别因子可能存在偏差（已用健全性检验+IC 过滤）。仅供研究，不构成投资建议。</p>
</div>

<script>
const P = {payload};
const axis={{axisLine:{{lineStyle:{{color:'#e6e8ee'}}}},axisLabel:{{color:'#7a8194'}},splitLine:{{lineStyle:{{color:'#eef0f4'}}}}}};
echarts.init(document.getElementById('chart')).setOption({{
  grid:{{left:60,right:30,top:40,bottom:40}},
  tooltip:{{trigger:'axis'}},
  legend:{{top:0,textStyle:{{color:'#7a8194'}}}},
  xAxis:{{type:'time',...axis}},
  yAxis:{{type:'value',...axis,scale:true}},
  series:[
    {{name:'size+amihud_20',type:'line',smooth:true,symbol:'none',lineStyle:{{width:2.4,color:'#d8463e'}},data:P.nav_sa.map((v,i)=>[P.date[i],v])}},
    {{name:'Alpha191 top20',type:'line',smooth:true,symbol:'none',lineStyle:{{width:1.8,color:'#2563eb'}},data:P.nav_a191.map((v,i)=>[P.date[i],v])}}
  ]
}});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
