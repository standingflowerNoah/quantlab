#!/usr/bin/env python3
"""生产模型重构 · 最终步：生成 HTML 报告
=====================================
输入: reports/prodmodel/{factor_ic20,shortlist,contribution,
      satellite_selection_*,models_result,sensitivity}.csv/json + curves.pkl
      + reports/factor_corr_matrix_20260907.csv
输出: reports/prodmodel_report.html
"""
from __future__ import annotations

import json
import sys
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports/prodmodel"
REPORT = ROOT / "reports/prodmodel_report.html"


def load_all():
    d = {}
    d["ic20"] = pd.read_csv(OUT / "factor_ic20.csv")
    d["short"] = pd.read_csv(OUT / "shortlist.csv")
    d["contrib"] = pd.read_csv(OUT / "contribution.csv")
    d["sel_ex"] = pd.read_csv(OUT / "satellite_selection_ex.csv")
    d["sel_all"] = pd.read_csv(OUT / "satellite_selection_all.csv")
    d["res"] = json.loads((OUT / "models_result.json").read_text(
        encoding="utf-8"))
    d["curves"] = pd.read_pickle(OUT / "curves.pkl")
    d["corr"] = pd.read_csv(ROOT / "reports/factor_corr_matrix_20260907.csv",
                            index_col=0)
    p = OUT / "sensitivity.json"
    if p.exists():
        d["sens"] = json.loads(p.read_text(encoding="utf-8"))
    p = OUT / "si_deep.json"
    if p.exists():
        d["si"] = json.loads(p.read_text(encoding="utf-8"))
    p = OUT / "si_fast.json"
    if p.exists():
        d["sif"] = json.loads(p.read_text(encoding="utf-8"))
        cp = OUT / "si_fast_curves.pkl"
        if cp.exists():
            d["sif_curves"] = pd.read_pickle(cp)
    p = OUT / "capacity.json"
    if p.exists():
        d["cap"] = json.loads(p.read_text(encoding="utf-8"))
    p = OUT / "hf_add.json"
    if p.exists():
        d["hfadd"] = json.loads(p.read_text(encoding="utf-8"))
    p = OUT / "hf_robust.json"
    if p.exists():
        d["hfrob"] = json.loads(p.read_text(encoding="utf-8"))
    p = OUT / "hf2.json"
    if p.exists():
        d["hf2"] = json.loads(p.read_text(encoding="utf-8"))
    p = OUT / "hf3.json"
    if p.exists():
        d["hf3"] = json.loads(p.read_text(encoding="utf-8"))
    p = OUT / "crash2024.json"
    if p.exists():
        d["crash"] = json.loads(p.read_text(encoding="utf-8"))
    return d


def fam(name: str) -> str:
    if name.startswith("alpha") or name.endswith("_composite"):
        return "Alpha191"
    if name.startswith("hf_"):
        return "高频"
    if name in ("size", "amihud_20"):
        return "核心"
    if name in ("dragon_net_20", "lockup_pressure_60", "holder_num_chg",
                "holder_num_chg_2", "inst_buy_20", "retail_buy_20",
                "block_premium_20", "sue", "sue_i", "sue_pred"):
        return "事件/基本面"
    return "风格/量价"


def scatter_data(ic20: pd.DataFrame):
    pts = {}
    for _, r in ic20.iterrows():
        pts.setdefault(fam(r["factor"]), []).append(
            [round(r["coverage"] * 100, 1), abs(r["icir"]),
             r["factor"], round(r["ic_mean"], 4)])
    return [{"name": k, "data": v} for k, v in pts.items()]


KEY_MODELS = ["PROD", "CORE_EX", "CORE_EX_IW", "POOL_REFINE", "DUAL_E",
              "PROD_SI"]


def nav_series(curves: dict):
    out = []
    for name in KEY_MODELS:
        if name not in curves:
            continue
        c = curves[name]
        cv = c["curve"]
        out.append({"name": name,
                    "dates": [str(pd.Timestamp(d).date()) for d in cv["date"]],
                    "nav": [round(float(v), 4) for v in cv["nav"]],
                    "bm": [round(float(v), 4) for v in cv["nav_bm"]]})
    return out


def roll_ic(curves: dict, win: int = 60):
    out = []
    for name in KEY_MODELS:
        if name not in curves:
            continue
        s = curves[name]["ic_series"].sort_index()
        r = s.rolling(win).mean().dropna()
        out.append({"name": name,
                    "dates": [str(pd.Timestamp(d).date()) for d in r.index],
                    "v": [round(float(v), 4) for v in r.values]})
    return out


def ic_hist(curves: dict):
    out = []
    for name in KEY_MODELS:
        if name not in curves:
            continue
        s = curves[name]["ic_series"]
        bins = np.linspace(-0.15, 0.15, 31)
        h, edges = np.histogram(s.values, bins=bins)
        out.append({"name": name,
                    "centers": [round((edges[i] + edges[i + 1]) / 2, 4)
                                for i in range(len(h))],
                    "counts": [int(x) for x in h]})
    return out


def corr_subset(corr: pd.DataFrame, factors: list[str]):
    fs = [f for f in factors if f in corr.index]
    m = corr.loc[fs, fs].fillna(0)
    return {"factors": fs,
            "matrix": [[round(float(v), 2) for v in row] for row in m.values]}


def yearly_chart(res: dict):
    models = [m for m in res if m != "PROD_SI"]
    years = sorted({int(y) for m in models for y in res[m]["yearly"]})
    out = {"years": [str(y) for y in years], "series": []}
    for m in models:
        vals = [round(res[m]["yearly"].get(str(y),
                res[m]["yearly"].get(y, 0)) * 100, 1) for y in years]
        out["series"].append({"name": m, "data": vals})
    return out


def sens_charts(sens: dict):
    out = {}
    for model, grid in sens.items():
        ns = [50, 100, 200]
        rebs = [10, 20, 40]
        ann, shp = [], []
        for n in ns:
            ra, rs = [], []
            for reb in rebs:
                g = grid.get(f"n{n}_reb{reb}", {})
                ra.append(round(g.get("annual", 0) * 100, 1))
                rs.append(g.get("sharpe", 0))
            ann.append(ra)
            shp.append(rs)
        out[model] = {"ns": [str(n) for n in ns], "rebs": [str(r) for r in rebs],
                      "ann": ann, "sharpe": shp,
                      "methods": {k: v for k, v in grid.items()
                                  if "method" in k or k.endswith(("equal", "min_var"))}}
    return out


