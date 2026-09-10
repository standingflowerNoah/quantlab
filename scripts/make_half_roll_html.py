r"""生成「半仓滚动法」HTML 研究报告（浅色主题 + ECharts）"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
REP = ROOT / "reports"
D = json.loads((REP / "overnight/half_roll_summary_v2.json").read_text(encoding="utf-8"))
NAV = pd.read_csv(REP / "overnight/half_roll_nav_v2.csv")
VAR = pd.read_csv(REP / "overnight/half_roll_variants_v2.csv")
COST = pd.read_csv(REP / "overnight/half_roll_cost_v2.csv")
YEAR = pd.read_csv(REP / "overnight/half_roll_by_year_v2.csv")
LIQ = pd.read_csv(REP / "overnight/half_roll_by_liq_v2.csv")
SIZE = pd.read_csv(REP / "overnight/half_roll_by_size_v2.csv")
ENT = pd.read_csv(REP / "overnight/half_roll_entry_timing.csv")

V = {(r["variant"], r["kind"]): r for r in D["variants"]}
def g(v, k, f="cagr_pct"):
    return round(float(V[(v, k)][f]), 1)

payload = {
    "nav_dates": NAV["dates"].tolist(),
    "nav": {c: NAV[c].round(2).tolist() for c in NAV.columns if c != "dates"},
    "cost": COST[["rt_bp", "cagr_pct", "maxdd_pct"]].to_dict("records"),
    "be": D["breakeven_rt_bp"],
    "year": YEAR.to_dict("records"),
    "liq": LIQ.to_dict("records"),
    "size": SIZE.to_dict("records"),
    "ent": ENT.to_dict("records"),
    "monthly": D["monthly"],
    "variants": VAR.to_dict("records"),
    "mean_i": D["mean_i_bp"], "mean_g": D["mean_g_bp"],
}

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>半仓滚动法收益测算 · A股</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
*{box-sizing:border-box}
body{margin:0;background:#f7f8fa;color:#1f2430;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
  font-size:14px;line-height:1.65;-webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto;padding:32px 24px 72px}
h1{font-size:26px;font-weight:650;margin:0 0 6px;letter-spacing:-.3px}
h2{font-size:17px;font-weight:620;margin:38px 0 4px;padding-left:11px;border-left:3px solid #2f6fdb}
h3{font-size:14px;font-weight:600;margin:22px 0 8px;color:#39414d}
.sub{color:#6b7480;font-size:13px;margin:0 0 4px}
.meta{color:#8b93a1;font-size:12px;margin-bottom:22px}
.card{background:#fff;border:1px solid #e8ebf0;border-radius:10px;padding:18px 20px;margin:14px 0}
.kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(178px,1fr));gap:12px;margin:18px 0 6px}
.kpi>div{background:#fff;border:1px solid #e8ebf0;border-radius:10px;padding:14px 16px}
.kpi .lb{font-size:11.5px;color:#8b93a1;letter-spacing:.2px}
.kpi .vv{font-size:23px;font-weight:650;margin:3px 0 1px;letter-spacing:-.5px}
.kpi .nt{font-size:11px;color:#8b93a1}
.up{color:#c0392b}.dn{color:#1e8e5a}.mid{color:#6b7480}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin:6px 0}
th,td{padding:7px 9px;text-align:right;border-bottom:1px solid #eef1f5;white-space:nowrap}
th{background:#fbfcfd;color:#5b6673;font-weight:600;font-size:11.5px;text-align:right}
th:first-child,td:first-child{text-align:left}
tbody tr:hover{background:#fafbfd}
.chart{width:100%;height:340px}
.chart.sm{height:290px}
.note{background:#f3f7fe;border:1px solid #dbe6fa;border-radius:9px;padding:13px 16px;font-size:12.8px;color:#33415c;margin:14px 0}
.warn{background:#fff8ef;border:1px solid #f5e0c3;color:#6b4a22}
.ok{background:#f1faf5;border:1px solid #cfe9dc;color:#1c5e42}
.note b{font-weight:650}
ul{margin:8px 0 8px 20px;padding:0}li{margin:5px 0}
.flex{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:820px){.flex{grid-template-columns:1fr}}
.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:20px;background:#eef1f5;color:#5b6673;margin-left:6px}
.foot{color:#9aa2ae;font-size:11.5px;margin-top:34px;border-top:1px solid #e8ebf0;padding-top:14px}
</style></head><body><div class="wrap">

<h1>半仓滚动法收益测算</h1>
<p class="sub">资金分两半，每半「早盘买入 → 次日收盘卖出」，两半交错滚动 —— 收益、暴露与成本可行性</p>
<p class="meta">样本：A股全市场等权 · 2022-01-05 → 2026-09-08（1134 个交易日 / 558.9 万条观测）· 除权除息已调整 · 纯 PIT 股票池</p>

<div class="kpi">
  <div><div class="lb">滚动半仓 毛收益（年化复合）</div><div class="vv up">+__RC__%</div><div class="nt">4.65 年累计 +__RT__%</div></div>
  <div><div class="lb">扣 15bp 往返成本后</div><div class="vv dn">__RN__%</div><div class="nt">夏普 __RS__</div></div>
  <div><div class="lb">满仓持有（基准）</div><div class="vv mid">+__BH__%</div><div class="nt">同期等权市场</div></div>
  <div><div class="lb">盈亏平衡成本线</div><div class="vv">__BE__ bp</div><div class="nt">每次满仓往返</div></div>
</div>

<div class="note warn"><b>一句话结论：结构是对的，但被成本吃掉。</b>
滚动半仓把「日内漂移 +盈利、隔夜段 −亏损」的收益结构利用到极致 —— 毛收益 __RC__%/年，显著高于满仓持有的 __BH__%/年；
代价是每天 0.5 次满仓往返，成本约 __COSTY__%/年。盈亏平衡点在 <b>__BE__bp/次往返</b>，
而 A 股现实成本（印花税 5bp + 佣金 + 冲击）恰好落在这条线上 —— 15bp 假设下净收益约 __RN__%/年。</div>

<h2>一、这个策略在做什么</h2>
<div class="card">
<p style="margin:0 0 10px">A 股（T+1）的收益有个稳定结构：<b>日内段（开盘→收盘）长期为正，隔夜段（收盘→次日开盘）长期为负</b>。
本样本 1134 个交易日的等权均值：</p>
<div class="kpi" style="margin:0 0 4px">
  <div><div class="lb">日内段 开盘→收盘</div><div class="vv up">+__MI__ bp</div><div class="nt">每交易日 · t=2.19</div></div>
  <div><div class="lb">隔夜段 收盘→次日开盘</div><div class="vv dn">__MG__ bp</div><div class="nt">每交易日 · t=−2.65</div></div>
  <div><div class="lb">买开盘持有到次日开盘</div><div class="vv mid">+4.36 bp</div><div class="nt">= 100% 日内 + 100% 隔夜</div></div>
  <div><div class="lb">滚动半仓法</div><div class="vv up">+6.75 bp</div><div class="nt">= 100% 日内 + <b>50%</b> 隔夜</div></div>
</div>
<p style="margin:12px 0 0">隔夜段每承担一次要亏 __MG__bp，那么<b>只要能让部分资金不过夜，就能白拿差额</b>。
「两半交错」正好做到这点：任一时点上，两半当天都在场（日内暴露 100%），但只有前一日进场的那一半持仓过夜（隔夜暴露 50%）。
所以它比满仓持有更省 β 暴露，却在<b>不减日内收益</b>的前提下砍掉一半隔夜亏损。</p>
</div>

<h2>二、收益结果</h2>
<div class="chart" id="c_nav"></div>
<p class="sub" style="margin-top:8px">净值起点 = 100（2022-01-05）。实线为毛收益，虚线为扣 15bp 往返成本后的净收益。</p>

<div class="card">
<h3 style="margin-top:0">各方案对比（2022-01-05 → 2026-09-08）</h3>
<table><thead><tr><th>方案</th><th>日内暴露</th><th>隔夜暴露</th><th>换手（满仓往返/年）</th>
<th>毛年化</th><th>毛累计</th><th>毛夏普</th><th>毛最大回撤</th><th>净年化(15bp)</th></tr></thead>
<tbody>
<tr><td><b>滚动半仓（本方案）</b></td><td>100%</td><td>50%</td><td>122</td>
<td class="up"><b>+__RC__%</b></td><td>+__RT__%</td><td>__RG__</td><td>−30.2%</td><td class="dn">__RN__%</td></tr>
<tr><td>日内满仓（开→收，每日平）</td><td>100%</td><td>0%</td><td>244</td>
<td class="up">+22.3%</td><td>+154.6%</td><td>1.01</td><td>−27.9%</td><td class="dn">−15.2%</td></tr>
<tr><td>全天满仓（开→次日开，始终在场）</td><td>100%</td><td>100%</td><td>244</td>
<td class="up">+7.6%</td><td>+40.3%</td><td>0.41</td><td>−34.6%</td><td class="dn">−25.4%</td></tr>
<tr><td>纯隔夜（收→次日开）</td><td>0%</td><td>100%</td><td>244</td>
<td class="dn">−12.0%</td><td>−44.9%</td><td>−1.23</td><td>−45.6%</td><td class="dn">−39.0%</td></tr>
<tr><td>买入持有（等权市场，不交易）</td><td>100%</td><td>100%</td><td>0</td>
<td class="mid">+4.2%</td><td>+20.9%</td><td>0.29</td><td>−36.2%</td><td class="mid">+4.2%</td></tr>
</tbody></table>
<p class="sub" style="margin:8px 0 0">同期指数：科创50 +3.3%/年、上证 +1.8%、中证500 +1.2%、中证1000 −0.9%、沪深300 −1.6%。</p>
</div>

<div class="note ok"><b>结构性结论成立</b>：滚动半仓的毛收益（__RC__%/年）比「全天满仓」（7.6%/年）高出 7.2 个百分点，
而平均暴露还更低 —— 多出来的收益完全来自「<b>用一半仓位换掉了本该承担的隔夜亏损</b>」。
把隔夜段单独拿出来做是 −12.0%/年，这正是它躲掉的东西的一部分。</div>

<h2>三、成本是唯一致命变量</h2>
<div class="flex">
  <div class="chart sm" id="c_cost"></div>
  <div class="chart sm" id="c_costdd"></div>
</div>
<div class="note warn"><b>盈亏平衡 = __BE__bp / 次满仓往返。</b>
每天买入 50% + 卖出 50% 仓位 ＝ 每天 0.5 次满仓往返，成本 = 0.5 × 往返费率。
按 15bp 计，年成本约 18.3%，毛收益 __RC__% 全部被吃掉。
只有在往返成本 ≤ __BE__bp 时才为正 —— 而小账户（万1佣金 + 5bp 印花税 + 3bp 冲击，约 11～13bp）恰好卡在这条线上。</div>

<div class="card">
<h3 style="margin-top:0">成本敏感性</h3>
<table><thead><tr><th>每次满仓往返成本</th><th>0 bp</th><th>5 bp</th><th>10 bp</th><th>15 bp</th><th>20 bp</th><th>25 bp</th><th>30 bp</th></tr></thead>
<tbody>
<tr><td>净年化收益</td><td>+14.8%</td><td>+8.0%</td><td>+1.6%</td><td class="dn">−4.4%</td><td class="dn">−10.0%</td><td class="dn">−15.4%</td><td class="dn">−20.4%</td></tr>
<tr><td>净夏普</td><td>0.72</td><td>0.45</td><td>0.19</td><td>−0.08</td><td>−0.35</td><td>−0.61</td><td>−0.88</td></tr>
<tr><td>最大回撤</td><td>−30.2%</td><td>−33.3%</td><td>−40.0%</td><td>−47.0%</td><td>−53.3%</td><td>−59.6%</td><td>−68.4%</td></tr>
</tbody></table>
</div>

<h2>四、收益从哪一年来</h2>
<div class="chart" id="c_year"></div>
<div class="card" style="overflow-x:auto">
<table><thead><tr><th>年度</th><th>交易日</th><th>日内均值</th><th>隔夜均值</th><th>滚动半仓毛累计</th><th>净累计(15bp)</th><th>同期等权持有</th></tr></thead>
<tbody>__YEARROWS__</tbody></table>
<p class="sub" style="margin:8px 0 0"><b>高度依赖 2025。</b>剔除 2025 年后，2022-2024+2026 的毛收益只有 8～10%/年 —— 已经低于成本线。
2025 年日内均值 20.7bp 是长样本（9.3bp）的两倍多，是唯一一年净收益显著为正的年份。</p>
</div>

<h2>五、分档：效应主要在哪类股票</h2>
<div class="flex">
  <div class="chart sm" id="c_liq"></div>
  <div class="chart sm" id="c_size"></div>
</div>
<div class="note warn"><b>这里修正了一个前一轮的结论。</b>上一轮研究用「<u>当日</u>成交额」给股票分档，得出「效应集中在高换手股、低流动性股反而反向」。
但当日成交额是<b>当天涨跌的结果</b>（涨得多的股票成交必然放大），用它分档等于把同期价格波动当成了预测变量 —— 典型的未来信息污染。
改用<b>过去 20 日均额</b>（PIT）重新分档后：日内漂移在<b>各档都成立</b>，低成交额档（+5.4bp）与高成交额档（+13.4bp）差距远没有之前那么大，
而<b>隔夜亏损全部集中在高成交额档</b>（Q5 为 −16.6bp，Q1 反而是 +6.0bp）。</div>
<div class="card" style="overflow-x:auto">
<table><thead><tr><th>分档（过去20日均额）</th><th>日内段</th><th>隔夜段</th><th>毛年化</th><th>毛夏普</th><th>净年化(15bp)</th><th>净最大回撤</th></tr></thead>
<tbody>__LIQROWS__</tbody></table>
<table style="margin-top:16px"><thead><tr><th>分档（流通市值）</th><th>日内段</th><th>隔夜段</th><th>毛年化</th><th>毛夏普</th><th>净年化(15bp)</th><th>净最大回撤</th></tr></thead>
<tbody>__SIZEROWS__</tbody></table>
</div>

<h2>六、「早盘」到底该多早</h2>
<div class="chart" id="c_ent"></div>
<p class="sub" style="margin-top:6px">半仓滚动结构不变，只把<b>进场那一半</b>的下单时点从开盘往后挪（出场那一半始终持全天）。2025+ 分钟层样本，含 15bp 成本。</p>
<div class="card" style="overflow-x:auto">
<table><thead><tr><th>进场时点</th><th>剩余日内收益</th><th>毛年化</th><th>净年化(15bp)</th><th>净年化(10bp)</th><th>净年化(20bp)</th></tr></thead>
<tbody>__ENTROWS__</tbody></table>
<p class="sub" style="margin:8px 0 0"><b>开盘（09:25 集合竞价 / 09:31）最优，全天单调衰减</b>，10:30 之后进场基本无法覆盖成本。
14:56 与收盘（15:00）是全天最差点位。<b>不能拖到午后再买</b>，这条约束比选股更重要。</p>
</div>

<h2>七、月度收益（净，15bp）</h2>
<div class="chart" id="c_month"></div>
<p class="sub" style="margin-top:6px">57 个月中 29 个月为正（50.9%）。回撤最深的 2024-01（−19.0%）与 2022-04（−12.7%）对应小微盘流动性危机。</p>

<h2>八、口径与数据说明</h2>
<div class="card">
<ul>
<li><b>股票池（纯 PIT）</b>：非北交所（4/8/920 段）、当日有成交、开盘非一字涨停（买不到）、存在下一交易日。
558.9 万条观测，占原始面板 95.4%。</li>
<li><b>不包含任何依赖次日的信息</b>。初版曾用「次日可卖出」过滤，会把次日跌停封板的样本剔除，
系统性抬高日内均值约 3～5bp —— 而本策略的日均收益只有 6.75bp，这类偏差足以改变结论。已全部移除。</li>
<li><b>收益口径</b>：直接采用日线表自带的 <code>total_ret / overnight_ret / intraday_ret</code>（数据源已按除权除息调整前收盘），
避免了手工用 close/open 相除丢失除权调整的问题。</li>
<li><b>成本模型</b>：每次满仓往返 rt bp，日成本 = 0.5×rt（每日买 50% + 卖 50%）。未额外建模开盘冲击，
开盘 30 分钟是全天流动性最好的时段，等权小单成交假设基本成立，但容量受限。</li>
<li><b>组合假设</b>：全市场等权、每日等额再平衡、两半各自独立复利。真实操作中 4500 只股票无法等权持有，
实际只能选子集 —— 但本研究的目的是测算<b>方法本身</b>的收益结构，不是给出可交易组合。</li>
</ul>
</div>

<div class="note"><b>下一步（需要确认）：</b>把滚动半仓作为<b>执行结构</b>套到生产组合（size + amihud_20 top100）上，
用成分股实际流动性重估成本 —— 生产组合持仓集中在微盘，开盘冲击成本会显著高于全市场等权假设，
这是决定它到底能不能用的唯一变量。</div>

<p class="foot">生成于 2026-09-11 · QuantLab · 脚本 scripts/half_roll_sim.py + half_roll_entry_timing.py ·
数据 data/lake/factor/overnight/daily_segments + mirror/kline_1min</p>
</div>
<script>
const D = __PAYLOAD__;
const AX={axisLine:{lineStyle:{color:'#dfe3e9'}},axisTick:{show:false},
  axisLabel:{color:'#6b7480',fontSize:10.5},splitLine:{lineStyle:{color:'#f0f2f6'}}};
const TT={trigger:'axis',backgroundColor:'rgba(255,255,255,.97)',borderColor:'#e2e6ec',
  textStyle:{color:'#1f2430',fontSize:11.5},axisPointer:{type:'line',lineStyle:{color:'#c9ced6'}}};
const LG={bottom:0,itemWidth:14,itemHeight:8,textStyle:{color:'#6b7480',fontSize:11}};
const UP='#c0392b', DN='#1e8e5a';

// 1 NAV
const navSeries=[
 {k:'roll_half_g',n:'滚动半仓（毛）',c:'#2f6fdb',w:2.6},
 {k:'roll_half_n',n:'滚动半仓（净 15bp）',c:'#2f6fdb',w:1.6,dash:true},
 {k:'always_full_g',n:'全天满仓（毛）',c:'#8b93a1',w:1.8},
 {k:'intraday_only_g',n:'日内满仓（毛）',c:'#d98324',w:1.6},
 {k:'overnight_only_g',n:'纯隔夜（毛）',c:'#57a773',w:1.6},
 {k:'buyhold_cc_g',n:'买入持有（毛）',c:'#b06bd6',w:1.6}
];
const logY = D.nav_dates.length>0;
echarts.init(document.getElementById('c_nav')).setOption({
 tooltip:Object.assign({},TT,{valueFormatter:v=>v==null?'-':(+v).toFixed(1)}),
 legend:Object.assign({},LG,{top:0}),
 grid:{left:52,right:18,top:34,bottom:34},
 xAxis:Object.assign({},AX,{type:'category',data:D.nav_dates,boundaryGap:false,
   axisLabel:{color:'#6b7480',fontSize:10,interval:Math.floor(D.nav_dates.length/9)}}),
 yAxis:Object.assign({},AX,{type:'log',name:'净值',nameTextStyle:{color:'#8b93a1',fontSize:10.5},
   axisLabel:{color:'#6b7480',fontSize:10.5,formatter:v=>v},
   splitLine:{lineStyle:{color:'#f0f2f6'}}}),
 series:navSeries.map(s=>({name:s.n,type:'line',showSymbol:false,smooth:false,data:D.nav[s.k],
   lineStyle:{width:s.w,color:s.c,type:s.dash?'dashed':'solid'},itemStyle:{color:s.c}}))
});

// 3 成本
echarts.init(document.getElementById('c_cost')).setOption({
 tooltip:Object.assign({},TT,{valueFormatter:v=>(+v).toFixed(1)+'%'}),
 grid:{left:52,right:18,top:30,bottom:30},
 xAxis:Object.assign({},AX,{type:'category',data:D.cost.map(r=>r.rt_bp)}),
 yAxis:Object.assign({},AX,{type:'value',name:'净年化 %',nameTextStyle:{color:'#8b93a1',fontSize:10.5}}),
 series:[{name:'净年化',type:'line',smooth:false,showSymbol:true,symbolSize:6,
   data:D.cost.map(r=>r.cagr_pct),itemStyle:{color:'#2f6fdb'},lineStyle:{width:2.6,color:'#2f6fdb'},
   areaStyle:{color:new echarts.graphic.LinearGradient(0,0,0,1,[
     {offset:0,color:'rgba(47,111,219,.18)'},{offset:1,color:'rgba(47,111,219,0)'}])},
   markLine:{silent:true,symbol:'none',label:{formatter:'盈亏平衡 '+D.be+'bp',color:'#c0392b',fontSize:10.5,position:'insideEndTop'},
     lineStyle:{color:'#c0392b',type:'dashed',width:1.4},data:[{xAxis:D.cost.findIndex(r=>r.rt_bp>=D.be)-0.5}]},
   markPoint:{silent:true,symbol:'circle',symbolSize:1,data:[]}}],
 title:{text:'成本敏感性',left:0,top:0,textStyle:{fontSize:12.5,color:'#39414d',fontWeight:600}}
});
echarts.init(document.getElementById('c_costdd')).setOption({
 tooltip:Object.assign({},TT,{valueFormatter:v=>(+v).toFixed(1)+'%'}),
 grid:{left:52,right:18,top:30,bottom:30},
 xAxis:Object.assign({},AX,{type:'category',data:D.cost.map(r=>r.rt_bp)}),
 yAxis:Object.assign({},AX,{type:'value',name:'最大回撤 %',nameTextStyle:{color:'#8b93a1',fontSize:10.5}}),
 series:[{type:'bar',data:D.cost.map(r=>r.maxdd_pct),barWidth:'52%',
   itemStyle:{color:'#e0a86f',borderRadius:[3,3,0,0]}}],
 title:{text:'成本 → 回撤',left:0,top:0,textStyle:{fontSize:12.5,color:'#39414d',fontWeight:600}}
});

// 4 分年
echarts.init(document.getElementById('c_year')).setOption({
 tooltip:Object.assign({},TT,{valueFormatter:v=>(+v).toFixed(1)+'%'}),
 legend:Object.assign({},LG,{top:0}),grid:{left:52,right:18,top:34,bottom:34},
 xAxis:Object.assign({},AX,{type:'category',data:D.year.map(r=>r.year)}),
 yAxis:Object.assign({},AX,{type:'value',name:'累计收益 %',nameTextStyle:{color:'#8b93a1',fontSize:10.5}}),
 series:[
  {name:'滚动半仓（毛）',type:'bar',data:D.year.map(r=>r.roll_total_pct),barWidth:'26%',
   itemStyle:{color:'#2f6fdb',borderRadius:[3,3,0,0]}},
  {name:'滚动半仓（净 15bp）',type:'bar',data:D.year.map(r=>r.roll_net_pct),barWidth:'26%',
   itemStyle:{color:'#a8c2ee',borderRadius:[3,3,0,0]}},
  {name:'同期等权持有',type:'bar',data:D.year.map(r=>r.buyhold_pct),barWidth:'26%',
   itemStyle:{color:'#d7dbe2',borderRadius:[3,3,0,0]}}]
});

// 5 分档
const bktChart=(id,data,title)=>{
 echarts.init(document.getElementById(id)).setOption({
  tooltip:Object.assign({},TT),legend:Object.assign({},LG,{top:0}),
  grid:{left:62,right:16,top:32,bottom:44},
  xAxis:Object.assign({},AX,{type:'category',data:data.map(r=>'Q'+r.bucket),
    axisLabel:{color:'#6b7480',fontSize:10.5,interval:0}}),
  yAxis:Object.assign({},AX,{type:'value',name:'bp/日',nameTextStyle:{color:'#8b93a1',fontSize:10.5}}),
  series:[
   {name:'日内段',type:'bar',data:data.map(r=>r.i_bp),barWidth:'30%',
    itemStyle:{color:'#2f6fdb',borderRadius:[3,3,0,0]},label:{show:true,position:'top',fontSize:9.5,color:'#5b6673',formatter:p=>(+p.value).toFixed(1)}},
   {name:'隔夜段',type:'bar',data:data.map(r=>r.g_bp),barWidth:'30%',
    itemStyle:{color:'#d98324',borderRadius:[3,3,0,0]},label:{show:true,position:'bottom',fontSize:9.5,color:'#5b6673',formatter:p=>(+p.value).toFixed(1)}}],
  title:{text:title,subtext:'Q1 最小 → Q5 最大',left:0,top:0,textStyle:{fontSize:12,color:'#39414d',fontWeight:600},
    subtextStyle:{fontSize:10,color:'#9aa2ae'}}
 });
};
bktChart('c_liq',D.liq,'按过去20日均额分档');
bktChart('c_size',D.size,'按流通市值分档');

// 6 进场时点
echarts.init(document.getElementById('c_ent')).setOption({
 tooltip:Object.assign({},TT,{valueFormatter:v=>(+v).toFixed(1)+'%'}),
 legend:Object.assign({},LG,{top:0}),grid:{left:52,right:18,top:34,bottom:44},
 xAxis:Object.assign({},AX,{type:'category',data:D.ent.map(r=>r.hm),
   axisLabel:{color:'#6b7480',fontSize:10,interval:0,rotate:38}}),
 yAxis:Object.assign({},AX,{type:'value',name:'年化 %',nameTextStyle:{color:'#8b93a1',fontSize:10.5}}),
 series:[
  {name:'毛年化',type:'line',showSymbol:true,symbolSize:5,data:D.ent.map(r=>r.gross_cagr_pct),
   lineStyle:{width:2.4,color:'#2f6fdb'},itemStyle:{color:'#2f6fdb'}},
  {name:'净年化（15bp）',type:'line',showSymbol:true,symbolSize:5,data:D.ent.map(r=>r.net_cagr_pct),
   lineStyle:{width:2.2,color:'#d98324'},itemStyle:{color:'#d98324'},
   markLine:{silent:true,symbol:'none',label:{formatter:'0',color:'#8b93a1',fontSize:10},
     lineStyle:{color:'#c9ced6',type:'dashed'},data:[{yAxis:0}]}}]
});

// 7 月度
echarts.init(document.getElementById('c_month')).setOption({
 tooltip:Object.assign({},TT,{valueFormatter:v=>(+v).toFixed(2)+'%'}),
 grid:{left:52,right:18,top:20,bottom:44},
 xAxis:Object.assign({},AX,{type:'category',data:D.monthly.map(r=>r.month),
   axisLabel:{color:'#6b7480',fontSize:9.5,interval:2,rotate:40}}),
 yAxis:Object.assign({},AX,{type:'value',name:'%',nameTextStyle:{color:'#8b93a1',fontSize:10.5}}),
 series:[{type:'bar',barWidth:'62%',data:D.monthly.map(r=>({value:r.ret_pct,
   itemStyle:{color:r.ret_pct>=0?UP:DN,borderRadius:r.ret_pct>=0?[2,2,0,0]:[0,0,2,2]}}))}]
});
window.addEventListener('resize',()=>document.querySelectorAll('.chart,.chart.sm').forEach(e=>{
  const i=echarts.getInstanceByDom(e); if(i) i.resize();}));
</script></body></html>
"""


