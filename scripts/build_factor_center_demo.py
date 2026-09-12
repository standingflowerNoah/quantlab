"""构建日常因子看板页面：单因子全维度评估（v2，去对标化 + 结构重组）。
输入 reports/factor_center_demo_data.json + v3 评估报告静态结论 →
输出 docs/factor_center_amihud_20.html（单文件，无外部依赖）。

v2 变更（用户反馈）：
  - 移除全部对标营销信息（PandaAI 同款 / QL+ 独有 / 对标总览表）
  - 五分位与十分位合并进统一"分组分析"区
  - "IC 衰减图"更名"多周期 IC 谱"（横轴=持有期长度，非时间）
  - 修复双轴图右轴刻度错用主轴 Y() 导致不显示的 bug
"""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
D = json.loads((ROOT / "reports" / "factor_center_demo_data.json")
              .read_text(encoding="utf-8"))

TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>因子看板 · amihud_20</title>
<style>
:root{
  --bg:#1a1a1a; --panel:#232323; --panel2:#2a2a2a; --line:#3a3a3a;
  --txt:#e8e8e8; --sub:#9a9a9a; --gold:#e6b455; --red:#e05d5d;
  --green:#5dc98e; --blue:#5da9e0; --purple:#b48ee6; --cyan:#55c4c4;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);
  font-family:"PingFang SC","Microsoft YaHei","Segoe UI",sans-serif;
  font-size:14px;line-height:1.6;padding-bottom:60px}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px}
header{padding:26px 0 10px;border-bottom:1px solid var(--line)}
h1{font-size:21px;font-weight:700}
h1 .badge{font-size:11px;background:var(--gold);color:#1a1a1a;border-radius:4px;
  padding:2px 8px;vertical-align:3px;margin-left:10px;font-weight:700}