def main():
    d = load_all()
    res = d["res"]

    # —— 汇总表 ——
    def fmt_row(name, r, window="full"):
        w = r[window]
        ic = r["ic"]
        return {
            "model": name, "config": r.get("config", {}),
            "annual": round(w["annual"] * 100, 1),
            "sharpe": w["sharpe"], "mdd": round(w["mdd"] * 100, 1),
            "calmar": w["calmar"], "vol": round(w["vol"] * 100, 1),
            "excess": round(w["excess"] * 100, 1), "ir": w["ir"],
            "turnover": round(w["turnover"] * 100, 1),
            "ic": ic["ic_mean"], "icir": ic["icir"],
            "ic_win": round(ic["ic_win"] * 100, 1),
            "yearly": r["yearly"],
            "phases": r["phases"],
            "is_oos": r["is_oos"],
            "focus": {k: round(v * 100, 1) if k in ("annual", "mdd",
                     "excess", "vol", "excess_mdd") else v
                      for k, v in r["focus"].items()},
        }

    summary = [fmt_row(m, r) for m, r in res.items()]

    all_models_factors = []
    for m, r in res.items():
        cfg = r.get("config", {})
        for f in cfg.get("factors", []) + cfg.get("base", []) + \
                ([cfg.get("refine")] if cfg.get("refine") else []):
            if f and f not in all_models_factors:
                all_models_factors.append(f)

    payload = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "summary": summary,
        "scatter": scatter_data(d["ic20"]),
        "shortlist": d["short"].head(40).to_dict("records"),
        "contrib": d["contrib"].to_dict("records"),
        "sel_ex": d["sel_ex"].to_dict("records"),
        "sel_all": d["sel_all"].to_dict("records"),
        "nav": nav_series(d["curves"]),
        "roll_ic": roll_ic(d["curves"]),
        "ic_hist": ic_hist(d["curves"]),
        "corr": corr_subset(d["corr"], all_models_factors),
        "yearly": yearly_chart(res),
        "sens": sens_charts(d.get("sens", {})),
        "si": d.get("si"),
        "sif": (d.get("sif") or {}).get("SIF"),
        "om_leg": (d.get("sif") or {}).get("OM_LEG"),
        "sif_curves": [{"name": k,
                        "dates": [str(pd.Timestamp(x).date())
                                  for x in v["date"]],
                        "nav": [round(float(y), 4) for y in v["nav"]]}
                       for k, v in (d.get("sif_curves") or {}).items()],
        "cap": d.get("cap"),
        "hfadd": d.get("hfadd"),
        "hfrob": d.get("hfrob"),
        "hf2": d.get("hf2"),
        "hf3": d.get("hf3"),
        "crash": d.get("crash"),
    }

    # 模板以 {{ }} 转义花括号（历史 .format 遗留）；先还原再注入 payload，
    # 避免 payload JSON 中的单括号被二次替换。
    html = TMPL.replace("{{", "{").replace("}}", "}")
    html = (html
            .replace("__PAYLOAD__",
                     json.dumps(payload, ensure_ascii=False))
            .replace("__GENERATED__", payload["generated"]))
    REPORT.write_text(html, encoding="utf-8")
    print(f"报告已生成: {REPORT}")


TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>生产模型重构：因子池深度利用与多模型构建</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
:root{--bg:#f7f8fa;--card:#ffffff;--text:#1c2330;--muted:#6b7688;
--line:#e3e7ee;--accent:#2f6fd6;--red:#d5453c;--green:#1d9e75;--amber:#d98a1f;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.65;padding:36px 20px;}
.wrap{max-width:1120px;margin:0 auto;}
h1{font-size:24px;font-weight:700;}
h2{font-size:17px;font-weight:600;margin:40px 0 14px;padding-left:12px;border-left:4px solid var(--accent);}
h3{font-size:14px;font-weight:600;margin:18px 0 8px;}
.sub{color:var(--muted);font-size:13px;margin-top:6px;}
.meta{color:var(--muted);font-size:12px;margin-top:4px;}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0;}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;}
.card .l{color:var(--muted);font-size:12px;}
.card .v{font-size:21px;font-weight:700;margin-top:4px;}
.card .e{font-size:11px;color:var(--muted);margin-top:2px;}
.chart{width:100%;height:400px;margin:8px 0;background:var(--card);border:1px solid var(--line);border-radius:12px;}
.chart-sm{height:300px;}
table{width:100%;border-collapse:collapse;margin:12px 0;font-size:12.5px;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;}
th,td{padding:7px 10px;text-align:right;border-bottom:1px solid var(--line);}
th{color:var(--muted);font-weight:600;background:#f0f2f6;white-space:nowrap;}
td:first-child,th:first-child{text-align:left;}
.tbl-scroll{overflow-x:auto;margin:12px 0;}
.pos{color:var(--red);font-weight:600;}
.neg{color:var(--green);font-weight:600;}
.note{color:var(--muted);font-size:12px;margin:6px 0 14px;}
.concl{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:10px;padding:16px 18px;margin:16px 0;font-size:13.5px;}
.concl b{color:var(--accent);}
.tag{display:inline-block;font-size:11px;padding:1px 8px;border-radius:8px;background:#eef3fb;color:var(--accent);margin-right:6px;}
</style>
</head>
<body>
<div class="wrap">
<h1>生产模型重构：因子池深度利用与多模型构建</h1>
<p class="sub">基于 260 因子全池评估 → 相关性去冗余 → 贡献度检验 → 多候选生产模型统一回测 / 敏感性 / 稳定性分析</p>
<p class="meta">生成时间 __GENERATED__ · 宇宙 ashare_ex（剔 ST/次新/北交所，约 5013 只）· 数据 2022-01-04 ~ 2026-09-04 · 回测 top100 / 20 日调仓 / inverse_vol / 全成本（佣金万2.5+印花税千1+滑点10bp）· 基准中证1000</p>

<h2>一、核心结论（TL;DR）</h2>
<div class="concl" id="conclBox">加载中…</div>

<h2>二、因子池有效性评估（260 因子）</h2>
<p class="note">Rank IC20（20 日前瞻收益，与生产调仓频率对齐）。横轴=覆盖率，纵轴=|ICIR|。Alpha191 与风格/事件/高频家族分色。|ICIR|≥0.2、覆盖≥80%、全历史者进入卫星候选。</p>
<div id="chartScatter" class="chart"></div>

<h3>2.1 短名单（|ICIR| 前 40，含 IC10 衰减对照）</h3>
<div class="tbl-scroll"><table id="tblShort"></table></div>

<h2>三、相关性与去冗余</h2>
<p class="note">核心簇 {size, amihud_20, avg_amount_20} 同聚类，avg_amount_20 被排除；卫星经贪心筛选与已选因子 |ρ|&lt;0.7。下图为最终入选因子的截面秩相关矩阵。</p>
<div id="chartCorr" class="chart" style="height:440px"></div>
<h3>3.1 卫星筛选明细（EX 组 = 生产候选）</h3>
<div class="tbl-scroll"><table id="tblSelEx"></table></div>

<h2>四、因子贡献度（leave-one-in）</h2>
<p class="note">在核心 size+amihud_20 上逐个加入卫星，度量复合评分 ICIR 增量。alpha191 组用于复核既往 batch7/8「卫星稀释」结论。</p>
<div id="chartContrib" class="chart chart-sm"></div>
<div class="tbl-scroll"><table id="tblContrib"></table></div>

<h2>五、模型构建细节</h2>
<div class="tbl-scroll"><table id="tblConfig"></table></div>
<p class="note">合成方法：每日截面 rank 标准化到 [0,1] → 方向校正（负 IC 因子取反）→ 等权 / ICIR 加权。POOL_REFINE：基线评分选池 400 只 → 精选因子池内二次排序。PROD_SI 仅 2025-05+ 短窗（sue_i 数据 2025-04 起），与其它模型不可直接比全期。</p>

<h2>六、回测结果对比</h2>
<div class="cards" id="cardsHero"></div>
<div class="tbl-scroll"><table id="tblMain"></table></div>
<h3>6.1 净值曲线（全期 2022-07 ~ 2026-09）</h3>
<div id="chartNav" class="chart"></div>
<h3>6.2 年度收益（%）</h3>
<div id="chartYear" class="chart chart-sm"></div>

<h2>七、稳定性分析</h2>
<h3>7.1 分年度表现（稳定性第一检验：逐年都不差才算稳）</h3>
<div id="chartYearlyStab" class="chart chart-sm"></div>
<h3>7.2 相位稳健性：4 个错位起点的焦点窗口（2025-05 起）</h3>
<div class="tbl-scroll"><table id="tblPhases"></table></div>
<h3>7.3 样本内 / 样本外（2025-01-01 切分）</h3>
<div class="tbl-scroll"><table id="tblOOS"></table></div>
<h3>7.4 滚动 60 日 IC（复合评分预测力的时变）</h3>
<div id="chartRollIC" class="chart"></div>
<h3>7.5 IC 分布</h3>
<div id="chartIcHist" class="chart chart-sm"></div>

<h2>八、敏感性分析</h2>
<p class="note">选股数 × 调仓频率网格（inverse_vol），及等权 / 最小方差权重法对照。好模型应在参数邻域内表现平滑，而非尖峰依赖。</p>
<div id="sensWrap"></div>

<div id="siDeepSection"></div>
<div id="siFastSection"></div>
<div id="capSection"></div>
<div id="hfSection"></div>
<div id="hf2Section"></div>
<div id="hf3Section"></div>
<div id="crashSection"></div>

<h2>九、生产接入建议</h2>
<div class="concl" id="conclProd"></div>

<p class="note">报告由 scripts/prodmodel_report.py 自动生成。历史回测含幸存者偏差控制（ashare_ex 剔除规则逐日生效由 universe 模块保证），但仍有成本模型简化风险。仅供研究，不构成投资建议。</p>
</div>

<script>
const P = __PAYLOAD__;
const C = {{accent:'#2f6fd6',red:'#d5453c',green:'#1d9e75',amber:'#d98a1f',purple:'#7b5cd6',teal:'#128f8b',gray:'#6b7688',line:'#e3e7ee',text:'#1c2330'}};
const PALETTE=[C.accent,C.amber,C.green,C.purple,C.teal,C.red,C.gray];
const axis={{axisLine:{{lineStyle:{{color:'#c9d0da'}}}},axisLabel:{{color:C.gray}},splitLine:{{lineStyle:{{color:C.line}}}}}};
const fmt=(v,d=1)=>(v>=0?'+':'')+v.toFixed(d);
function tbl(el,head,rows,fmtFns){{
  const t=document.getElementById(el);
  t.innerHTML='<thead><tr>'+head.map(h=>'<th>'+h+'</th>').join('')+'</tr></thead><tbody>'+
    rows.map(r=>'<tr>'+r.map((v,i)=>{{const f=fmtFns&&fmtFns[i]||((x)=>x);const sv=(typeof v==='string')?v:f(v);const cls=(f===fmtPct&&typeof v==='number'&&v<0)?'neg':(f===fmtPct&&typeof v==='number'&&v>0?'pos':'');return '<td class="'+cls+'">'+sv+'</td>'}}).join('')+'</tr>').join('')+'</tbody>';
}}
const fmtPct=v=>v==null?'-':v.toFixed(1)+'%';
const fmtN=v=>v==null?'-':v;
const fmtS=v=>v==null?'-':String(v);

// TL;DR
(function(){{
  const s=P.summary;
  const g=n=>{{const x=s.find(y=>y.model===n);return x;}};
  const prod=g('PROD'), corex=g('CORE_EX'), duale=g('DUAL_E'), psi=g('PROD_SI');
  const f=x=>x?fmt(x.annual)+'%/夏普'+x.sharpe+'/回撤'+fmt(x.mdd)+'%':'-';
  document.getElementById('conclBox').innerHTML=
    '<b>1）因子池：</b>260 因子全池 IC20 评估，163 个 |ICIR|≥0.15 且覆盖≥60%；'+
    '卫星去冗余后 EX 组 8 个（dragon_net_20 / amount_std_20 / lockup_pressure_60 / max_return_20 / momentum_60 / skewness_20 / momentum_20 / ev_high_vol_20）。'+
    '<br><b>2）诚实负面结论：</b>卫星显著提升复合 IC（0.42→0.64）与逐年 IC 稳定性（ICIR 0.80 vs 0.42），'+
    '但组合级全被稀释——等权 CORE_EX 年化 '+fmt(corex.annual)+'% vs 现役 PROD '+fmt(prod.annual)+'%，换手翻倍（48% vs 20%）；'+
    '两块式混合即使 w=0.1（DUAL_E '+fmt(duale.annual)+'%）仍未超越 PROD。与既往 batch7/8「a191 卫星稀释」结论互相印证：'+
    '<b>截面正交 ≠ 选股边际贡献</b>。'+
    '<br><b>3）真实增量在 PROD_SI：</b>短窗（2025-05+，sue_i 数据起点）年化 '+fmt(psi.annual)+'%/夏普 '+psi.sharpe+
    '/回撤 '+fmt(psi.mdd)+'%，全面优于 PROD 同窗（'+fmt(prod.focus.annual)+'%/夏普'+(prod.focus.sharpe/100).toFixed(2)+'/回撤'+fmt(prod.focus.mdd)+'%）——'+
    '继续纸面记账积累前向样本，≥60 交易日后按双闸门裁决。'+
    '<br><b>4）已落地：</b>PROD_DUAL（两块式 w=0.1 观察仓）接入每日流水线记账；现役生产 PROD 保持不变。';
}})();

// 散点
(function(){{
  echarts.init(document.getElementById('chartScatter')).setOption({{
    grid:{{left:60,right:30,top:40,bottom:50}},
    tooltip:{{formatter:p=>p.data[2]+'<br>覆盖率 '+p.data[0]+'% · |ICIR| '+p.data[1]+' · IC '+p.data[3]}},
    legend:{{top:0,textStyle:{{color:C.gray}}}},
    xAxis:{{type:'value',name:'覆盖率%',nameTextStyle:{{color:C.gray}},max:100,...axis}},
    yAxis:{{type:'value',name:'|ICIR|',nameTextStyle:{{color:C.gray}},...axis}},
    series:P.scatter.map((s,i)=>({{name:s.name,type:'scatter',symbolSize:7,
      itemStyle:{{color:PALETTE[i%PALETTE.length],opacity:0.75}},data:s.data}}))
  }});
}})();

// 短名单表
(function(){{
  const rows=P.shortlist.map(r=>[r.factor,r.ic_mean,r.icir,fmtPct(r.ic_win*100),
    r.n_years_pos+'/'+r.n_years,r.ic10_icir,r.data_start]);
  tbl('tblShort',['因子','IC20','ICIR20','IC胜率','正IC年数','ICIR10','数据起点'],rows,
      [fmtS,null,null,fmtPct,fmtS,null,fmtS]);
}})();

// 相关热力图
(function(){{
  const fs=P.corr.factors;
  echarts.init(document.getElementById('chartCorr')).setOption({{
    tooltip:{{position:'top'}},
    grid:{{left:120,bottom:90,right:60,top:10}},
    xAxis:{{type:'category',data:fs,axisLabel:{{color:C.gray,rotate:45,fontSize:10}}}},
    yAxis:{{type:'category',data:fs,axisLabel:{{color:C.gray,fontSize:10}}}},
    visualMap:{{min:-1,max:1,calculable:true,orient:'horizontal',left:'center',bottom:0,
      inRange:{{color:['#1d9e75','#ffffff','#d5453c']}}}},
    series:[{{type:'heatmap',data:(()=>{{const a=[];fs.forEach((f,i)=>fs.forEach((g,j)=>a.push([j,i,P.corr.matrix[i][j]])));return a}})(),
      label:{{show:fs.length<=14,fontSize:9,formatter:p=>p.value[2].toFixed(2)}}}}]
  }});
}})();

// 卫星筛选明细
(function(){{
  const rows=P.sel_ex.map(r=>[r.factor,r.icir,r.picked?'✓ 入选':'✗ 剔除',r.reason]);
  tbl('tblSelEx',['因子','ICIR20','是否入选','原因（与已选因子最大|ρ|）'],rows,[fmtS,null,fmtS,fmtS]);
}})();

// 贡献度条形
(function(){{
  const rows=P.contrib;
  rows.sort((a,b)=>b.d_icir-a.d_icir);
  echarts.init(document.getElementById('chartContrib')).setOption({{
    grid:{{left:150,right:60,top:10,bottom:30}},
    tooltip:{{}},
    xAxis:{{type:'value',name:'ΔICIR',nameTextStyle:{{color:C.gray}},...axis}},
    yAxis:{{type:'category',data:rows.map(r=>r.factor),axisLabel:{{color:C.text,fontSize:11}}}},
    series:[{{type:'bar',data:rows.map(r=>({{value:r.d_icir,itemStyle:{{color:r.d_icir>=0?C.red:C.green,borderRadius:3}}}})),
      label:{{show:true,position:'right',fontSize:10,formatter:p=>p.value.toFixed(3)}}}}]
  }});
  tbl('tblContrib',['因子','组','核心 ICIR','加入后 ICIR','ΔICIR'],
      rows.map(r=>[r.factor,r.group,r.icir_core,r.icir_add,r.d_icir]),[fmtS,fmtS,null,null,null]);
}})();

// 模型配置
(function(){{
  const rows=P.summary.map(s=>{{
    const c=s.config||{{}};
    let f=c.factors?c.factors.join(' + '):(c.base?c.base.join('+')+' →池400→ '+c.refine:'—');
    if(c.window)f+='（'+c.window+'）';
    return [s.model,f,c.method||'rank等权',s.turnover+'%'];
  }});
  tbl('tblConfig',['模型','因子/结构','加权方式','平均换手/期'],rows,[fmtS,fmtS,fmtS,fmtS]);
}})();

// 主表
(function(){{
  const head=['模型','年化%','夏普','回撤%','Calmar','波动%','超额年化%','IR','IC20','ICIR','IC胜率%'];
  const rows=P.summary.map(s=>[s.model,s.annual,s.sharpe,s.mdd,s.calmar,s.vol,s.excess,s.ir,s.ic,s.icir,s.ic_win]);
  tbl('tblMain',head,rows,[fmtS,fmtPct,null,fmtPct,null,fmtPct,fmtPct,null,null,null,fmtPct]);
  const hero=P.summary.filter(x=>['PROD','CORE_EX','CORE_EX_IW','POOL_REFINE'].includes(x.model));
  document.getElementById('cardsHero').innerHTML=hero.map((x,i)=>
    '<div class="card"><div class="l">'+x.model+'</div><div class="v" style="color:'+PALETTE[i%PALETTE.length]+'">'+fmt(x.annual)+'%</div>'+
    '<div class="e">夏普 '+x.sharpe+' · 回撤 '+fmt(x.mdd)+'% · Calmar '+x.calmar+'</div></div>').join('');
}})();

// 净值
(function(){{
  const series=P.nav.map((s,i)=>({{name:s.name,type:'line',symbol:'none',smooth:true,
    lineStyle:{{width:2,color:PALETTE[i%PALETTE.length]}},
    data:s.dates.map((d,j)=>[d,s.nav[j]])}}));
  series.push({{name:'中证1000',type:'line',symbol:'none',smooth:true,lineStyle:{{width:1.5,type:'dashed',color:C.gray}},
    data:P.nav[0].dates.map((d,j)=>[d,P.nav[0].bm[j]])}});
  echarts.init(document.getElementById('chartNav')).setOption({{
    grid:{{left:70,right:30,top:40,bottom:50}},
    tooltip:{{trigger:'axis'}},
    legend:{{top:0,textStyle:{{color:C.gray}}}},
    xAxis:{{type:'time',...axis}},yAxis:{{type:'value',scale:true,...axis}},series}});
}})();

// 年度
(function(){{
  const yrs=P.yearly.years;
  const mk=(name,data,i)=>({{name,type:'bar',data:data.map(v=>({{value:v,itemStyle:{{color:v>=0?C.red:C.green,borderRadius:2}}}})),barWidth:12}});
  echarts.init(document.getElementById('chartYear')).setOption({{
    grid:{{left:60,right:20,top:40,bottom:30}},tooltip:{{trigger:'axis'}},
    legend:{{top:0,textStyle:{{color:C.gray}}}},
    xAxis:{{type:'category',data:yrs,...axis,axisLabel:{{color:C.text}}}},
    yAxis:{{type:'value',...axis,axisLabel:{{color:C.gray,formatter:v=>v+'%'}}}},
    series:P.yearly.series.map((s,i)=>Object.assign(mk(s.name,s.data,i),{{name:s.name}}))
  }});
  // 稳定性用同一数据（分年度）
  echarts.init(document.getElementById('chartYearlyStab')).setOption({{
    grid:{{left:60,right:20,top:40,bottom:30}},tooltip:{{trigger:'axis'}},
    legend:{{top:0,textStyle:{{color:C.gray}}}},
    xAxis:{{type:'category',data:yrs,...axis,axisLabel:{{color:C.text}}}},
    yAxis:{{type:'value',...axis,axisLabel:{{color:C.gray,formatter:v=>v+'%'}}}},
    series:P.yearly.series.map((s,i)=>({{name:s.name,type:'line',symbol:'circle',symbolSize:7,
      lineStyle:{{width:2,color:PALETTE[i%PALETTE.length]}},itemStyle:{{color:PALETTE[i%PALETTE.length]}},data:s.data}}))
  }});
}})();

// 相位
(function(){{
  const models=P.summary;
  const rows=[];
  models.forEach(m=>m.phases.forEach(p=>rows.push([m.model,p.start,fmtPct(p.annual*100),p.sharpe,fmtPct(p.mdd*100)])));
  tbl('tblPhases',['模型','相位起点','年化%','夏普','回撤%'],rows,[fmtS,fmtS,fmtPct,null,fmtPct]);
}})();

// OOS
(function(){{
  const rows=[];
  P.summary.forEach(m=>{{
    if(!m.is_oos)return;
    ['in','out'].forEach(seg=>{{
      const x=m.is_oos[seg];
      rows.push([m.model,seg==='in'?'样本内(→2025-01)':'样本外(2025-01→)',fmtPct(x.annual*100),x.sharpe,fmtPct(x.mdd*100),fmtPct(x.excess*100),x.ir]);
    }});
  }});
  tbl('tblOOS',['模型','段','年化%','夏普','回撤%','超额年化%','IR'],rows,[fmtS,fmtS,fmtPct,null,fmtPct,fmtPct,null]);
}})();

// 滚动 IC
(function(){{
  echarts.init(document.getElementById('chartRollIC')).setOption({{
    grid:{{left:70,right:30,top:40,bottom:50}},tooltip:{{trigger:'axis'}},
    legend:{{top:0,textStyle:{{color:C.gray}}}},
    xAxis:{{type:'time',...axis}},yAxis:{{type:'value',scale:true,...axis}},
    series:P.roll_ic.map((s,i)=>({{name:s.name,type:'line',symbol:'none',
      lineStyle:{{width:1.8,color:PALETTE[i%PALETTE.length]}},
      data:s.dates.map((d,j)=>[d,s.v[j]])}}))
  }});
}})();

// IC 分布
(function(){{
  const centers=P.ic_hist[0].centers;
  echarts.init(document.getElementById('chartIcHist')).setOption({{
    grid:{{left:60,right:20,top:40,bottom:40}},tooltip:{{trigger:'axis'}},
    legend:{{top:0,textStyle:{{color:C.gray}}}},
    xAxis:{{type:'category',data:centers.map(v=>v.toFixed(2)),...axis}},
    yAxis:{{type:'value',...axis}},
    series:P.ic_hist.map((s,i)=>({{name:s.name,type:'bar',data:s.counts,barWidth:8,
      itemStyle:{{color:PALETTE[i%PALETTE.length],opacity:0.75}}}}))
  }});
}})();

// 敏感性
(function(){{
  const wrap=document.getElementById('sensWrap');
  const models=Object.keys(P.sens);
  models.forEach((m,idx)=>{{
    const s=P.sens[m];
    const div=document.createElement('div');
    div.innerHTML='<h3>'+m+'：年化%（上）与夏普（下）随 n×reb 变化</h3>'+
      '<div id="sensA'+idx+'" class="chart chart-sm" style="height:260px"></div>'+
      '<div id="sensS'+idx+'" class="chart chart-sm" style="height:260px"></div>'+
      '<div id="sensT'+idx+'"></div>';
    wrap.appendChild(div);
    const hm=(el,data,title,post)=>{{
      echarts.init(document.getElementById(el)).setOption({{
        grid:{{left:80,right:80,top:10,bottom:60}},
        tooltip:{{position:'top'}},
        xAxis:{{type:'category',data:s.rebs,name:'调仓(日)',nameLocation:'middle',nameGap:35,...axis,axisLabel:{{color:C.text}}}},
        yAxis:{{type:'category',data:s.ns,name:'选股数',...axis,axisLabel:{{color:C.text}}}},
        visualMap:{{min:Math.min(...data.flat()),max:Math.max(...data.flat()),calculable:true,orient:'horizontal',left:'center',bottom:0,inRange:{{color:['#1d9e75','#f5d76e','#d5453c']}}}},
        series:[{{type:'heatmap',label:{{show:true,formatter:p=>post(p.value[2])}},data:(()=>{{const a=[];s.ns.forEach((n,i)=>s.rebs.forEach((r,j)=>a.push([j,i,data[i][j]])));return a}})()}}]
      }});
    }};
    hm('sensA'+idx,s.ann,m+' 年化%',v=>v.toFixed(0));
    hm('sensS'+idx,s.sharpe,m+' 夏普',v=>v.toFixed(2));
    const mrows=Object.entries(s.methods).map(([k,v])=>[k,fmtPct(v.annual*100),v.sharpe,fmtPct(v.mdd*100)]);
    tbl('sensT'+idx,['权重法变体','年化%','夏普','回撤%'],mrows,[fmtS,fmtPct,null,fmtPct]);
  }});
}})();

// PROD_SI 深化验证
(function(){{
  if(!P.si)return;
  const S=P.si, wrap=document.getElementById('siDeepSection');
  const v=S.variants, names=Object.keys(v);
  let h='<h2>八·补 PROD_SI 切换前深化验证（短窗 2025-05 ~ 2026-09）</h2>';
  h+='<p class="note">上轮唯一正增量是 PROD_SI，但仅 ~1.3 年证据。本节做切换前四道检验：变体/权重扫描（排除参数巧合）、短窗敏感性网格、滑点压力、月度稳定性。</p>';

  // 1) 变体对比
  h+='<h3>D1 变体与权重扫描（n100 · reb20 · inverse_vol）</h3><div id="siVarChart" class="chart chart-sm"></div><div class="tbl-scroll"><table id="siVarTbl"></table></div>';
  // 2) 敏感性
  h+='<h3>D2 短窗敏感性：年化% 热力图（上 PROD / 下 PROD_SI）</h3><div id="siSensP" class="chart" style="height:250px"></div><div id="siSensS" class="chart" style="height:250px"></div>';
  // 3) 成本压力
  h+='<h3>D3 成本压力：滑点 ×2 / ×3（全换手成本=佣金×2+印花税+滑点×2，基准 0.35%/次）</h3><div class="tbl-scroll"><table id="siCostTbl"></table></div>';
  // 4) 月度
  const M=S.monthly;
  h+='<h3>D4 月度收益对照（近 '+M.n_months+' 个完整月）</h3><div id="siMonChart" class="chart"></div>'+
     '<div class="concl">SI−PROD 配对月差胜率 <b>'+fmtPct(M.diff_win_rate*100)+'</b> · 月均差 '+
     fmtPct(M.diff_mean*100)+' · SI 最差月 '+M.worst_si_month.ym+'（'+fmtPct(M.worst_si_month.ret*100)+'）· 两模型日收益相关 '+S.daily_ret_corr+'</div>';
  wrap.innerHTML=h;

  // 变体柱状+表
  const anns=names.map(n=>+(v[n].annual*100).toFixed(1));
  echarts.init(document.getElementById('siVarChart')).setOption({{
    grid:{{left:60,right:20,top:30,bottom:60}},
    tooltip:{{}},
    xAxis:{{type:'category',data:names,axisLabel:{{color:C.text,rotate:30,fontSize:10}}}},
    yAxis:{{type:'value',...axis,axisLabel:{{color:C.gray,formatter:v=>v+'%'}}}},
    series:[{{type:'bar',data:anns.map(a=>({{value:a,itemStyle:{{color:a>=0?C.red:C.green,borderRadius:3}}}})),
      label:{{show:true,position:'top',fontSize:10,formatter:p=>p.value}}}}]
  }});
  tbl('siVarTbl',['变体','构成','年化%','夏普','回撤%','超额年化%','换手','ICIR'],
    names.map(n=>{{
      const cfg={{'PROD':'核心等权（基线）','PROD_SI':'4 因子等权','V3_W40':'0.6核心+0.4SI块','W30':'0.7核心+0.3SI块','W20':'0.8核心+0.2SI块','SUE_ONLY_W40':'0.6核心+0.4仅sue_i','EQ3':'核心+sue_i 等权'}};
      const x=v[n];
      return [n,cfg[n]||'-',fmtPct(x.annual*100),x.sharpe,fmtPct(x.mdd*100),fmtPct(x.excess*100),fmtPct(x.turnover*100),S.icir[n]];
    }}),[fmtS,fmtS,fmtPct,null,fmtPct,fmtPct,fmtPct,null]);

  // 敏感性热力图（短窗）
  const hm2=(el,grid)=>{{
    const ns=['50','100','200'],rebs=['10','20','40'];
    const data=ns.map(n=>rebs.map(r=>+((grid['n'+n+'_reb'+r]||{{}}).annual*100||0).toFixed(1)));
    echarts.init(document.getElementById(el)).setOption({{
      grid:{{left:80,right:80,top:10,bottom:60}},
      tooltip:{{position:'top'}},
      xAxis:{{type:'category',data:rebs,name:'调仓(日)',nameLocation:'middle',nameGap:35,...axis,axisLabel:{{color:C.text}}}},
      yAxis:{{type:'category',data:ns,name:'选股数',...axis,axisLabel:{{color:C.text}}}},
      visualMap:{{min:Math.min(...data.flat()),max:Math.max(...data.flat()),calculable:true,orient:'horizontal',left:'center',bottom:0,inRange:{{color:['#1d9e75','#f5d76e','#d5453c']}}}},
      series:[{{type:'heatmap',label:{{show:true}},data:(()=>{{const a=[];ns.forEach((n,i)=>rebs.forEach((r,j)=>a.push([j,i,data[i][j]])));return a}})()}}]
    }});
  }};
  hm2('siSensP',S.sensitivity.PROD);
  hm2('siSensS',S.sensitivity.PROD_SI);

  // 成本压力
  const ck=Object.keys(S.cost_stress);
  tbl('siCostTbl',['口径','年化%','夏普','回撤%'],
    ck.map(k=>[k,fmtPct(S.cost_stress[k].annual*100),S.cost_stress[k].sharpe,fmtPct(S.cost_stress[k].mdd*100)]),
    [fmtS,fmtPct,null,fmtPct]);

  // 月度
  echarts.init(document.getElementById('siMonChart')).setOption({{
    grid:{{left:60,right:20,top:40,bottom:40}},tooltip:{{trigger:'axis'}},
    legend:{{top:0,textStyle:{{color:C.gray}}}},
    xAxis:{{type:'category',data:M.months,...axis,axisLabel:{{color:C.text,rotate:30,fontSize:10}}}},
    yAxis:{{type:'value',...axis,axisLabel:{{color:C.gray,formatter:v=>v+'%'}}}},
    series:[
      {{name:'PROD',type:'bar',data:M.prod.map(v=>+(v*100).toFixed(1)),itemStyle:{{color:C.gray,opacity:0.7}},barWidth:10}},
      {{name:'PROD_SI',type:'bar',data:M.si.map(v=>+(v*100).toFixed(1)),itemStyle:{{color:C.accent}},barWidth:10}}
    ]
  }});
}})();

// sue_i_fast 历史回补验证
(function(){{
  if(!P.sif||!P.om_leg)return;
  const S=P.sif, OM=P.om_leg, wrap=document.getElementById('siFastSection');
  const w0=S._window_start||'?';
  let h='<h2>八·补2 sue_i_fast 历史回补验证（4 季快窗变体）</h2>';
  h+='<p class="note">标准 sue 框架（8 季窗）受财务数据 2021Q1 起限制最早 2025-04 出值。'+
     'sue_i_fast 把窗口缩到 4 季（σ 仅 4 期观测，更噪），有效起点提前到 2023 中——'+
     '多出约 2 年含 2024 微盘股灾年的检验窗。<b>这是因子定义变体，用于稳健性三角验证。</b></p>';
  const omk=Object.keys(OM);
  h+='<h3>E1 全历史单腿检验：overnight_mom_20 加入核心（2022-07+）</h3><div class="tbl-scroll"><table id="sifOmTbl"></table></div>';
  const sk=Object.keys(S).filter(k=>!k.startsWith('_')&&S[k].annual!==undefined);
  h+='<h3>E2 sue_i_fast 全期检验（'+w0+' ~ 2026-09，n100/reb20）</h3><div class="tbl-scroll"><table id="sifTbl"></table></div>';
  h+='<h3>E3 净值对照（各模型自有起点）</h3><div id="sifNav" class="chart"></div>';
  wrap.innerHTML=h;

  const rowOf=(n,x)=>[n,fmtPct(x.annual*100),x.sharpe,fmtPct(x.mdd*100),
    x.icir!=null?x.icir:'-',fmtPct(x.turnover*100),
    x.is_oos?('样本外 '+fmtPct(x.is_oos.out.annual*100)+'/夏普'+x.is_oos.out.sharpe):'-'];
  tbl('sifOmTbl',['模型','年化%','夏普','回撤%','ICIR','换手','IS/OOS'],
    omk.map(k=>rowOf(k+'（2022-07+）',OM[k])),[fmtS,fmtPct,null,fmtPct,null,fmtPct,fmtS]);
  tbl('sifTbl',['模型','年化%','夏普','回撤%','ICIR','换手','IS/OOS(2025-01+)'],
    sk.map(k=>rowOf(k,S[k])),[fmtS,fmtPct,null,fmtPct,null,fmtPct,fmtS]);

  const PA=[C.gray,C.amber,C.accent];
  echarts.init(document.getElementById('sifNav')).setOption({{
    grid:{{left:70,right:30,top:40,bottom:50}},tooltip:{{trigger:'axis'}},
    legend:{{top:0,textStyle:{{color:C.gray}}}},
    xAxis:{{type:'time',...axis}},yAxis:{{type:'value',scale:true,...axis}},
    series:P.sif_curves.map((s,i)=>({{name:s.name,type:'line',symbol:'none',
      lineStyle:{{width:2,color:PA[i%PA.length]}},
      data:s.dates.map((d,j)=>[d,s.nav[j]])}}))
  }});
}})();

// 容量与可成交性
(function(){{
  if(!P.cap)return;
  const C_=P.cap, wrap=document.getElementById('capSection');
  const ms=Object.keys(C_.capacity);
  let h='<h2>八·补3 容量与可成交性压力测试</h2>';
  h+='<p class="note">ADV 法：单股持仓 / 该股 20 日均成交额 ≤10% 为红线。'+
     '冲击成本用平方根模型（10bp 基准滑点 × √(参与率/10%)）。'+
     '年费用拖累 = 单边成本 × 2 × 年换手（EQ3/PROD_SI 年换手 ~7.4x，PROD/PROD_DUAL ~2.8-2.9x，来自回测实测）。</p>';
  h+='<h3>F1 持仓流动性与容量（最新目标持仓）</h3><div class="tbl-scroll"><table id="capTbl1"></table></div>';
  h+='<h3>F2 冲击成本调整后的年费用拖累（给定资金规模）</h3><div class="tbl-scroll"><table id="capTbl2"></table></div>';
  h+='<h3>F3 披露季换手集中度（财报月 1/4/8/10 月 vs 其他月，名单更替率）</h3><div class="tbl-scroll"><table id="capTbl3"></table></div>';
  wrap.innerHTML=h;
  tbl('capTbl1',['模型','持仓数','ADV中位(亿)','ADV 10%分位(亿)','容量·5%线(亿)','容量·中位线(亿)'],
    ms.map(m=>[m,C_.profiles[m].n,C_.profiles[m].adv_med_yi,C_.profiles[m].adv_p10_yi,
      C_.capacity[m].cap_max_5pct_yi,C_.capacity[m].cap_max_median_yi]),
    [fmtS,null,null,null,null,null]);
  const aums=Object.keys(C_.impact[ms[0]]);
  tbl('capTbl2',['模型',...aums.map(a=>a+'(单边bp/年拖累%)')],
    ms.map(m=>[m,...aums.map(a=>C_.impact[m][a].per_side_bp+' / '+C_.impact[m][a].annual_drag_pct)]),
    [fmtS,...aums.map(()=>fmtS)]);
  const tms=Object.keys(C_.turnover_monthly);
  tbl('capTbl3',['模型','披露月均换手','其他月均换手','相对抬升'],
    tms.map(m=>{{
      const t=C_.turnover_monthly[m];
      const rel=(t.disclosure_months_mean/t.other_months_mean-1)*100;
      return [m,fmtPct(t.disclosure_months_mean*100),fmtPct(t.other_months_mean*100),(rel>=0?'+':'')+rel.toFixed(0)+'%'];
    }}),[fmtS,fmtPct,fmtPct,fmtS]);
}})();

// hf 系增量检验（第五轮，用户指令：|IC| 选样负向翻转）
(function(){{
  if(!P.hfadd)return;
  const A=P.hfadd, R=P.hfrob, wrap=document.getElementById('hfSection');
  const order=['PROD','EQ3','HF_AMIH','HF3','HF5','EQ3_HF3','HFWF','EQ3_HFWF']
    .filter(m=>A[m]);
  let h='<h2>八·补4 hf（高频分钟）因子增量检验</h2>';
  h+='<p class="note">用户指令：按 |ICIR| 选样、负 IC 因子按 FACTOR_DIRECTION 翻转方向（做多低值），'+
     'hf 系正式纳入生产候选。统一窗口 2025-02-01~2026-09-04（hf 因子 2025-01-22 起有值），'+
     'top100 / 20日调仓 / inverse_vol / 全成本。方向表已按实测 IC 符号注册入 composite.py。</p>';
  h+='<h3>G1 候选模型同窗对比</h3><div class="tbl-scroll"><table id="hfTbl1"></table></div>';
  const mp=A._monthly_vs_prod||{{}};
  h+='<h3>G2 月度配对差 vs PROD（19 个完整月）</h3><div class="tbl-scroll"><table id="hfTbl2"></table></div>';
  if(R){{
    h+='<h3>G3 稳健性：n×reb 网格范围与滑点压力</h3><div class="tbl-scroll"><table id="hfTbl3"></table></div>';
  }}
  wrap.innerHTML=h;
  tbl('hfTbl1',['模型','因子构成','年化%','夏普','回撤%','换手','IC','ICIR','2025','2026'],
    order.map(m=>{{
      const r=A[m];
      return [m,r.factors.join('+'),(r.annual*100).toFixed(1),r.sharpe,(r.mdd*100).toFixed(1),
        (r.turnover*100).toFixed(1),r.ic.ic_mean,r.ic.icir,
        (r.yearly['2025']*100).toFixed(1),(r.yearly['2026']*100).toFixed(1)];
    }}),[fmtS,fmtS,fmtPct,null,fmtPct,fmtPct,null,null,fmtPct,fmtPct]);
  tbl('hfTbl2',['模型','月配对胜率','月均差(pp)'],
    order.filter(m=>mp[m]).map(m=>[m,fmtPct(mp[m].win*100),mp[m].mean_pp]),
    [fmtS,fmtPct,fmtS]);
  if(R){{
    const ms=Object.keys(R.grid);
    tbl('hfTbl3',['模型','网格年化范围%','网格夏普范围','滑点×2 年化%/夏普','滑点×3 年化%/夏普'],
      ms.map(m=>{{
        const g=Object.values(R.grid[m]).filter(v=>v.annual!==undefined);
        const anns=g.map(v=>v.annual),shs=g.map(v=>v.sharpe);
        const c2=R.cost[m+'_x2'],c3=R.cost[m+'_x3'];
        return [m,
          (Math.min(...anns)*100).toFixed(1)+'-'+(Math.max(...anns)*100).toFixed(1),
          Math.min(...shs)+'-'+Math.max(...shs),
          (c2.annual*100).toFixed(1)+' / '+c2.sharpe,
          (c3.annual*100).toFixed(1)+' / '+c3.sharpe];
      }}),[fmtS,fmtS,fmtS,fmtS,fmtS]);
  }}
}})();

// 第六轮：增量合并 + 容量 + 持仓归因
(function(){{
  if(!P.hf2)return;
  const B=P.hf2, wrap=document.getElementById('hf2Section');
  const order=['PROD','EQ3','HF_AMIH','EQ3_HFA','HFA_OM'].filter(m=>B[m]);
  let h='<h2>八·补5 第六轮：增量合并、容量补测与持仓归因</h2>';
  h+='<p class="note">两个已验证增量（sue_i 事件维度、hf_amihud_20 分钟流动性精化）此前从未合并测试。'+
     '同窗口径同上。overnight_mom_20 再度作为反例入组（第三轮单腿全历史不过）。</p>';
  h+='<h3>H1 合并候选同窗对比</h3><div class="tbl-scroll"><table id="hf2Tbl1"></table></div>';
  const md=B._monthly||{{}};
  h+='<h3>H2 月度配对差（19 个完整月）</h3><div class="tbl-scroll"><table id="hf2Tbl2"></table></div>';
  h+='<h3>H3 最新持仓 ADV 画像（亿）与 top100 重合度</h3><div class="tbl-scroll"><table id="hf2Tbl3"></table></div>';
  wrap.innerHTML=h;
  tbl('hf2Tbl1',['模型','因子构成','年化%','夏普','回撤%','换手','ICIR','2025%','2026%'],
    order.map(m=>{{
      const r=B[m];
      return [m,r.factors.join('+'),(r.annual*100).toFixed(1),r.sharpe,(r.mdd*100).toFixed(1),
        (r.turnover*100).toFixed(1),r.ic.icir,(r.yearly['2025']*100).toFixed(1),(r.yearly['2026']*100).toFixed(1)];
    }}),[fmtS,fmtS,fmtPct,null,fmtPct,fmtPct,null,fmtPct,fmtPct]);
  const mk=Object.keys(md);
  tbl('hf2Tbl2',['配对','月胜率','月均差(pp)'],
    mk.map(k=>[k.replace('_vs_',' vs '),fmtPct(md[k].win*100),md[k].mean_pp]),
    [fmtS,fmtPct,fmtS]);
  const cap=B._capacity, ovl=B._overlap_top100;
  tbl('hf2Tbl3',['模型','ADV中位(亿)','ADV 10%分位(亿)',
    ...Object.keys(ovl).map(k=>k.replace('|','∩'))],
    order.map(m=>[m,cap[m].adv_med_yi,cap[m].adv_p10_yi,
      ...Object.keys(ovl).map(k=>{{
        const p=k.split('|');
        return (p[0]===m||p[1]===m)?ovl[k]:'-';
      }})]),
    [fmtS,null,null,...Object.keys(ovl).map(()=>null)]);
}})();

// 第七轮：子域检验 / 权重法 / 独特信息归因
(function(){{
  if(!P.hf3)return;
  const T=P.hf3, wrap=document.getElementById('hf3Section');
  let h='<h2>八·补6 第七轮：子域检验、权重法与独特信息归因</h2>';
  h+='<p class="note">① 剔除每日市值底部 30% 后重测（回答"是否纯微盘 beta"）；'+
     '② EQ3_HFA 的 ICIR 加权变体；③ hf_amihud_20 相对 amihud_20 的秩残差 IC（独特信息量化）。</p>';
  h+='<h3>I1 子域（剔市值底 30%，保留 ~70%）+ ICW 变体</h3><div class="tbl-scroll"><table id="hf3Tbl1"></table></div>';
  const rs=T.RESID_HF_AMIH;
  h+='<h3>I2 hf_amihud 独特信息（秩残差 IC20）</h3>'+
     '<div class="concl">残差 IC '+rs.ic_mean+'（ICIR '+rs.icir+'，'+rs.n_days+' 日；'+
     '2025: '+rs.yearly['2025']+' / 2026: '+rs.yearly['2026']+'）——独特信息弱但为正且 2026 年增强，'+
     '与"ρ=0.86 仍贡献组合增量"一致：增量主要来自头部选股的边际重排而非全域预测力。</div>';
  wrap.innerHTML=h;
  const rows1=[
    ['PROD（全池对照）','56.3','2.01','-25.6','0.484'],
    ['EQ3（全池对照）','61.3','2.29','-22.6','0.588'],
    ['EQ3_HFA（全池对照）','71.7','2.79','-20.7','0.629'],
  ];
  const sub=['SUB_PROD','SUB_EQ3','SUB_EQ3_HFA'].filter(k=>T[k]);
  sub.forEach(k=>{{
    const v=T[k];
    rows1.push([k.replace('SUB_','')+'（剔微盘）',(v.annual*100).toFixed(1),v.sharpe,(v.mdd*100).toFixed(1),v.ic.icir]);
  }});
  if(T.EQ3_HFA_ICW){{
    const v=T.EQ3_HFA_ICW;
    rows1.push(['EQ3_HFA_ICW（ICIR 加权）',(v.annual*100).toFixed(1),v.sharpe,(v.mdd*100).toFixed(1),v.ic.icir]);
  }}
  tbl('hf3Tbl1',['模型','年化%','夏普','回撤%','ICIR'],rows1,
    [fmtS,fmtPct,null,fmtPct,null]);
}})();

// 第八轮：2024 微盘股灾静态回放
(function(){{
  if(!P.crash)return;
  const K=P.crash, wrap=document.getElementById('crashSection');
  const order=['PROD','EQ3','HF_AMIH','EQ3_HFA','EQ3_HFA_ICW'].filter(m=>K[m]);
  const wins=[['crash_2024Q1','股灾谷底 2024-02-05'],['crash_plus_rebound','含反弹 2024-02-08'],['full_2024','2024 全年']];
  let h='<h2>八·补7 极端情景：2024 年初微盘股灾静态回放</h2>';
  h+='<p class="note">hf/sue 证据全在牛市窗，唯一未覆盖的尾部情景是 2024-01~02 微盘股灾。'+
     '方法：取各模型当前最新目标持仓（权重固定）回放该窗口——衡量风格/选股暴露的尾部敏感度，'+
     '非真实调仓路径（持仓会是另一批票，但因子倾斜持续）。基准=中证1000。</p>';
  h+='<div class="tbl-scroll"><table id="crashTbl"></table></div>';
  wrap.innerHTML=h;
  tbl('crashTbl',['模型','持仓数',...wins.map(w=>w[1])+'%','相对基准(股灾)'],
    order.map(m=>{{
      const r=K[m];
      const rel=(r.crash_2024Q1.port-r.crash_2024Q1.bench)*100;
      return [m,r.n_holdings,
        ...wins.map(w=>(r[w[0]].port*100).toFixed(1)),
        (rel>=0?'+':'')+rel.toFixed(1)+'pp'];
    }}),[fmtS,null,...wins.map(()=>fmtPct),fmtS]);
}})();

// 生产建议
(function(){{
  document.getElementById('conclProd').innerHTML=
    '<b>裁决队列（2026-12 前向 60 交易日双闸门）：</b>主候选 PROD_HFA'+
    '（size+amihud_20+sue_i+hf_amihud_20，等权=收益型 / ICW=稳健型）&gt; EQ3 &gt; HF_AMIH；'+
    'PROD_SI / V3_SI / PROD_DUAL 为背景对照。现役 PROD 不动。'+
    '<br><b>检验矩阵已完备：</b>变体单调性 / 参数网格 / 滑点×3 / 容量流动性 / 子域（剔微盘）'+
    ' / 权重法 / 2024 股灾回放——全部无阻碍，hf 窗口仅 ~1.5 年牛市是唯一残留限制。'+
    '<br><b>复核命令：</b>python scripts/gate_review.py（台账前向收益 + 配对差 + 闸门进度）。'+
    '候选账本：PROD / PROD_SI / V3_SI / PROD_DUAL / EQ3 / PROD_HF / PROD_HFA '+
    '（daily_pipeline 每日自动记账）。';
}})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