def fmt_year(r):
    cls = "up" if r["roll_total_pct"] > 0 else "dn"
    nc = "up" if r["roll_net_pct"] > 0 else "dn"
    return (f"<tr><td><b>{r['year']}</b></td><td>{r['n_days']}</td>"
            f"<td class=\"{'up' if r['i_bp']>0 else 'dn'}\">{r['i_bp']:+.2f}</td>"
            f"<td class=\"{'up' if r['g_bp']>0 else 'dn'}\">{r['g_bp']:+.2f}</td>"
            f"<td class=\"{cls}\">{r['roll_total_pct']:+.1f}%</td>"
            f"<td class=\"{nc}\">{r['roll_net_pct']:+.1f}%</td>"
            f"<td>{r['buyhold_pct']:+.1f}%</td></tr>")


def fmt_bkt(rows):
    return "\n".join(
        f"<tr><td>Q{r['bucket']}</td>"
        f"<td class=\"{'up' if r['i_bp']>0 else 'dn'}\">{r['i_bp']:+.2f}</td>"
        f"<td class=\"{'up' if r['g_bp']>0 else 'dn'}\">{r['g_bp']:+.2f}</td>"
        f"<td class=\"up\">+{r['gross_cagr_pct']:.1f}%</td><td>{r['gross_sharpe']}</td>"
        f"<td class=\"{'up' if r['net_cagr_pct']>0 else 'dn'}\">{r['net_cagr_pct']:+.1f}%</td>"
        f"<td>{r['net_maxdd_pct']:.1f}%</td></tr>" for _, r in rows.iterrows())


