"""构建因子中心展示页：PandaAI 同款评估项 + QuantLab 独有增强区。
输入 reports/factor_center_demo_data.json + v3 评估报告静态结论 →
输出 docs/factor_center_amihud_20.html（单文件，无外部依赖）。"""
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
<title>QuantLab 因子中心 · 因子分析增强面板（amihud_20）</title>
<style>
:root{
  --bg:#1a1a1a; --panel:#232323; --panel2:#2a2a2a; --line:#3a3a3a;
  --txt:#e8e8e8; --sub:#9a9a9a; --gold:#e6b455; --red:#e05d5d;
  --green:#5dc98e; --blue:#5da9e0; --purple:#b48ee6; --cyan:#55c4c4;
  --ql:#e6b455;
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
h2{font-size:16px;font-weight:700;margin:34px 0 6px;display:flex;align-items:center;gap:8px}
h2 .tag{font-size:10.5px;font-weight:700;border-radius:3px;padding:1px 7px}
.tag.panda{background:#3d4a5c;color:#a8c4e8}
.tag.ql{background:var(--gold);color:#1a1a1a}
.desc{color:var(--sub);font-size:12px;margin-bottom:14px}
.grid{display:grid;gap:12px}
.g2{grid-template-columns:1fr 1fr}.g3{grid-template-columns:1fr 1fr 1fr}
.g4{grid-template-columns:repeat(4,1fr)}
@media(max-width:900px){.g2,.g3,.g4{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
.card.ql{border-color:#6b5a33;background:#262215}
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
.bad{color:var(--red);font-weight:700}
.light{font-size:34px;line-height:1}
svg text{font-family:"PingFang SC","Microsoft YaHei",sans-serif}
.note{color:var(--sub);font-size:11.5px;margin-top:8px}
.heatmap td,.heatmap th{padding:5px 6px;text-align:center;font-size:11.5px}
footer{margin-top:44px;padding-top:16px;border-top:1px solid var(--line);
  color:var(--sub);font-size:11.5px}
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1>QuantLab 因子中心 · 因子分析增强面板<span class="badge">QL+ 独有评估已叠加</span></h1>
  <div class="meta">示例因子 <b>amihud_20</b>（Amihud 非流动性 · 20日均值）｜全A · 2023-01-01 ~ 2026-09-11 ｜数据更新至 2026-09-11｜对标 PandaAI 因子中心布局，所有数字为 QuantLab 真实计算</div>
</header>

<h2><span class="tag panda">PandaAI 同款</span>对标总览：他们有什么，我们加了什么</h2>
<table>
<tr><th>PandaAI 因子中心评估项</th><th>展现形式</th><th>本页</th><th>QuantLab 增强（QL+）</th></tr>
<tr><td>IC_MEAN / RANK_IC / IC_IR / IC_STD</td><td>因子卡片四指标</td><td>✓ 同款四指标</td><td class="pos">+ FDR q 多重检验校正、HLZ t&gt;3 门槛</td></tr>
<tr><td>绩效概览（收益/夏普/回撤）</td><td>指标网格</td><td>✓ 最优分组全指标</td><td class="pos">+ 盈亏平衡成本 / 安全边际 / TC（模型层）</td></tr>
<tr><td>IC 指标（P 值 / t 统计量 / 单调性）</td><td>指标网格</td><td>✓ 含 skew / kurt</td><td class="pos">+ 自相关折减 N_eff 后的诚实 t 值</td></tr>
<tr><td>IC 衰减图</td><td>折线图</td><td>✓ h=1~120 全谱</td><td class="pos">+ 半衰期定量 + 换手下限解读</td></tr>
<tr><td>IC 分布图（skew/kurt）</td><td>直方图</td><td>✓</td><td>—</td></tr>
<tr><td>IC 自相关图</td><td>柱状图</td><td>✓ lag 1~10</td><td class="pos">+ 联动 N_eff（多重检验的地基）</td></tr>
<tr><td>5 组收益 + 分组收益表</td><td>净值图 + 表格</td><td>✓ 五组 + 多空</td><td class="pos">+ 十分位分组 / 尾部集中度 / 多空腿可收割性</td></tr>
<tr><td>最新数据（Top 因子值）</td><td>表格</td><td>✓</td><td class="pos">+ PIT 时点口径声明</td></tr>
<tr><td>—（无对应）</td><td>—</td><td class="pos">QL+</td><td class="pos">红绿灯综合判定 / PIT 审计 / 正交化增量 / 经济机制登记 / 月度 IC 热力 / 报告归档检索</td></tr>
</table>

<h2><span class="tag panda">PandaAI 同款</span>因子卡片</h2>
<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px">
    <div><b style="font-size:16px">amihud_20</b>
      <span class="chip">流动性 / liquidity</span>
      <span class="chip">production</span>
      <span class="chip">equity</span></div>
    <div style="color:var(--sub);font-size:11.5px">起始 2022-01-04｜更新 2026-09-11｜因子池 311 个（已审计 311 · 0 FAIL）</div>
  </div>
  <div style="color:var(--sub);font-size:12px;margin:4px 0 12px">Amihud 非流动性 |日收益|/亿元成交额 的 20 日均值（低流动性溢价）｜经济机制登记：friction 结构摩擦 + 风险补偿 ✓</div>
  <div class="grid g4">
    <div class="metric"><div class="k">IC_MEAN（Pearson）</div><div class="v" id="m_ic"></div></div>
    <div class="metric"><div class="k">RANK_IC</div><div class="v">+0.0790</div></div>
    <div class="metric"><div class="k">IC_IR</div><div class="v">+0.456</div></div>
    <div class="metric"><div class="k">IC_STD</div><div class="v">0.1732</div></div>
  </div>
  <div class="grid g4" style="margin-top:8px">
    <div class="metric"><div class="k">FDR q（BH+自相关折减）</div><div class="v" style="color:var(--green)">0.0007</div><div class="s">QL+ 311 因子多重检验</div></div>
    <div class="metric"><div class="k">t_adj（库级 ρ=0.9）</div><div class="v">3.73</div><div class="s">HLZ 门槛 3.0 ✓｜本窗实测 ρ=0.954 → t=2.08</div></div>
    <div class="metric"><div class="k">PIT 审计</div><div class="v" style="color:var(--green)">PASS</div><div class="s">QL+ 时点信息合规</div></div>
    <div class="metric"><div class="k">IC 半衰期</div><div class="v">&gt;120 日</div><div class="s">QL+ 慢周期配置型信号</div></div>
  </div>
</div>

<h2><span class="tag panda">PandaAI 同款</span>因子分析面板</h2>
<div class="desc">PandaAI 深面板结构：绩效概览 → IC 指标（含 P/t/单调性）→ 最新数据 → 分组收益 → IC 衰减/分布/自相关图。以下全部为 QuantLab 真实数据同款重算。</div>

<div class="grid g4">
  <div class="metric"><div class="k">因子收益（最优分组 Q5 年化）</div><div class="v pos">+34.2%</div><div class="s">20日远期逐日摊薄口径</div></div>
  <div class="metric"><div class="k">夏普比率（Q5）</div><div class="v" id="m_q5sh"></div></div>
  <div class="metric"><div class="k">最大回撤（Q5）</div><div class="v neg" id="m_q5dd"></div></div>
  <div class="metric"><div class="k">多空年化（Q5−Q1）</div><div class="v pos">+27.3%</div><div class="s">⚠️ QL+ 提示：A股不可低成本做空</div></div>
</div>
<div class="grid g4" style="margin-top:8px">
  <div class="metric"><div class="k">IC_mean</div><div class="v">+0.0790</div></div>
  <div class="metric"><div class="k">Rank_IC</div><div class="v">+0.0790</div></div>
  <div class="metric"><div class="k">IC_std</div><div class="v">0.1732</div></div>
  <div class="metric"><div class="k">IC_IR</div><div class="v">+0.456</div></div>
</div>
<div class="grid g4" style="margin-top:8px">
  <div class="metric"><div class="k">P(IC &gt; 0.02)</div><div class="v">67.2%</div></div>
  <div class="metric"><div class="k">P(IC &lt; −0.02)</div><div class="v">24.7%</div></div>
  <div class="metric"><div class="k">t-统计量</div><div class="v">2.08</div><div class="s">N_eff=21（lag1 ρ=0.954）｜库级口径 3.73</div></div>
  <div class="metric"><div class="k">p-value（折减后）</div><div class="v">0.038</div><div class="s">单调性 1.00（五分位）</div></div>
</div>

<div class="grid g2" style="margin-top:14px">
  <div class="card"><b>factor_value IC 衰减图（QL+ 全周期谱 h=1~120）</b>
    <div id="chart_decay"></div>
    <div class="note">PandaAI 展示单条衰减曲线；QuantLab 同时给 IC / ICIR 双轴谱 + 半衰期（&gt;120 日未衰减过半 = 慢周期配置型信号，20 日调仓不损耗预测力；h=1 IC +0.025 短端即有效）。</div>
  </div>
  <div class="card"><b>factor_value IC distribution　skew=−0.769　kurt=0.593</b>
    <div id="chart_hist"></div>
    <div class="note">PandaAI 同款分布图。左偏（负偏）= 存在 IC 深负的坏月份（2024-01 微盘崩塌 −0.431），均值被尾部拖累——QL+ 月度热力图进一步定位坏月份。</div>
  </div>
  <div class="card"><b>factor_value IC 自相关图（lag 1~10）</b>
    <div id="chart_acf"></div>
    <div class="note">lag1 ρ=0.954：20 日 IC 序列相邻共享 19/20 收益窗口。QL+ 以此做 N_eff 折减——不做折减时 276/311 因子“显著”，等于没筛。</div>
  </div>
  <div class="card"><b>factor_value 5 groups return（净值，2023-01 ~ 2026-09）</b>
    <div id="chart_nav"></div>
    <div class="note">Q1~Q5 = 因子值五分位组逐日摊薄净值；灰线 = 多空（Q5−Q1）。</div>
  </div>
</div>

<div class="card" style="margin-top:12px"><b>分组收益（五分位 + 多空，20 日远期 · 逐日摊薄）</b>
  <table id="tbl_groups" style="margin-top:8px"></table>
  <div class="note">PandaAI 分组表含年化/超额/回撤/波动/换手/月胜率/夏普/IR；QuantLab 同位置展示同源指标，口径=20日远期收益逐日摊薄（非真实可交易组合，分组回测须过组合层 A/B 终审）。</div>
</div>

<div class="card" style="margin-top:12px"><b>最新数据（Top 10 因子值）</b>
  <table id="tbl_latest" style="margin-top:8px"></table>
  <div class="note">QL+ PIT 口径：因子值基于当日及以前公开数据计算，无 as-of 回填、无前视。</div>
</div>

<h2><span class="tag ql">QL+ QuantLab 独有</span>红绿灯综合判定与逐项依据</h2>
<div class="card ql">
  <div style="display:flex;gap:24px;align-items:center;flex-wrap:wrap">
    <div><div class="light">🟡</div><div style="color:var(--sub);font-size:11px">综合判定（初筛）</div></div>
    <div style="flex:1;min-width:280px">
      <b>黄色：两项冗余标记 + 正交化增量微弱</b><br>
      <span style="color:var(--sub);font-size:12px">与 size 相关 −0.84、与 hf_amihud_20 相关 +0.87（|ρ|&gt;0.7）；对 size 正交化后残差 IC 仅 +0.0129（原始 +0.0790，保留 16%）。统计与合规本身全绿——降级来自信息冗余，这是 PandaAI 体系完全看不见的一层。</span>
    </div>
  </div>
  <table style="margin-top:12px">
    <tr><th>检验项</th><th>结果</th><th>判定</th><th>PandaAI 是否覆盖</th></tr>
    <tr><td>PIT / 结构审计（时点信息合规）</td><td>PASS（311 因子全库 0 FAIL）</td><td class="ok">✅</td><td style="color:var(--sub)">无</td></tr>
    <tr><td>现场重算 IC20 / ICIR</td><td>+0.0790 / +0.456（875 交易日）</td><td class="ok">✅</td><td>部分（无现场审计对照）</td></tr>
    <tr><td>FDR q（BH + ρ 自相关折减）</td><td>0.0007（t_adj 3.73 &gt; HLZ 3.0）</td><td class="ok">✅</td><td style="color:var(--sub)">无（仅朴素 t/p）</td></tr>
    <tr><td>分组单调性（五分位 / 十分位）</td><td>1.00 / 1.00</td><td class="ok">✅</td><td>有（十分位无）</td></tr>
    <tr><td>近 12M 滚动 ICIR</td><td>0.225（区间 [−0.04, 1.15]）</td><td class="ok">✅</td><td style="color:var(--sub)">无</td></tr>
    <tr><td>经济机制登记</td><td>friction + 风险补偿（已登记）</td><td class="ok">✅</td><td style="color:var(--sub)">无</td></tr>
    <tr><td>年度 IC 反号</td><td>0/4 年</td><td class="ok">✅</td><td style="color:var(--sub)">无</td></tr>
    <tr><td>核心因子冗余</td><td>size −0.84 ｜ hf_amihud_20 +0.87</td><td class="warn">🟡</td><td style="color:var(--sub)">无</td></tr>
    <tr><td>正交化增量（对 size+amihud_20）</td><td>残差 IC +0.0129 vs 原始 +0.0790</td><td class="warn">🟡</td><td style="color:var(--sub)">无</td></tr>
  </table>
</div>

<div class="grid g2" style="margin-top:12px">
  <div class="card ql"><b>QL+ 十分位分组 · 尾部集中度（全期，bp）</b>
    <div id="chart_decile"></div>
    <div class="note">D9→D10 的增量（207→336bp）才是组合真正买入的部分；五分位看不出尾部结构。多空（D10−D1）= +283bp/20日。</div>
  </div>
  <div class="card ql"><b>QL+ 多空腿可收割性（A股做空约束）</b>
    <table style="margin-top:6px">
      <tr><th>年</th><th>多头腿 Q5</th><th>空头腿 Q1</th><th>多空</th><th>多头占比</th></tr>
      <tr><td>2023</td><td class="pos">+230bp</td><td class="neg">−180bp</td><td class="pos">+410</td><td>56%</td></tr>
      <tr><td>2024</td><td class="pos">+262bp</td><td class="pos">+128bp</td><td class="pos">+134</td><td>196%（空头腿也涨）</td></tr>
      <tr><td>2025</td><td class="pos">+532bp</td><td class="pos">+285bp</td><td class="pos">+247</td><td>215%（空头腿也涨）</td></tr>
      <tr><td>2026</td><td class="neg">−71bp</td><td class="neg">−59bp</td><td class="neg">−12</td><td>—（同负）</td></tr>
    </table>
    <div class="note">A 股无低成本做空：预测力集中在空头腿 = 不可收割。2024/2025 多头占比 &gt;100% = 空头腿自身为正收益，多空价差全部由多头腿贡献——此维度直接回答“IC 好的钱到底能不能赚”。</div>
  </div>
  <div class="card ql"><b>QL+ 正交化增量（对生产因子 size 的边际贡献）</b>
    <div id="chart_resid"></div>
    <div class="note">对 size 逐日截面 OLS 取残差后的 rank IC：原始 +0.0790 → 残差 +0.0129，<b>仅保留 16% 增量</b>（ICIR 0.456→0.099）。反向：size 对 amihud 正交化保留 73%。结论：PROD 双因子里 size 是主信息载体，amihud_20 边际贡献小，且与 hf_amihud_20（ρ=0.87）近似同一信号。</div>
  </div>
  <div class="card ql"><b>QL+ 月度 IC 热力图（时变性显式化）</b>
    <div style="overflow-x:auto"><table class="heatmap" id="tbl_monthly" style="margin-top:6px"></table></div>
    <div class="note">2024-01 的 −0.431（微盘崩塌月）、2026-05 的 −0.144 一眼可见——IC 均值被这些月份拖累，也是左偏分布的来源。</div>
  </div>
</div>

<div class="card ql" style="margin-top:12px"><b>QL+ 年度分解（含五分位组收益，bp/20日）</b>
  <table style="margin-top:8px">
    <tr><th>年</th><th>天数</th><th>IC</th><th>ICIR</th><th>胜率</th><th>Q1</th><th>Q2</th><th>Q3</th><th>Q4</th><th>Q5</th><th>多空</th></tr>
    <tr><td>2023</td><td>242</td><td class="pos">+0.1470</td><td>+1.090</td><td>87%</td><td class="neg">−180</td><td class="neg">−86</td><td class="neg">−23</td><td class="pos">+57</td><td class="pos">+230</td><td class="pos">+410</td></tr>
    <tr><td>2024</td><td>242</td><td class="pos">+0.0215</td><td>+0.098</td><td>62%</td><td class="pos">+128</td><td class="pos">+107</td><td class="pos">+110</td><td class="pos">+124</td><td class="pos">+262</td><td class="pos">+134</td></tr>
    <tr><td>2025</td><td>243</td><td class="pos">+0.0902</td><td>+0.647</td><td>74%</td><td class="pos">+285</td><td class="pos">+304</td><td class="pos">+341</td><td class="pos">+406</td><td class="pos">+532</td><td class="pos">+247</td></tr>
    <tr><td>2026</td><td>148</td><td class="pos">+0.0434</td><td>+0.282</td><td>59%</td><td class="neg">−59</td><td class="neg">−120</td><td class="neg">−90</td><td class="neg">−23</td><td class="neg">−71</td><td class="neg">−12</td></tr>
  </table>
  <div class="note">2026 年多空腿整体转负（−12bp/20日）——因子层级信号衰减的年度级证据，与“近 12M 滚动 ICIR 0.225（区间下限 −0.04）”互相印证。</div>
</div>

<div class="card ql" style="margin-top:12px"><b>QL+ 工程化能力（评估体系之外）</b>
  <table style="margin-top:8px">
    <tr><th>能力</th><th>说明</th></tr>
    <tr><td>报告归档仓库</td><td>reports/factor_eval/，命名 {type}_{name}_{yyyymmdd}_{HHMM}.md，同日重跑不覆盖，index.json 追加式索引</td></tr>
    <tr><td>检索 CLI</td><td>search_eval.py：按名称模糊 / 标签 / 日期区间 / 类型 / ID 检索，--latest --show 直出全文</td></tr>
    <tr><td>批量评估</td><td>因子路径 ~40s/个（含多周期谱+十分位+正交化），模型路径含回测+TC+盈亏平衡</td></tr>
    <tr><td>审计可追溯</td><td>每个数字可回溯到审计 JSON / 因子湖 parquet / DuckDB 只读查询，全链路无写锁</td></tr>
  </table>
</div>

<footer>
  口径附注：IC 为全市场 rank IC（h=20，后复权 LEAD 收益）；分组收益 = 20 日远期收益逐日摊薄（非可交易组合，终审须组合层 A/B）；FDR 基于 311 因子审计快照（BH + ρ=0.9 自相关折减），本窗实测 ρ=0.954 → t_adj=2.08，阈值对自相关假设敏感；正交化增量 = 对 size 逐日截面 OLS 残差 rank IC；多空腿以 A 股做空约束为前提解读。红绿灯仅初筛，入生产候选须组合层 A/B 终审。<br>
  数据源：QuantLab 因子湖 + DuckDB（只读）｜生成：2026-09-12｜评估引擎 scripts/factor_eval/run_eval.py（v3）
</footer>
</div>

<script>
const DATA = __DATA__;
document.getElementById('m_ic').textContent = (DATA.ic_pearson_mean>=0?'+':'')+DATA.ic_pearson_mean;
const q5=DATA.groups.Q5, ls=DATA.groups.LS;
document.getElementById('m_q5sh').textContent=q5.sharpe;
document.getElementById('m_q5dd').textContent=(q5.mdd*100).toFixed(1)+'%';

const C={q:'#9a9a9a',q1:'#5da9e0',q2:'#7bb8e8',q3:'#9a9a9a',q4:'#e0a35d',q5:'#e05d5d',ls:'#e6b455',ic:'#e05d5d',icir:'#55c4c4'};
function svgEl(w,h){const s=document.createElementNS('http://www.w3.org/2000/svg','svg');
  s.setAttribute('viewBox','0 0 '+w+' '+h);s.setAttribute('width','100%');return s}
function add(s,tag,attrs,txt){const e=document.createElementNS('http://www.w3.org/2000/svg',tag);
  for(const k in attrs)e.setAttribute(k,attrs[k]);if(txt!=null)e.textContent=txt;s.appendChild(e);return e}

/* ---- 折线图（decay / nav）---- */
function lineChart(el,xs,series,xlabels,fmt){
  const W=520,H=230,L=46,R=46,T=14,B=30,w=W-L-R,h=H-T-B;
  const s=svgEl(W,H);el.appendChild(s);
  const main=series.filter(p=>!p.axis2), sec=series.filter(p=>p.axis2);
  const ymin=Math.min(...main.flatMap(p=>p.data)),ymax=Math.max(...main.flatMap(p=>p.data));
  let ymin2=0,ymax2=1;
  if(sec.length){ymin2=Math.min(...sec.flatMap(p=>p.data));ymax2=Math.max(...sec.flatMap(p=>p.data));}
  const X=i=>L+(xs.length>1?i/(xs.length-1)*w:0);
  const Y=v=>T+h-(v-ymin)/(ymax-ymin)*h, Y2=v=>T+h-(v-ymin2)/(ymax2-ymin2)*h;
  for(let g=0;g<=4;g++){const v=ymin+(ymax-ymin)*g/4;
    add(s,'line',{x1:L,y1:Y(v),x2:W-R,y2:Y(v),stroke:'#333','stroke-width':1});
    add(s,'text',{x:L-5,y:Y(v)+3,'text-anchor':'end',fill:'#9a9a9a','font-size':9},fmt?fmt(v):v.toFixed(2));
    if(sec.length){const v2=ymin2+(ymax2-ymin2)*g/4;
      add(s,'text',{x:W-R+5,y:Y(v2)+3,fill:'#55c4c4','font-size':9},v2.toFixed(2));}}
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

/* ---- IC 衰减 ---- */
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
  const m=DATA.monthly_ic,mm=['','1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月'];
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
print("pearson IC_MEAN =", D["ic_pearson_mean"], "| Q5 sharpe =", D["groups"]["Q5"]["sharpe"])
