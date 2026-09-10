r"""买入时点研究 —— 生成可视化 HTML 报告（浅色主题，ECharts CDN）"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
REP = ROOT / "reports" / "overnight"
OUT = ROOT / "reports" / "intraday_entry_timing_report.html"

cur = pd.read_csv(REP / "entry_timing_final_curve.csv")
cur = cur[cur["mod"] <= 900].copy()
js = json.loads((REP / "entry_timing_report.json").read_text(encoding="utf-8"))
series = pd.read_csv(REP / "entry_timing_final_series.csv")

# ── 数据打包 ──
curve_x = cur["hm"].tolist()
curve_y = cur["mean_bp"].tolist()
curve_t = cur["t"].tolist()
curve_intra = cur["mean_intra_bp"].tolist()
curve_gap = cur["mean_gap_bp"].tolist()
cont_mask = [int(m) <= 896 for m in cur["mod"]]           # 连续竞价窗口
curve_ok_x = [x for x, k in zip(curve_x, cont_mask) if k]
curve_ok_y = [y for y, k in zip(curve_y, cont_mask) if k]

months = [r["ym"] for r in js["monthly_spread"]]
mspread = [r["open_minus_close"] for r in js["monthly_spread"]]
mwin = [r["win"] for r in js["monthly_spread"]]

liq = pd.DataFrame(js["by_liq"])
liq_tab = liq.pivot(index="hm", columns="q", values="mean_bp")
liq_series = {int(q): liq_tab[q].tolist() for q in liq_tab.columns}
liq_x = liq_tab.index.tolist()

bs = pd.DataFrame(js["bucket_spread"])
bsize = bs[bs.bucket == "size"].sort_values("q")
bliq = bs[bs.bucket == "liq"].sort_values("q")

ext = pd.DataFrame(js["daily_ext"])
ext_y = ext[ext.year != "ALL"]
ext_all = ext[ext.year == "ALL"].iloc[0]

kpi = {
    "best_time": "09:25 – 09:31（开盘）",
    "best_bp": float(cur[cont_mask]["mean_bp"].max()),
    "worst_time": "14:56 – 15:00（收盘）",
    "worst_bp": float(cur[cur["hm"] == "14:56"]["mean_bp"].iloc[0]),
    "spread": [r for r in js["paired"] if r["a"] == "09:31" and r["b"] == "15:00"][0],
    "ext_spread": float(ext_all["spread_bp"]),
    "ext_spread_t": float(ext_all["spread_t"]),
    "ext_open": float(ext_all["open_bp"]),
    "ext_close": float(ext_all["close_bp"]),
    "n_days": js["n_days"],
    "n_obs": js["n_obs"],
    "cost": js["cost_bp"],
    "pos_months": f"{sum(1 for v in mspread if v > 0)}/{len(mspread)}",
    "liq_q5": float(bliq[bliq.q == 5]["spread_bp"].iloc[0]),
    "liq_q5_t": float(bliq[bliq.q == 5]["t"].iloc[0]),
    "liq_q1": float(bliq[bliq.q == 1]["spread_bp"].iloc[0]),
    "liq_q1_t": float(bliq[bliq.q == 1]["t"].iloc[0]),
    "c1456_1500": [r for r in js["paired"] if r["a"] == "14:56"][0],
}

payload = dict(
    kpi=kpi, curve_x=curve_x, curve_y=curve_y, curve_t=curve_t,
    curve_intra=curve_intra, curve_gap=curve_gap,
    ok_x=curve_ok_x, ok_y=curve_ok_y,
    months=months, mspread=mspread, mwin=mwin,
    liq_x=liq_x, liq_series=liq_series,
    size_q=bsize["q"].tolist(),
    size_sp=bsize["spread_bp"].tolist(), size_t=bsize["t"].tolist(),
    liq_q=bliq["q"].tolist(), liq_sp=bliq["spread_bp"].tolist(),
    liq_t=bliq["t"].tolist(), liq_w=bliq["win"].tolist(),
    ext_years=ext_y["year"].astype(int).tolist(),
    ext_open=ext_y["open_bp"].tolist(), ext_close=ext_y["close_bp"].tolist(),
    ext_spread=ext_y["spread_bp"].tolist(),
    cum_spread=series["roll60"].fillna(0).round(2).tolist(),
    series_d=[str(d)[:10] for d in series["d"]],
    curve_table=cur[["hm", "mean_bp", "t", "win", "mean_intra_bp",
                     "mean_gap_bp", "net_bp"]].round(2).values.tolist(),
)

tmpl = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>A股每日最优买入时点研究 · QuantLab</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1b1f26;--muted:#6b7480;--line:#e3e6eb;
--up:#d64545;--down:#1a9e6f;--accent:#2f6fdb;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.65;padding:36px 28px 60px;}
.wrap{max-width:1180px;margin:0 auto;}
h1{font-size:26px;letter-spacing:-.3px;}
h2{font-size:18px;margin:34px 0 12px;padding-left:10px;border-left:4px solid var(--accent);}
h3{font-size:15px;margin:20px 0 8px;color:var(--muted);font-weight:600;}
.sub{color:var(--muted);font-size:13px;margin-top:8px;}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin-top:14px;}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:18px;}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;}
.kpi .lab{font-size:12px;color:var(--muted);}
.kpi .val{font-size:20px;font-weight:700;margin-top:4px;letter-spacing:-.4px;}
.kpi .note{font-size:12px;color:var(--muted);margin-top:2px;}
.up{color:var(--up);} .down{color:var(--down);}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:10px;}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap;}
th:first-child,td:first-child{text-align:left;}
thead th{background:#f0f2f5;color:var(--muted);font-weight:600;font-size:12px;}
tbody tr:hover{background:#fafbfc;}
.chart{width:100%;height:420px;}
.chart.sm{height:330px;}
.note-box{background:#fff8e6;border:1px solid #f0dfae;border-radius:8px;padding:12px 16px;font-size:13px;color:#6b5a20;margin-top:14px;}
.neg-box{background:#fdeeee;border:1px solid #f0c4c4;border-radius:8px;padding:12px 16px;font-size:13px;color:#7a2f2f;margin-top:14px;}
code{background:#eef1f5;padding:1px 5px;border-radius:4px;font-size:12px;}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px;}
@media(max-width:860px){.grid2{grid-template-columns:1fr;}}
</style>
</head>
<body>
<div class="wrap">
<h1>A 股每日最优买入时点研究</h1>
<div class="sub">买入 = T 日盘中某一分钟收盘价成交；卖出 = T+1 开盘价。样本 2025-01-02 → 2026-09-08（__NDAYS__ 个交易日、__NOBS__ 亿条 观测）；日线长样本外延 2022–2026。等权截面均值，t 值按逐日横截面均值序列计算。</div>

<div class="kpis">
  <div class="kpi"><div class="lab">最优买入时点</div><div class="val up">09:25 – 09:31</div><div class="note">开盘（集合竞价 / 首分钟）</div></div>
  <div class="kpi"><div class="lab">最优时点毛收益</div><div class="val up">+__BEST__ bp/日</div><div class="note">年化 +28.6%（未扣费）</div></div>
  <div class="kpi"><div class="lab">买开盘 vs 买收盘</div><div class="val up">+__SPREAD__ bp/日</div><div class="note">配对 t=__SPREAD_T__，胜率 __SPREAD_W__%</div></div>
  <div class="kpi"><div class="lab">长样本 2022–2026 价差</div><div class="val up">+__EXTS__ bp/日</div><div class="note">配对 t=__EXTT__，五年全部为正</div></div>
  <div class="kpi"><div class="lab">成本线（往返）</div><div class="val">15 bp</div><div class="note">最优时点毛收益仍低于成本线</div></div>
  <div class="kpi"><div class="lab">月度方向一致率</div><div class="val">__POSM__</div><div class="note">21 个月中价差为正的月数</div></div>
</div>

<h2>1. 全天买入时点收益曲线</h2>
<div class="card">
  <div id="c1" class="chart"></div>
  <div class="note-box"><b>读法</b>：曲线单调下滑——买入越早，吃到的日内正漂移越多。买开盘 +10.3bp/日，买收盘 −7.3bp/日。14:57 之后已进入收盘集合竞价（1 分钟 bar 成交趋零），不在可信区间内。</div>
</div>

<div class="card">
  <h3>收益分解：日内剩余段 + 隔夜段</h3>
  <div id="c2" class="chart"></div>
  <div class="note-box"><b>关键</b>：隔夜段对所有买入时点恒定（≈ −8.5bp/日），全部差异来自<span class="up">日内剩余段</span>。开盘 30 分钟内完成的漂移占全天日内漂移的 <b>53%</b>，此后 4.5 小时只贡献另外一半。</div>
</div>

<h2>2. 稳健性</h2>
<div class="card">
  <h3>月度价差（09:31 − 15:00，bp/日）</h3>
  <div id="c3" class="chart sm"></div>
  <div class="note-box"><b>18/21 个月为正</b>（均值 +17.5bp，区间 −31.2 ~ +64.5）。反向的 3 个月（2026-03 / 2026-05 / 2026-07）均为指数下行月——"买早"等于多暴露 4 小时市场 beta，下跌月会放大亏损。</div>
</div>

<div class="grid2">
  <div class="card">
    <h3>价差 × 成交额五分位</h3>
    <div id="c4" class="chart sm"></div>
  </div>
  <div class="card">
    <h3>价差 × 市值五分位</h3>
    <div id="c5" class="chart sm"></div>
  </div>
</div>
<div class="neg-box"><b>最重要的限定</b>：效应几乎全部来自<b>高换手股票</b>。成交额最大的五分位（Q5）价差 <b>+__LQ5__bp/日（t=__LQ5T__）</b>；成交额最小的五分位（Q1）反而是 <b>__LQ1__bp/日（t=__LQ1T__）</b>。低流动性冷门股不存在"买早更优"的规律。</div>

<h2>3. 长样本外延（2022–2026，日线口径，1134 个交易日）</h2>
<div class="card">
  <div id="c6" class="chart"></div>
  <div class="note-box"><b>五年价差全部为正</b>：2022 +3.2 / 2023 +5.2 / 2024 +8.1 / 2025 +20.5 / 2026 +8.9 bp/日（全期 +9.2bp，配对 t=2.17）。但买开盘的<b>绝对收益</b>全期仅 +4.9bp/日（t=0.99），不显著且低于成本线——"开盘买"是相对更优，不是绝对能赚。</div>
</div>

<h2>4. 可投资性裁决</h2>
<div class="card">
<table>
<thead><tr><th>口径</th><th>毛收益 bp/日</th><th>扣 15bp 净额</th><th>年化净额</th><th>裁决</th></tr></thead>
<tbody>
<tr><td>09:31 买 → 次日开盘卖</td><td class="up">+10.31</td><td class="down">−4.69</td><td class="down">−10.8%</td><td>❌ 不可独立投资</td></tr>
<tr><td>15:00 买 → 次日开盘卖</td><td class="down">−7.29</td><td class="down">−22.29</td><td class="down">−41.5%</td><td>❌</td></tr>
<tr><td>长样本 2022–2026 买开盘</td><td class="up">+4.89</td><td class="down">−10.11</td><td class="down">−22.3%</td><td>❌</td></tr>
<tr><td><b>09:31 vs 15:00 边际价差</b></td><td class="up"><b>+17.60</b></td><td class="up"><b>+17.60</b><br><span style="font-size:11px;color:#6b7480">交易次数不变</span></td><td>—</td><td>✅ <b>可执行，纯增量</b></td></tr>
</tbody>
</table>
<div class="note-box"><b>结论</b>：作为独立策略（每天全仓买开盘、次日开盘卖）<b>证伪</b>；作为执行时点规则<b>成立</b>——把既有调仓的买入从收盘挪到开盘/早盘，每次白捡 9–18bp，交易次数与换手率完全不变。</div>
</div>

<h2>5. 数据陷阱（已排除）</h2>
<div class="card">
<table>
<thead><tr><th>陷阱</th><th>现象</th><th>处理</th></tr></thead>
<tbody>
<tr><td>2026 年 13:00 污染 bar</td><td>仅在约 90 只 688 段次新股上出现，价格与前后 bar 脱节，使该时点日均收益虚高至 <b>+222bp（t=12.2）</b>，一度排进榜首</td><td>全表剔除 mod 780</td></tr>
<tr><td>2026-06-24 网格切换</td><td>上午末根 bar 由 11:30 变 11:29；14:57 后 bar 成交量归零</td><td>主结论限定 09:30–14:56</td></tr>
<tr><td>15:01–15:30 北交所零量占位 bar</td><td>280 只 920xxx 的伪造时点</td><td>过滤 mod ≤ 900 + 剔除北交所</td></tr>
<tr><td>分钟 15:00 close ≠ 官方收盘价</td><td>两个口径的隔夜段差 +1.57bp（= 收盘集合竞价上冲幅度）</td><td>分别报告，不混用</td></tr>
<tr><td>涨停封板不可成交</td><td>计入会高估买入收益（这类股次日普遍高开）</td><td>剔除买入时点触及涨停样本</td></tr>
</tbody>
</table>
</div>

<h2>6. 完整曲线数据（每 10 分钟）</h2>
<div class="card" style="overflow-x:auto"><div id="tb"></div></div>

<div class="sub" style="margin-top:28px">QuantLab · 研究：<code>research/intraday-entry-timing/A股每日最优买入时点研究.md</code> · 脚本：<code>scripts/intraday_entry_timing.py</code> / <code>scripts/intraday_entry_analyze.py</code></div>
</div>

<script>
const D = __DATA__;
const AX = {axisLine:{lineStyle:{color:'#c9ced6'}},axisLabel:{color:'#6b7480',fontSize:11},
  splitLine:{lineStyle:{color:'#eef0f3'}}};
const UP='#d64545', DOWN='#1a9e6f', AC='#2f6fdb', MUT='#8b93a1';

function lineChart(el, x, series, opt){
  const c=echarts.init(document.getElementById(el));
  c.setOption(Object.assign({
    grid:{left:56,right:24,top:34,bottom:52},
    tooltip:{trigger:'axis',valueFormatter:v=>(v===null?'':(+v).toFixed(2)+' bp')},
    legend:{top:2,textStyle:{color:'#6b7480',fontSize:11}},
    xAxis:Object.assign({}, AX, {type:'category',data:x,boundaryGap:false,
      axisLabel:{color:'#6b7480',fontSize:10,interval:Math.floor(x.length/12)}}),
    yAxis:Object.assign({}, AX, {type:'value',name:'bp/日',nameTextStyle:{color:'#6b7480'},
      axisLabel:{color:'#6b7480',fontSize:11,formatter:v=>v+'bp'}}),
    series:series
  }, opt||{}));
  window.addEventListener('resize',()=>c.resize());
  return c;
}

// 1 主曲线
const nOk = D.ok_y.length;
const auc = D.curve_y.map((v,i)=> i>=nOk-1 ? v : null);
lineChart('c1', D.curve_x, [
  {name:'连续竞价窗口（可信）',type:'line',smooth:true,showSymbol:false,data:D.ok_y,
   lineStyle:{width:2.6,color:AC},itemStyle:{color:AC},
   areaStyle:{color:new echarts.graphic.LinearGradient(0,0,0,1,[{offset:0,color:'rgba(47,111,219,.20)'},{offset:1,color:'rgba(47,111,219,0)'}])},
   markLine:{silent:true,symbol:'none',label:{formatter:'0',color:'#8b93a1',fontSize:10},
     lineStyle:{color:'#c9ced6',type:'dashed'},data:[{yAxis:0}]}},
  {name:'集合竞价窗口（不可信）',type:'line',smooth:false,showSymbol:true,symbolSize:5,
   data:auc,lineStyle:{width:2,color:MUT,type:'dotted'},itemStyle:{color:MUT}}
]);

// 2 分解
lineChart('c2', D.curve_x, [
  {name:'日内剩余段（随买入时点变化）',type:'line',smooth:true,showSymbol:false,data:D.curve_intra,
   lineStyle:{width:2.4,color:'#e08a2e'},itemStyle:{color:'#e08a2e'}},
  {name:'隔夜段（与买入时点无关）',type:'line',smooth:true,showSymbol:false,data:D.curve_gap,
   lineStyle:{width:2,color:'#7a86a8',type:'dashed'},itemStyle:{color:'#7a86a8'}},
  {name:'合计 = 买入持有收益',type:'line',smooth:true,showSymbol:false,data:D.curve_y,
   lineStyle:{width:2.6,color:AC},itemStyle:{color:AC}}
]);

// 3 月度价差
(function(){
 const c=echarts.init(document.getElementById('c3'));
 c.setOption({
  grid:{left:56,right:24,top:30,bottom:52},
  tooltip:{trigger:'axis',valueFormatter:v=>(+v).toFixed(1)+' bp'},
  xAxis:Object.assign({},AX,{type:'category',data:D.months,axisLabel:{color:'#6b7480',fontSize:10,rotate:45}}),
  yAxis:Object.assign({},AX,{type:'value',axisLabel:{color:'#6b7480',fontSize:11,formatter:v=>v+'bp'}}),
  series:[{type:'bar',data:D.mspread.map(v=>({value:v,itemStyle:{color:v>=0?UP:DOWN,borderRadius:2}})),
    markLine:{silent:true,symbol:'none',label:{show:false},lineStyle:{color:'#c9ced6',type:'dashed'},data:[{yAxis:0}]}}]
 });
 window.addEventListener('resize',()=>c.resize());
})();

// 4/5 分桶
function bar(el,names,vals,ts){
 const c=echarts.init(document.getElementById(el));
 c.setOption({
  grid:{left:52,right:16,top:26,bottom:36},
  tooltip:{trigger:'axis',formatter:p=>{const i=p[0].dataIndex;return names[i]+'<br/>价差 '+vals[i].toFixed(1)+' bp<br/>t = '+ts[i];}},
  xAxis:Object.assign({},AX,{type:'category',data:names}),
  yAxis:Object.assign({},AX,{type:'value',axisLabel:{color:'#6b7480',fontSize:11,formatter:v=>v+'bp'}}),
  series:[{type:'bar',barWidth:'52%',label:{show:true,position:'top',fontSize:11,color:'#4a5261',
    formatter:p=>p.value.toFixed(0)},
    data:vals.map(v=>({value:v,itemStyle:{color:v>=0?UP:DOWN,borderRadius:3}})),
    markLine:{silent:true,symbol:'none',label:{show:false},lineStyle:{color:'#c9ced6',type:'dashed'},data:[{yAxis:0}]}}]
 });
 window.addEventListener('resize',()=>c.resize());
}
bar('c4', D.liq_q.map(q=>'Q'+q), D.liq_sp, D.liq_t);
bar('c5', D.size_q.map(q=>'Q'+q), D.size_sp, D.size_t);

// 6 长样本
(function(){
 const c=echarts.init(document.getElementById('c6'));
 c.setOption({
  grid:{left:56,right:24,top:34,bottom:44},
  tooltip:{trigger:'axis',valueFormatter:v=>(+v).toFixed(2)+' bp'},
  legend:{top:2,textStyle:{color:'#6b7480',fontSize:11}},
  xAxis:Object.assign({},AX,{type:'category',data:D.ext_years.map(String)}),
  yAxis:Object.assign({},AX,{type:'value',axisLabel:{color:'#6b7480',fontSize:11,formatter:v=>v+'bp'}}),
  series:[
   {name:'买开盘 → 次日开盘',type:'bar',data:D.ext_open.map(v=>({value:v,itemStyle:{color:v>=0?UP:DOWN,borderRadius:3}}))},
   {name:'买收盘 → 次日开盘',type:'bar',data:D.ext_close.map(v=>({value:v,itemStyle:{color:v>=0?'#e59a9a':'#7fc9ae',borderRadius:3}}))},
   {name:'价差（配对）',type:'line',data:D.ext_spread,symbolSize:7,lineStyle:{width:2.4,color:'#e08a2e'},itemStyle:{color:'#e08a2e'}}
  ]
 });
 window.addEventListener('resize',()=>c.resize());
})();

// 表格
(function(){
 const hdr=['时点','毛收益 bp','t','日胜率','日内剩余段','隔夜段','扣15bp净额'];
 let h='<table><thead><tr>'+hdr.map(x=>'<th>'+x+'</th>').join('')+'</tr></thead><tbody>';
 D.curve_table.forEach((r,i)=>{
   if(i%10!==0 && !['09:25','09:31','14:56','15:00'].includes(r[0])) return;
   const cls=r[1]>=0?'up':'down';
   h+='<tr><td>'+r[0]+'</td><td class="'+cls+'">'+(r[1]>0?'+':'')+r[1].toFixed(2)+'</td><td>'+r[2].toFixed(2)+
      '</td><td>'+(r[3]*100).toFixed(1)+'%</td><td>'+r[4].toFixed(2)+'</td><td>'+r[5].toFixed(2)+
      '</td><td class="'+(r[6]>=0?'up':'down')+'">'+r[6].toFixed(2)+'</td></tr>';
 });
 h+='</tbody></table>';
 document.getElementById('tb').innerHTML=h;
})();
</script>
</body>
</html>
"""

html = (tmpl
        .replace("__DATA__", json.dumps(payload, ensure_ascii=False, default=str))
        .replace("__NDAYS__", str(kpi["n_days"]))
        .replace("__NOBS__", f"{kpi['n_obs']/1e8:.2f}")
        .replace("__BEST__", f"{kpi['best_bp']:+.2f}")
        .replace("__SPREAD__", f"{kpi['spread']['spread_bp']:+.2f}")
        .replace("__SPREAD_T__", str(kpi["spread"]["t"]))
        .replace("__SPREAD_W__", f"{kpi['spread']['win']*100:.1f}")
        .replace("__EXTS__", f"{kpi['ext_spread']:+.2f}")
        .replace("__EXTT__", str(kpi["ext_spread_t"]))
        .replace("__POSM__", kpi["pos_months"])
        .replace("__LQ5__", f"{kpi['liq_q5']:+.1f}")
        .replace("__LQ5T__", str(kpi["liq_q5_t"]))
        .replace("__LQ1__", f"{kpi['liq_q1']:+.1f}")
        .replace("__LQ1T__", str(kpi["liq_q1_t"])))

OUT.write_text(html, encoding="utf-8")
print(f"[wrote] {OUT}  ({len(html)/1024:.0f} KB)")