def fmt_ent(rows):
    out = []
    for _, r in rows.iterrows():
        hl = " style=\"background:#f3f7fe\"" if r["hm"] in ("09:25开盘", "09:31") else ""
        out.append(
            f"<tr{hl}><td>{r['hm']}</td>"
            f"<td class=\"{'up' if r['ri_bp']>0 else 'dn'}\">{r['ri_bp']:+.2f}bp</td>"
            f"<td class=\"up\">+{r['gross_cagr_pct']:.1f}%</td>"
            f"<td class=\"{'up' if r['net_cagr_pct']>0 else 'dn'}\">{r['net_cagr_pct']:+.1f}%</td>"
            f"<td>{r['rt10']:+.1f}%</td><td>{r['rt20']:+.1f}%</td></tr>"
            if "rt10" in r else "")
    return "\n".join(out)


RC = g("roll_half", "gross")
RN = g("roll_half", "net")
RS = V[("roll_half", "net")]["sharpe"]
RG = V[("roll_half", "gross")]["sharpe"]
RT = g("roll_half", "gross", "total_pct")
BH = g("buyhold_cc", "gross")

# 进场时点 + 成本矩阵
ENT2 = pd.read_csv(REP / "overnight/half_roll_entry_timing.csv")
day = pd.read_parquet(ROOT / "data/lake/factor/overnight/half_roll/daily_v2.parquet")
cell = pd.read_parquet(ROOT / "data/lake/factor/overnight/entry_timing/cell_main.parquet")
cell = cell[~cell.buy_lim]
cell["d"] = pd.to_datetime(cell["d"])
mm = cell.groupby(["d", "mod"]).agg(n=("n", "sum"), s=("s_intra", "sum")).reset_index()
mm["ri"] = mm["s"] / mm["n"]
piv = mm.pivot(index="d", columns="mod", values="ri")
d2 = day.copy()
d2["d"] = pd.to_datetime(d2.index)
d2 = d2.set_index("d")
i = d2["i0"].reindex(piv.index); gl = d2["g"].shift(1).reindex(piv.index)
exiting = 0.5 * ((1 + gl.fillna(0)) * (1 + i) - 1)
extra = {}
for mod in ENT2["mod"]:
    r = (exiting + 0.5 * piv[mod]).dropna()
    yrs = len(r) / 244.0
    extra[int(mod)] = {f"rt{c}": round((((1 + r - c/2/1e4).cumprod().iloc[-1]) ** (1/yrs) - 1) * 100, 1)
                       for c in [10, 20]}