.meta{color:var(--sub);font-size:12px;margin-top:6px}
h2{font-size:16px;font-weight:700;margin:34px 0 6px}
.desc{color:var(--sub);font-size:12px;margin-bottom:14px}
.grid{display:grid;gap:12px}
.g2{grid-template-columns:1fr 1fr}.g3{grid-template-columns:1fr 1fr 1fr}
.g4{grid-template-columns:repeat(4,1fr)}
@media(max-width:900px){.g2,.g3,.g4{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
.metric{background:var(--panel2);border-radius:8px;padding:10px 12px}
.metric .k{color:var(--sub);font-size:11px;letter-spacing:.4px}
.metric .v{font-size:20px;font-weight:700;margin-top:2px;font-variant-numeric:tabular-nums}
.metric .s{color:var(--sub);font-size:11px}
table{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
th{color:var(--sub);text-align:right;font-weight:500;padding:7px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td{text-align:right;padding:6px 8px;border-bottom:1px solid #2e2e2e;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
.pos{color:var(--red)} .neg{color:var(--green)}
.chip{display:inline-block;font-size:11px;border-radius:10px;padding:1px 10px;
  border:1px solid var(--line);color:var(--sub);margin:2px}
.ok{color:var(--green);font-weight:700}.warn{color:var(--gold);font-weight:700}
.light{font-size:34px;line-height:1}
svg text{font-family:"PingFang SC","Microsoft YaHei",sans-serif}
.note{color:var(--sub);font-size:11.5px;margin-top:8px}
.heatmap td,.heatmap th{padding:5px 6px;text-align:center;font-size:11.5px}
h3{font-size:13.5px;font-weight:700;margin:18px 0 8px;color:#d8c89a}
footer{margin-top:44px;padding-top:16px;border-top:1px solid var(--line);
  color:var(--sub);font-size:11.5px}
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1>因子看板 · amihud_20</h1>
  <div class="meta">Amihud 非流动性 |日收益|/亿元成交额 的 20 日均值（低流动性溢价）｜全A · 2023-01-01 ~ 2026-09-11｜IC horizon = 20 日｜数据更新至 2026-09-11</div>
</header>

<!-- ============ 1. 概览指标 ============ -->
<h2>概览指标</h2>
<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px">
    <div><b style="font-size:16px">amihud_20</b>
      <span class="chip">流动性 / liquidity</span>
      <span class="chip">production</span>
      <span class="chip">机制登记：friction 结构摩擦 + 风险补偿</span></div>
    <div style="color:var(--sub);font-size:11.5px">起始 2022-01-04｜更新 2026-09-11｜因子池 311 个（已审计 311 · 0 FAIL）</div>
  </div>
  <div class="grid g4" style="margin-top:12px">
    <div class="metric"><div class="k">IC_MEAN（Pearson）</div><div class="v" id="m_ic"></div></div>
    <div class="metric"><div class="k">Rank_IC（h=20）</div><div class="v">+0.0790</div></div>
    <div class="metric"><div class="k">IC_IR</div><div class="v">+0.456</div></div>
    <div class="metric"><div class="k">IC_STD</div><div class="v">0.1732</div></div>
  </div>
  <div class="grid g4" style="margin-top:8px">
    <div class="metric"><div class="k">FDR q（BH + 自相关折减）</div><div class="v" style="color:var(--green)">0.0007</div><div class="s">311 因子多重检验校正</div></div>
    <div class="metric"><div class="k">t_adj</div><div class="v">3.73</div><div class="s">库级 ρ=0.9｜本窗实测 ρ=0.954 → t=2.08</div></div>
    <div class="metric"><div class="k">PIT 审计</div><div class="v" style="color:var(--green)">PASS</div><div class="s">时点信息合规</div></div>
    <div class="metric"><div class="k">IC 半衰期</div><div class="v">&gt;120 日</div><div class="s">慢周期配置型信号</div></div>
  </div>
</div>

<!-- ============ 2. 结论摘要 ============ -->
<h2>结论摘要</h2>
<div class="card">
  <div style="display:flex;gap:24px;align-items:center;flex-wrap:wrap">
    <div><div class="light">🟡</div><div style="color:var(--sub);font-size:11px">综合判定（初筛）</div></div>
    <div style="flex:1;min-width:280px">
      <b>黄色：两项冗余标记 + 正交化增量微弱</b><br>
      <span style="color:var(--sub);font-size:12px">与 size 相关 −0.836、与 hf_amihud_20 相关 +0.874（|ρ|&gt;0.7）；对 size 正交化后残差 IC 仅 +0.0129（原始 +0.0790，保留 16%）。统计与合规本身全绿——降级来自信息冗余，不是统计不显著。</span>
    </div>
  </div>
  <table style="margin-top:12px">
    <tr><th>检验项</th><th>结果</th><th>判定</th></tr>
    <tr><td>PIT / 结构审计（时点信息合规）</td><td>PASS（311 因子全库 0 FAIL）</td><td class="ok">✅</td></tr>
    <tr><td>现场重算 IC20 / ICIR</td><td>+0.0790 / +0.456（875 交易日）</td><td class="ok">✅</td></tr>
    <tr><td>FDR q（BH + ρ 自相关折减）</td><td>0.0007（t_adj 3.73，HLZ 门槛 3.0）</td><td class="ok">✅</td></tr>
    <tr><td>分组单调性（五分位 / 十分位）</td><td>1.00 / 1.00</td><td class="ok">✅</td></tr>
    <tr><td>近 12M 滚动 ICIR</td><td>0.225（区间 [−0.04, 1.15]）</td><td class="ok">✅</td></tr>
    <tr><td>经济机制登记</td><td>friction + 风险补偿（已登记）</td><td class="ok">✅</td></tr>
    <tr><td>年度 IC 反号</td><td>0/4 年</td><td class="ok">✅</td></tr>
    <tr><td>核心因子冗余</td><td>size −0.836 ｜ hf_amihud_20 +0.874</td><td class="warn">🟡</td></tr>
    <tr><td>正交化增量（对 size）</td><td>残差 IC +0.0129 vs 原始 +0.0790</td><td class="warn">🟡</td></tr>
  </table>
</div>

<!-- ============ 3. IC 分析 ============ -->
<h2>IC 分析</h2>
<div class="desc">多周期结构 · 分布形态 · 序列自相关 · 月度时变性</div>

<div class="grid g2">
  <div class="card"><b>多周期 IC / ICIR 谱（持有期 h = 1 ~ 120 日）</b>
    <div id="chart_decay"></div>
    <div class="note">横轴为<b>持有期长度</b>（信号预测的未来收益区间），非时间：|IC| 随 h 拉长单调增强，120 日内未跌破峰值一半（半衰期 &gt;120 日）——慢周期配置型信号，20 日调仓不损耗预测力；h=1 时 IC 已 +0.025，短端即有效。</div>
  </div>
  <div class="card"><b>IC 分布（skew = −0.769，kurt = 0.593）</b>
    <div id="chart_hist"></div>
    <div class="note">左偏（负偏）= 存在 IC 深负的坏月份（2024-01 微盘崩塌 −0.431），均值被尾部拖累——坏月份的定位见右下月度热力图。</div>
  </div>
  <div class="card"><b>IC 自相关（lag 1 ~ 10）</b>
    <div id="chart_acf"></div>
    <div class="note">lag1 ρ=0.954：20 日 IC 序列相邻共享 19/20 收益窗口。多重检验的 N_eff 折减即以此为据——不做折减时 276/311 因子"显著"，等于没筛。</div>
  </div>
  <div class="card"><b>月度 IC 热力图</b>
    <div style="overflow-x:auto"><table class="heatmap" id="tbl_monthly"></table></div>
    <div class="note">2024-01 的 −0.431（微盘崩塌月）、2026-05 的 −0.144 一眼可见——IC 均值被这些月份拖累，也是左偏分布的来源。</div>
  </div>
</div>

<div class="grid g4" style="margin-top:12px">
  <div class="metric"><div class="k">P(IC &gt; 0.02)</div><div class="v">67.2%</div></div>
  <div class="metric"><div class="k">P(IC &lt; −0.02)</div><div class="v">24.7%</div></div>
  <div class="metric"><div class="k">t-统计量</div><div class="v">2.08</div><div class="s">N_eff=21（lag1 ρ=0.954）｜库级口径 3.73</div></div>
  <div class="metric"><div class="k">p-value（折减后）</div><div class="v">0.038</div><div class="s">分组单调性 1.00</div></div>
</div>

<!-- ============ 4. 分组分析 ============ -->
<h2>分组分析</h2>
<div class="desc">五分位看整体单调结构，十分位看尾部集中度（组合实际买入的是最右一组）；口径 = 20 日远期收益逐日摊薄（非真实可交易组合，终审须组合层 A/B）</div>

<div class="card"><b>分组收益（五分位 + 多空）</b>
  <table id="tbl_groups" style="margin-top:8px"></table>
</div>

<div class="grid g2" style="margin-top:12px">
  <div class="card"><b>分组净值曲线（2023-01 ~ 2026-09）</b>
    <div id="chart_nav"></div>
    <div class="note">Q1~Q5 = 因子值五分位组逐日摊薄净值；金线 = 多空（Q5−Q1）。</div>
  </div>
  <div class="card"><b>十分位分组 · 尾部集中度（全期均值，bp/20日）</b>
    <div id="chart_decile"></div>
    <div class="note">与左侧五分位同源、加细到十组：D9→D10 的增量（207→336bp）才是组合真正买入的部分，五分位看不出尾部结构。多空（D10−D1）= +283bp/20日。</div>
  </div>
  <div class="card"><b>多空腿可收割性（A 股做空约束）</b>
    <table style="margin-top:6px">
      <tr><th>年</th><th>多头腿 Q5</th><th>空头腿 Q1</th><th>多空</th><th>多头占比</th></tr>
      <tr><td>2023</td><td class="pos">+230bp</td><td class="neg">−180bp</td><td class="pos">+410</td><td>56%</td></tr>
      <tr><td>2024</td><td class="pos">+262bp</td><td class="pos">+128bp</td><td class="pos">+134</td><td>196%（空头腿也涨）</td></tr>
      <tr><td>2025</td><td class="pos">+532bp</td><td class="pos">+285bp</td><td class="pos">+247</td><td>215%（空头腿也涨）</td></tr>
      <tr><td>2026</td><td class="neg">−71bp</td><td class="neg">−59bp</td><td class="neg">−12</td><td>—（同负）</td></tr>
    </table>
    <div class="note">A 股无低成本做空：预测力集中在空头腿 = 不可收割。2024/2025 多头占比 &gt;100% = 空头腿自身为正，多空价差全部由多头腿贡献——直接回答"IC 好的钱到底能不能赚"。</div>
  </div>
  <div class="card"><b>年度分解（含五分位组收益，bp/20日）</b>
    <table style="margin-top:8px">
      <tr><th>年</th><th>天数</th><th>IC</th><th>ICIR</th><th>胜率</th><th>Q1</th><th>Q2</th><th>Q3</th><th>Q4</th><th>Q5</th><th>多空</th></tr>
      <tr><td>2023</td><td>242</td><td class="pos">+0.1470</td><td>+1.090</td><td>87%</td><td class="neg">−180</td><td class="neg">−86</td><td class="neg">−23</td><td class="pos">+57</td><td class="pos">+230</td><td class="pos">+410</td></tr>
      <tr><td>2024</td><td>242</td><td class="pos">+0.0215</td><td>+0.098</td><td>62%</td><td class="pos">+128</td><td class="pos">+107</td><td class="pos">+110</td><td class="pos">+124</td><td class="pos">+262</td><td class="pos">+134</td></tr>
      <tr><td>2025</td><td>243</td><td class="pos">+0.0902</td><td>+0.647</td><td>74%</td><td class="pos">+285</td><td class="pos">+304</td><td class="pos">+341</td><td class="pos">+406</td><td class="pos">+532</td><td class="pos">+247</td></tr>
      <tr><td>2026</td><td>148</td><td class="pos">+0.0434</td><td>+0.282</td><td>59%</td><td class="neg">−59</td><td class="neg">−120</td><td class="neg">−90</td><td class="neg">−23</td><td class="neg">−71</td><td class="neg">−12</td></tr>
    </table>
    <div class="note">2026 年多空腿整体转负（−12bp/20日）——信号衰减的年度级证据，与"近 12M 滚动 ICIR 0.225（区间下限 −0.04）"互相印证。</div>
  </div>
</div>

<!-- ============ 5. 信息增量 ============ -->
<h2>信息增量</h2>
<div class="desc">该因子在现有生产因子集之外还能贡献多少独立信息</div>

<div class="grid g2">
  <div class="card"><b>正交化增量（对生产因子 size 的边际贡献）</b>
    <div id="chart_resid"></div>
    <div class="note">对 size 逐日截面 OLS 取残差后的 rank IC：原始 +0.0790 → 残差 +0.0129，<b>仅保留 16% 增量</b>（ICIR 0.456→0.099）。反向：size 对 amihud 正交化保留 73%。结论：PROD 双因子里 size 是主信息载体，amihud_20 边际贡献小，且与 hf_amihud_20（ρ=+0.874）近似同一信号。</div>
  </div>
  <div class="card"><b>与核心因子截面相关（最近 250 日 spearman 均值）</b>
    <table style="margin-top:6px">
      <tr><th>核心因子</th><th>ρ</th><th>天数</th><th>判定</th></tr>
      <tr><td>size</td><td class="neg">−0.836</td><td>250</td><td class="warn">⚠️ 冗余（|ρ|&gt;0.7）</td></tr>
      <tr><td>sue_i</td><td>−0.080</td><td>250</td><td class="ok">独立</td></tr>
      <tr><td>hf_amihud_20</td><td class="pos">+0.874</td><td>250</td><td class="warn">⚠️ 冗余（|ρ|&gt;0.7）</td></tr>
    </table>
    <div class="note">与 sue_i 基本正交；对 size 与 hf_amihud_20 双冗余——与左图正交化结论一致，是本因子降级 🟡 的直接原因。</div>
  </div>
</div>

<!-- ============ 6. 最新数据 ============ -->
<h2>最新数据</h2>
<div class="card">
  <table id="tbl_latest"></table>
  <div class="note">PIT 口径：因子值基于当日及以前公开数据计算，无 as-of 回填、无前视。</div>
</div>

<footer>
  口径附注：IC 为全市场 rank IC（h=20，后复权 LEAD 收益）；分组收益 = 20 日远期收益逐日摊薄（非可交易组合，终审须组合层 A/B）；FDR 基于 311 因子审计快照（BH + ρ=0.9 自相关折减），本窗实测 ρ=0.954 → t_adj=2.08，阈值对自相关假设敏感；正交化增量 = 对 size 逐日截面 OLS 残差 rank IC；多空腿以 A 股做空约束为前提解读。红绿灯仅初筛，入生产候选须组合层 A/B 终审。<br>
  数据源：QuantLab 因子湖 + DuckDB（只读）｜生成：2026-09-12｜评估引擎 scripts/factor_eval/run_eval.py（v3）
</footer>
</div>

<script>
const DATA = __DATA__;
document.getElementById('m_ic').textContent = (DATA.ic_pearson_mean>=0?'+':'')+DATA.ic_pearson_mean;

const C={q:'#9a9a9a',q1:'#5da9e0',q2:'#7bb8e8',q3:'#9a9a9a',q4:'#e0a35d',q5:'#e05d5d',ls:'#e6b455',ic:'#e05d5d',icir:'#55c4c4'};
function svgEl(w,h){const s=document.createElementNS('http://www.w3.org/2000/svg','svg');
  s.setAttribute('viewBox','0 0 '+w+' '+h);s.setAttribute('width','100%');return s}
function add(s,tag,attrs,txt){const e=document.createElementNS('http://www.w3.org/2000/svg',tag);
  for(const k in attrs)e.setAttribute(k,attrs[k]);if(txt!=null)e.textContent=txt;s.appendChild(e);return e}

/* ---- 折线图（支持双轴：右轴刻度 + 右轴线，刻度位置用右轴比例）---- */
function lineChart(el,xs,series,xlabels,fmt){
  const W=520,H=230,L=46,R=52,T=16,B=30,w=W-L-R,h=H-T-B;
  const s=svgEl(W,H);el.appendChild(s);
  const main=series.filter(p=>!p.axis2), sec=series.filter(p=>p.axis2);
  const ymin=Math.min(...main.flatMap(p=>p.data)),ymax=Math.max(...main.flatMap(p=>p.data));
  let ymin2=0,ymax2=1;
  if(sec.length){ymin2=Math.min(...sec.flatMap(p=>p.data));ymax2=Math.max(...sec.flatMap(p=>p.data));
    const pad2=(ymax2-ymin2)*0.08||0.05;ymin2-=pad2;ymax2+=pad2;}
  const X=i=>L+(xs.length>1?i/(xs.length-1)*w:0);
  const Y=v=>T+h-(v-ymin)/(ymax-ymin)*h, Y2=v=>T+h-(v-ymin2)/(ymax2-ymin2)*h;
  for(let g=0;g<=4;g++){
    const v=ymin+(ymax-ymin)*g/4;
    add(s,'line',{x1:L,y1:Y(v),x2:W-R,y2:Y(v),stroke:'#333','stroke-width':1});
    add(s,'text',{x:L-5,y:Y(v)+3,'text-anchor':'end',fill:'#9a9a9a','font-size':9},fmt?fmt(v):v.toFixed(2));
    if(sec.length){
      const v2=ymin2+(ymax2-ymin2)*g/4;
      /* 右轴刻度：位置用 Y2（右轴比例）——修复旧版误用 Y() 导致右轴不显示 */
      add(s,'text',{x:W-R+6,y:Y2(v2)+3,fill:'#55c4c4','font-size':9},v2.toFixed(2));}}
  if(sec.length){add(s,'line',{x1:W-R,y1:T,x2:W-R,y2:T+h,stroke:'#55c4c4','stroke-width':1,opacity:0.5});}
  const nx=Math.min(xs.length,8);
  for(let i=0;i<nx;i++){const xi=Math.round(i*(xs.length-1)/(nx-1));
    add(s,'text',{x:X(xi),y:H-10,'text-anchor':'middle',fill:'#9a9a9a','font-size':9},xlabels?xlabels[xi]:xs[xi]);}
  series.forEach(p=>{
    add(s,'polyline',{fill:'none',stroke:p.color,'stroke-width':2,
      points:p.data.map((v,i)=>X(i)+','+(p.axis2?Y2(v):Y(v))).join(' ')});
    if(p.data.length<=10)p.data.forEach((v,i)=>add(s,'circle',{cx:X(i),cy:p.axis2?Y2(v):Y(v),r:3,fill:p.color}));});
  let lx=L+4;series.forEach(p=>{add(s,'circle',{cx:lx,cy:T+2,r:3,fill:p.color});
    const t=add(s,'text',{x:lx+7,y:T+5,fill:'#bbb','font-size':10},p.name);lx+=7+t.getComputedTextLength()+16;});
}

/* ---- 多周期 IC 谱 ---- */
const hs=[1,3,5,10,20,40,60,120];
lineChart(document.getElementById('chart_decay'),hs,
  [{name:'IC',color:C.ic,data:__DECAY_IC__},
   {name:'ICIR（右轴）',color:C.icir,data:__DECAY_ICIR__,axis2:true}],
  hs.map(String),v=>v.toFixed(2));

/* ---- 直方图 ---- */
(function(){const el=document.getElementById('chart_hist');
  const W=520,H=230,L=46,R=12,T=14,B=30,w=W-L-R,h=H-T-B;
  const s=svgEl(W,H);el.appendChild(s);
  const arr=DATA.ic_series,nb=30,mn=Math.min(...arr),mx=Math.max(...arr);
  const bins=new Array(nb).fill(0);
  arr.forEach(v=>{let i=Math.floor((v-mn)/(mx-mn)*nb);if(i>=nb)i=nb-1;bins[i]++;});
  const bm=Math.max(...bins);
  bins.forEach((b,i)=>{const bw=w/nb;
    add(s,'rect',{x:L+i*bw+0.5,y:T+h-b/bm*h,width:bw-1,height:b/bm*h,fill:'#e05d5d',opacity:0.85});});
  const mean=DATA.ic_stats.mean;
  const X=v=>L+(v-mn)/(mx-mn)*w;
  add(s,'line',{x1:X(mean),y1:T,x2:X(mean),y2:T+h,stroke:'#e6b455','stroke-width':1.5,'stroke-dasharray':'4 3'});
  add(s,'text',{x:X(mean)+4,y:T+10,fill:'#e6b455','font-size':10},'mean '+mean);
  for(let g=0;g<=4;g++){const v=mn+(mx-mn)*g/4;
    add(s,'line',{x1:L,y1:T+h,x2:W-R,y2:T+h,stroke:'#333'});
    add(s,'text',{x:L+(mx-mn)*g/4/(mx-mn)*w,y:T+h+14,'text-anchor':'middle',fill:'#9a9a9a','font-size':9},v.toFixed(2));}})();

/* ---- 自相关 ---- */
(function(){const el=document.getElementById('chart_acf');
  const W=520,H=230,L=46,R=12,T=14,B=30,w=W-L-R,h=H-T-B;
  const s=svgEl(W,H);el.appendChild(s);
  const acf=DATA.ic_acf,bm=1;
  add(s,'line',{x1:L,y1:T+h/2,x2:W-R,y2:T+h/2,stroke:'#555'});
  acf.forEach((v,i)=>{const bw=w/acf.length,x=L+i*bw;
    const hh=Math.abs(v)/bm*h/2;
    add(s,'rect',{x:x+bw*0.25,y:v>=0?T+h/2-hh:T+h/2,width:bw*0.5,height:hh,
      fill:v>=0?'#55c4c4':'#e05d5d'});
    add(s,'text',{x:x+bw*0.5,y:H-10,'text-anchor':'middle',fill:'#9a9a9a','font-size':9},(i+1));});
  for(let g=0;g<=4;g++){const v=1-g/4;
    add(s,'text',{x:L-5,y:T+h*(1-v)/2+3,'text-anchor':'end',fill:'#9a9a9a','font-size':9},v.toFixed(1));
    add(s,'line',{x1:L,y1:T+h*(1-v)/2,x2:W-R,y2:T+h*(1-v)/2,stroke:'#333'});}})();

/* ---- 分组净值 ---- */
(function(){const keys=['Q1','Q2','Q3','Q4','Q5'];
  const series=keys.map(k=>({name:k,color:C[k.toLowerCase()],data:DATA.navs[k]}));
  series.push({name:'多空',color:C.ls,data:DATA.navs.LS});
  const xs=DATA.navs.Q1.map((_,i)=>i);
  lineChart(document.getElementById('chart_nav'),xs,series,DATA.nav_dates,v=>v.toFixed(1));})();

/* ---- 十分位 ---- */
(function(){const el=document.getElementById('chart_decile');
  const W=520,H=230,L=40,R=12,T=14,B=30,w=W-L-R,h=H-T-B;
  const s=svgEl(W,H);el.appendChild(s);
  const d=__DECILE__,mx=Math.max(...d.map(Math.abs));
  d.forEach((v,i)=>{const bw=w/10;
    add(s,'rect',{x:L+i*bw+1,y:T+h-v/mx*h*0.9,width:bw-2,height:v/mx*h*0.9,
      fill:i===9?'#e6b455':'#5da9e0',opacity:i===9?1:0.8});
    add(s,'text',{x:L+i*bw+bw/2,y:T+h-v/mx*h*0.9-4,'text-anchor':'middle',fill:'#bbb','font-size':9},v);
    add(s,'text',{x:L+i*bw+bw/2,y:H-12,'text-anchor':'middle',fill:'#9a9a9a','font-size':9},'D'+(i+1));});
  add(s,'line',{x1:L,y1:T+h,x2:W-R,y2:T+h,stroke:'#333'});})();

/* ---- 正交化增量对比 ---- */
(function(){const el=document.getElementById('chart_resid');
  const W=520,H=170,L=40,R=12,T=20,B=30,w=W-L-R,h=H-T-B;
  const s=svgEl(W,H);el.appendChild(s);
  const items=[['原始 IC',0.0790,C.q5],['正交化残差 IC',0.0129,'#e6b455']];
  const mx=0.09;
  items.forEach((it,i)=>{const bw=w/items.length*0.5,x=L+i*(w/items.length)+bw*0.25;
    add(s,'rect',{x,y:T+h-it[1]/mx*h,width:bw,height:it[1]/mx*h,fill:it[2]});
    add(s,'text',{x:x+bw/2,y:T+h-it[1]/mx*h-6,'text-anchor':'middle',fill:'#eee','font-size':12,'font-weight':700},'+'+it[1]);
    add(s,'text',{x:x+bw/2,y:T+h+14,'text-anchor':'middle',fill:'#9a9a9a','font-size':10},it[0]);});
  add(s,'line',{x1:L,y1:T+h,x2:W-R,y2:T+h,stroke:'#333'});})();

/* ---- 表格 ---- */
(function(){const t=document.getElementById('tbl_groups');
  const rows=[['Q1','分组1'],['Q2','分组2'],['Q3','分组3'],['Q4','分组4'],['Q5','分组5（最优）'],['LS','多空组合（Q5−Q1）']];
  let html='<tr><th>分组</th><th>20日均值(bp)</th><th>年化收益</th><th>年化波动</th><th>夏普</th><th>最大回撤</th><th>月度胜率</th></tr>';
  rows.forEach(r=>{const g=DATA.groups[r[0]];
    html+='<tr><td>'+r[1]+'</td><td>'+(g.mean_fwd_bp>0?'+':'')+g.mean_fwd_bp+'</td>'+
      '<td class="'+(g.ann_ret>=0?'pos':'neg')+'">'+(g.ann_ret*100).toFixed(1)+'%</td>'+
      '<td>'+(g.ann_vol*100).toFixed(1)+'%</td><td>'+g.sharpe+'</td>'+
      '<td class="neg">'+(g.mdd*100).toFixed(1)+'%</td><td>'+(g.mon_win*100).toFixed(0)+'%</td></tr>';});
  t.innerHTML=html;})();

(function(){const t=document.getElementById('tbl_latest');
  let html='<tr><th>日期</th><th>股票代码</th><th>因子值</th></tr>';
  DATA.latest.top.forEach(r=>{html+='<tr><td>'+DATA.latest.date+'</td><td>'+r.code+'</td><td>'+r.value+'</td></tr>';});
  t.innerHTML=html;})();

(function(){const t=document.getElementById('tbl_monthly');
  const m=DATA.monthly_ic;
  let html='<tr><th>年</th>';m.months.forEach(x=>html+='<th>'+x+'月</th>');html+='</tr>';
  m.years.forEach((y,i)=>{html+='<tr><td>'+y+'</td>';
    m.values[i].forEach(v=>{let bg='',c='#777';
      if(v!=null){bg='rgba(224,93,93,'+Math.min(Math.abs(v)*1.8,0.85)+')';c=v>0.15?'#fff':'#bbb';
        if(v<0)bg='rgba(93,201,142,'+Math.min(Math.abs(v)*1.8,0.85)+')';}
      html+='<td style="background:'+bg+';color:'+c+'">'+(v==null?'—':(v>0?'+':'')+v.toFixed(3))+'</td>';});
    html+='</tr>';});
  t.innerHTML=html;})();
</script>
</body>
</html>"""

decay_ic = [0.0248, 0.0368, 0.0456, 0.0608, 0.0790, 0.0945, 0.1038, 0.1371]
decay_icir = [0.155, 0.227, 0.281, 0.366, 0.456, 0.523, 0.575, 0.746]
decile = [53, 56, 60, 80, 92, 116, 147, 170, 207, 336]

html = (TEMPLATE
        .replace("__DATA__", json.dumps(D, ensure_ascii=False))
        .replace("__DECAY_IC__", json.dumps(decay_ic))
        .replace("__DECAY_ICIR__", json.dumps(decay_icir))
        .replace("__DECILE__", json.dumps(decile)))

out = ROOT / "docs" / "factor_center_amihud_20.html"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(html, encoding="utf-8")
print(f"[built] {out}  ({out.stat().st_size:,} bytes)")
print("pearson IC_MEAN =", D["ic_pearson_mean"])
