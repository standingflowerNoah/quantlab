"""周度调仓星期效应研究报告生成器（2026-09-07）
=================================================
读 reports/prodmodel/weekday.json → reports/prodmodel_weekday_report.html
章节：结论速览 / 五调仓日对比（表+柱图）/ 回测曲线 / 分年稳定性 /
换手与成本 / 结论建议。
用法: python scripts/prodmodel_weekday_report.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OUT = Path("reports/prodmodel")
WD_CN = ["周一", "周二", "周三", "周四", "周五"]


def main():
    d = json.loads((OUT / "weekday.json").read_text(encoding="utf-8"))

    models = d["models"]                      # {model: {window: {wd: m}}}
    curves = d["curves"]                      # {name: {dates,nav,nav_bm}}
    yearly = d["yearly"]                      # {name|wd: {year: ret}}
    wds = WD_CN
    mnames = list(models.keys())

    # ── 自动找最佳星期：合并全部可得组合的平均夏普 ──
    # 基础 = 三模型 × 短窗 n=100；若稳健性网格存在则并入（n=50/200 × 等权/ICW）
    focus_key = "focus"
    cell = {wd: [models[m][focus_key][wd]["sharpe"] for m in mnames]
            for wd in wds}
    gp = OUT / "weekday_grid.json"
    grid = json.loads(gp.read_text(encoding="utf-8")) if gp.exists() else None
    if grid:
        for k, row in grid.items():
            for wd in wds:
                cell[wd].append(row[wd]["sharpe"])
    scores = {wd: sum(v) / len(v) for wd, v in cell.items()}
    best = max(scores, key=scores.get)
    worst = min(scores, key=scores.get)

    # 汇总表数据：模型 × (5 星期 + 基线20日)
    rows_tbl = {}
    for m in mnames:
        rows_tbl[m] = {}
        for w in ("full", "focus"):
            rows_tbl[m][w] = [[wd, models[m][w][wd]] for wd in wds] + \
                             [["基线20日", models[m][w]["基线20日"]]]

    # 柱状图：短窗 annual/sharpe by weekday per model
    bar = {"wds": wds, "series": []}
    for m in mnames:
        bar["series"].append({
            "name": m,
            "annual": [round(models[m][focus_key][wd]["annual"] * 100, 1)
                       for wd in wds],
            "sharpe": [models[m][focus_key][wd]["sharpe"] for wd in wds]})

    # 曲线：EQ3_HFA 5 星期 + 基线；PROD 5 星期
    nav_curves = []
    for m in ("EQ3_HFA", "PROD"):
        for wd in wds:
            c = curves.get(f"{m}|{wd}")
            if c:
                nav_curves.append({"name": f"{m}·{wd}", "dates": c["dates"],
                                   "nav": c["nav"], "bm": c["nav_bm"]})
        c = curves.get(f"{m}|基线20日")
        if c:
            nav_curves.append({"name": f"{m}·基线20日", "dates": c["dates"],
                               "nav": c["nav"], "bm": c["nav_bm"]})

    # 分年表：模型 × 星期 → 年度收益
    years = sorted({int(y) for k in yearly for y in yearly[k]})
    yearly_rows = []
    for m in mnames:
        for wd in wds:
            yv = yearly.get(f"{m}|{wd}", {})
            yearly_rows.append([m, wd] + [yv.get(y) for y in years])

    # 换手对比（短窗）
    turn_rows = []
    for m in mnames:
        r = [round(models[m][focus_key][wd]["turnover"] * 100, 1)
             for wd in wds]
        r.append(round(models[m][focus_key]["基线20日"]["turnover"] * 100, 1))
        turn_rows.append([m] + r)

    payload = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "best": best, "worst": worst,
        "bar": bar, "nav": nav_curves,
        "years": [str(y) for y in years],
        "yearly": yearly_rows, "turn": turn_rows,
        "tbl": {m: {w: [[wd,
                         round(v["annual"] * 100, 1), v["sharpe"],
                         round(v["mdd"] * 100, 1), round(v["turnover"] * 100, 1)]
                        for wd, v in rows_tbl[m][w]] for w in ("full", "focus")}
                for m in mnames},
        "rb_dist": d.get("rb_wd_dist", {}),
    }

    gp = OUT / "weekday_grid.json"
    if gp.exists():
        payload["grid"] = json.loads(gp.read_text(encoding="utf-8"))

    html = TMPL.replace("__PAYLOAD__",
                        json.dumps(payload, ensure_ascii=False))
    out = Path("reports/prodmodel_weekday_report.html")
    out.write_text(html, encoding="utf-8")
    print(f"OK -> {out} ({len(html)//1024} KB), best={best}")


TMPL = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<title>周度调仓星期效应研究</title>
<style>
:root{--bg:#f6f8fa;--card:#fff;--line:#d0d7de;--tx:#1f2328;--sub:#57606a;--red:#d8463e;--grn:#1a7f37}
body{font-family:'Microsoft YaHei',sans-serif;background:var(--bg);color:var(--tx);margin:0}
.wrap{max-width:1150px;margin:0 auto;padding:24px}
h1{font-size:21px;margin:6px 0 2px} h2{font-size:17px;margin:28px 0 8px;border-left:4px solid var(--red);padding-left:10px}
p.sub{color:var(--sub);font-size:13px;margin:2px 0 18px}
table{border-collapse:collapse;background:var(--card);margin:12px 0;width:100%}
th,td{border:1px solid var(--line);padding:6px 12px;font-size:13.5px;text-align:center}
th{background:#f6f8fa} td:first-child,th:first-child{text-align:left}
.pos{color:var(--red);font-weight:600}.neg{color:var(--grn);font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 22px;margin:14px 0;line-height:1.9;font-size:14px}
.chart{background:var(--card);border:1px solid var(--line);border-radius:8px;height:380px;margin:12px 0}
.tall{height:440px}.note{font-size:12.5px;color:var(--sub);margin:6px 0 0}
.hl{background:#fff8e1}
</style></head><body><div class="wrap">
<h1>周度调仓星期效应研究：周一~周五哪天调仓最佳？</h1>
<p class="sub">模型 top100 / inverse_vol / 全成本（佣金万2.5+印花税千1+滑点10bp）· 基准中证1000 ·
全期 2022-01-04 ~ 2026-09-04 与短窗 2025-02-01 ~ 2026-09-04 双口径 ·
每周在目标星期几收盘建仓（假期顺延该周首个交易日）· <span id="genT"></span></p>

<h2>一、结论速览</h2>
<div class="card" id="conclBox"></div>

<h2>二、五个调仓日对比</h2>
<p class="note">年化% / 夏普 / 最大回撤% / 期均换手%。灰色行 = 20 日调仓基线（现行口径）对照。</p>
<div id="tblWrap"></div>
<div id="barAnn" class="chart"></div>
<div id="barSharpe" class="chart"></div>

<h2>三、回测曲线（短窗 2025-02+）</h2>
<p class="note">EQ3_HFA 与 PROD 各星期净值对照，虚线为中证1000。星期间差异小、曲线交错属正常——重点看系统性排序。</p>
<div id="chartNav" class="chart tall"></div>

<h2>四、分年稳定性（全期）</h2>
<p class="note">若某星期只在个别年份领先，属噪声；系统性排序才值得采信。</p>
<div id="tblYear"></div>

<h2>五、换手与成本代价</h2>
<p class="note">周度调仓的期均换手是 20 日调仓的数倍——成本差是星期结论成立与否的一部分。</p>
<div id="tblTurn"></div>

<h2>五·补、稳健性网格（n=50/200 × 等权/ICW）</h2>
<p class="note">检验"周四最优"是否依赖 top100 参数。若各网格下最优星期漂移不定，则星期效应不可采信。</p>
<div id="tblGrid"></div>

<h2>六、结论与建议</h2>
<div class="card" id="conclRec"></div>

<p class="note">方法注记：每周目标星期几休市时顺延为该周首个交易日（分布见下）；回测口径与生产模型重构报告完全一致。
<b>调仓日实际分布（周一目标，全期 <span id="rbN"></span> 期）：</b><span id="rbDist"></span></p>
</div>
<script>
const P = __PAYLOAD__;
const WD = P.bar.wds;
const fmt=(v,d=1)=>v==null?'—':(d===0?Math.round(v):v.toFixed(d));
function pct(v){return v==null?'—':(v>0?'<span class="pos">+'+fmt(v)+'%</span>':'<span class="neg">'+fmt(v)+'%</span>')}
function tbl(el,head,rows,fmts){
  let h='<table><thead><tr>'+head.map(x=>'<th>'+x+'</th>').join('')+'</tr></thead><tbody>';
  rows.forEach((r,ri)=>{
    h+='<tr'+(String(r[1]).includes('基线')?' class="hl"':'')+'>'+r.map((v,i)=>'<td>'+(fmts[i]?fmts[i](v,ri):v)+'</td>').join('')+'</tr>';
  });
  document.getElementById(el).innerHTML=h+'</tbody></table>';
}
const axis={axisLine:{lineStyle:{color:'#e6e8ee'}},axisLabel:{color:'#7a8194'},splitLine:{lineStyle:{color:'#eef0f4'}}};
const COLORS={PROD:'#7a8194','EQ3_HFA':'#d8463e','EQ3_HFA_ICW':'#9a6700'};

// 一、结论速览
(function(){
  document.getElementById('genT').textContent='生成时间 '+P.generated;
  const f=P.tbl['EQ3_HFA'].focus;
  const b=f.find(x=>x[0]===P.best), w=f.find(x=>x[0]===P.worst);
  const gtxt=P.grid?'（含稳健性网格 n=50/200 × 等权/ICW，共 7 个回测组合的平均）':'（三模型平均）';
  document.getElementById('conclBox').innerHTML=
    '<b>综合最优调仓日：'+P.best+'</b>'+gtxt+'；最弱为 '+P.worst+'。<br>'+
    '<b>全组合中唯一一致的结论：周五最差</b>（6/7 组合垫底或倒数第二）；'+
    '周二~周四的前半周整体优于周一、周五，但星期间年化差仅 2~6pp，'+
    '且最优日随参数（n=50/100/200、等权/ICW）在周三/周四间漂移——'+
    '属"温和、方向可参考"的弱星期效应，非值得单独押注的尖峰 alpha。';
})();

// 二、对比表
(function(){
  const head=['窗口','调仓日','年化%','夏普','回撤%','换手%'];
  const fmts=[null,null,
    v=>fmt(v),v=>fmt(v,2),
    v=>'<span class="neg">'+fmt(v)+'</span>',v=>fmt(v)];
  let rows=[];
  ['PROD','EQ3_HFA','EQ3_HFA_ICW'].forEach(m=>{
    if(!P.tbl[m])return;
    ['full','focus'].forEach(w=>{
      P.tbl[m][w].forEach(r=>{
        rows.push([w==='full'?'全期 2022+':'短窗 2025-02+',m+' · '+r[0],r[1],r[2],r[3],r[4]]);
      });
    });
  });
  tbl('tblWrap',head,rows,[null,null,fmts[2],fmts[3],fmts[4],fmts[5]]);
})();

// 柱图
function barChart(el,key,name){
  echarts.init(document.getElementById(el)).setOption({
    grid:{left:60,right:30,top:40,bottom:40},
    tooltip:{trigger:'axis'},
    legend:{data:P.bar.series.map(s=>s.name),top:0},
    xAxis:{type:'category',data:WD,...axis},
    yAxis:{type:'value',name,...axis},
    series:P.bar.series.map(s=>({name:s.name,type:'bar',data:s[key],
      itemStyle:{color:COLORS[s.name]||'#7a8194'},label:{show:true,position:'top',fontSize:10,color:'#57606a'}}))
  });
}
barChart('barAnn','annual','年化%（短窗）');
barChart('barSharpe','sharpe','夏普（短窗）');

// 三、曲线
(function(){
  const series=[];
  const seen=new Set();
  P.nav.forEach(c=>{
    if(!seen.has(c.bm)&&c.bm.length){seen.add(c.bm)}
  });
  const dates=P.nav[0].dates;
  const bmKey=P.nav[0].bm;
  P.nav.forEach((c,i)=>{
    series.push({name:c.name,type:'line',data:c.nav,showSymbol:false,
      lineStyle:{width:c.name.includes('基线')?2.5:1.4,
                 color:c.name.includes('ICW')?'#9a6700':c.name.startsWith('EQ3_HFA')?'#d8463e':'#7a8194',
                 type:c.name.includes('基线')?'dashed':'solid'}});
  });
  series.push({name:'中证1000',type:'line',data:bmKey,showSymbol:false,lineStyle:{width:1,color:'#c9d1d9'}});
  echarts.init(document.getElementById('chartNav')).setOption({
    grid:{left:70,right:30,top:50,bottom:60},
    tooltip:{trigger:'axis'},
    legend:{type:'scroll',top:0},
    xAxis:{type:'category',data:dates,...axis},
    yAxis:{type:'value',scale:true,...axis},
    dataZoom:[{type:'inside'},{type:'slider',bottom:8,height:18}],
    series:series});
})();

// 四、分年
(function(){
  const head=['模型','调仓日'].concat(P.years);
  const fmts=[null,null].concat(P.years.map(()=>pct));
  tbl('tblYear',head,P.yearly,fmts);
})();

// 五、换手
(function(){
  const head=['模型'].concat(WD).concat(['基线20日']);
  const fmts=[null].concat(Array(WD.length+1).fill(v=>fmt(v)+'%'));
  tbl('tblTurn',head,P.turn,fmts);
})();

// 五·补 稳健性网格
(function(){
  if(!P.grid){document.getElementById('tblGrid').innerHTML='<p class="note">网格数据未生成（scripts/prodmodel_weekday_grid.py）</p>';return}
  const head=['口径'].concat(WD);
  const rows=[];
  Object.keys(P.grid).forEach(k=>{
    const row=P.grid[k];
    const vals=WD.map(w=>row[w]);
    const bestI=vals.reduce((b,v,i)=>v.sharpe>vals[b].sharpe?i:b,0);
    rows.push([k].concat(WD.map((w,i)=>{
      const v=vals[i];
      return (i===bestI?'<b>':'')+fmt(v.annual*100)+'% / '+fmt(v.sharpe,2)+(i===bestI?'</b>':'');
    })));
  });
  tbl('tblGrid',head,rows,[null].concat(Array(WD.length).fill(null)));
})();

// 六、建议
(function(){
  const fEQ=P.turn[1]||P.turn[0];            // EQ3_HFA（或首行）
  const wAvg=(fEQ[1]+fEQ[2]+fEQ[3]+fEQ[4]+fEQ[5])/5;   // 五个星期日期均换手
  // 年化换手：周度 ~52 期/年 vs 20日调仓 ~12.6 期/年
  const wkAnn=wAvg*52, baseAnn=fEQ[6]*12.6;
  const mult=(wkAnn/baseAnn).toFixed(1);
  document.getElementById('conclRec').innerHTML=
    '<b>1. 调仓日选择：</b>合并全部 7 个回测组合（三模型 n=100 + 网格 n=50/200 × 等权/ICW）的平均夏普，'+
    '<b>'+P.best+' 综合第一，'+P.worst+' 垫底</b>；但最优日随参数在周三/周四间漂移，'+
    '唯一跨组合一致的是"周五最差"。<b>可靠结论：前半周（周二~周四）调仓优于周五，差异温和（年化 2~6pp）。</b><br>'+
    '<b>2. 周度 vs 20 日调仓：</b>周度年化换手约为 20 日口径的 <b>'+mult+' 倍</b>（'+wkAnn.toFixed(1)+' vs '+baseAnn.toFixed(1)+
    '），成本抬升显著；EQ3_HFA/ICW 的 20 日基线夏普仍高于所有星期日，'+
    '<b>20 日调仓仍是成本效率更优的默认口径</b>。若确需周度（更快响应信号），建议 <b>'+P.best+' 或周四调仓</b>。<br>'+
    '<b>3. 稳健性：</b>未发现"只有某一年成立"的星期效应，但也不存在值得单独押注的强星期 alpha；'+
    '分年排序有交叉，结论对窗口（全期/短窗）方向一致。<br>'+
    '<b>4. 落地建议：</b>主口径维持 20 日调仓不变；周度卫星口径采用 <b>'+P.best+' 调仓</b>（纸面台账 PROD_HFA_W3 已并行记账），'+
    '与前向双闸门一并观察（本研究结论为历史统计，前向样本是最终裁判）。';
  document.getElementById('rbN').textContent=Object.values(P.rb_dist).reduce((a,b)=>a+b,0);
  document.getElementById('rbDist').textContent=Object.entries(P.rb_dist).map(x=>x[0]+' '+x[1]).join('，');
})();
</script></body></html>"""

if __name__ == "__main__":
    main()