ENT2["rt10"] = ENT2["mod"].map(lambda m: extra.get(m, {}).get("rt10"))
ENT2["rt20"] = ENT2["mod"].map(lambda m: extra.get(m, {}).get("rt20"))

html = (HTML
        .replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
        .replace("__RC__", f"{RC:.1f}").replace("__RN__", f"{RN:+.1f}")
        .replace("__RS__", str(RS)).replace("__RG__", str(RG))
        .replace("__RT__", f"{RT:.1f}").replace("__BH__", f"{BH:.1f}")
        .replace("__BE__", f"{D['breakeven_rt_bp']:.1f}")
        .replace("__MI__", f"{D['mean_i_bp']:+.2f}").replace("__MG__", f"{D['mean_g_bp']:+.2f}")
        .replace("__COSTY__", "18.3")
        .replace("__YEARROWS__", "\n".join(fmt_year(r) for r in D["by_year"]))
        .replace("__LIQROWS__", fmt_bkt(LIQ)).replace("__SIZEROWS__", fmt_bkt(SIZE))
        .replace("__ENTROWS__", "\n".join(
            f"<tr{' style=\"background:#f3f7fe\"' if r.hm in ('09:25开盘','09:31') else ''}>"
            f"<td>{r.hm}</td><td class=\"{'up' if r.ri_bp>0 else 'dn'}\">{r.ri_bp:+.2f}bp</td>"
            f"<td class=\"up\">+{r.gross_cagr_pct:.1f}%</td>"
            f"<td class=\"{'up' if r.net_cagr_pct>0 else 'dn'}\">{r.net_cagr_pct:+.1f}%</td>"
            f"<td class=\"{'up' if r.rt10>0 else 'dn'}\">{r.rt10:+.1f}%</td>"
            f"<td class=\"{'up' if r.rt20>0 else 'dn'}\">{r.rt20:+.1f}%</td></tr>"
            for _, r in ENT2.iterrows()))
        )

out = REP / "half_roll_report.html"
out.write_text(html, encoding="utf-8")
print(f"[ok] {out}  {len(html)/1024:.1f} KB")
