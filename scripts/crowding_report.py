"""拥挤度监控报告生成器：读 JSON → reports/crowding_monitor_20260906.html"""
import sys
sys.path.insert(0, '.')

import json

d = json.load(open("reports/crowding_monitor_20260906.json", encoding="utf-8"))

GROUP_NAME = {"core": "生产核心", "pricevol": "量价代表", "new": "新因子（本两批）"}
COLOR = {"size": "#1a3a5c", "amihud_20": "#2d5f8a",
         "momentum_20": "#7f8c9b", "reversal_5": "#95a5a6",
         "volatility_20": "#b0bec5", "price_position_250": "#8d6e63",
         "sue": "#c0392b", "overnight_mom_20": "#e67e22",
         "chip_vwap_bias_250": "#2e7d32"}


def pct(v, nd=0):
    return f"{v:.{nd}%}" if v is not None else "—"


def crowding_cell(c, hp):
    """拥挤度着色：历史分位 >80% 红（拥挤）、<20% 绿（干净）、其余中性"""
    if c is None:
        return "<td>—</td>"
    if hp is not None and hp >= 0.8:
        cls = "bad"
    elif hp is not None and hp <= 0.2:
        cls = "ok"
    else:
        cls = ""
    return f"<td class='{cls}'><b>{c:+.2f}</b></td>"


# ── 当前水平表 ──
rows = ["<tr><th>因子</th><th>组</th><th>最新拥挤度</th><th>历史分位</th>"
        "<th>估值价差 z（贡献取反）</th><th>配对相关 z</th><th>多头换手分位</th><th>采样点</th></tr>"]
for group in ("core", "pricevol", "new"):
    for n in d["pool"][group]:
        s = d["series"].get(n)
        lt = d["latest"].get(n)
        if not s or not lt:
            continue
        val_z = "—" if lt["val_z"] is None else f"{lt['val_z']:+.2f}"
        corr_z = "—" if lt["corr_z"] is None else f"{lt['corr_z']:+.2f}"
        rows.append(
            f"<tr><td><b style='color:{COLOR.get(n, '#333')}'>{n}</b></td>"
            f"<td>{GROUP_NAME[group]}</td>"
            f"{crowding_cell(lt['crowding'], lt['hist_pct'])}"
            f"<td>{pct(lt['hist_pct'])}</td>"
            f"<td>{val_z}</td>"
            f"<td>{corr_z}</td>"
            f"<td>{pct(lt['turnover_pct'])}</td>"
            f"<td>{len(s['dates'])}</td></tr>")
latest_table = "\n".join(rows)

chart_data = json.dumps({
    "series": d["series"],
    "gate": d["gate"],
    "color": COLOR,
    "pool": d["pool"],
}, ensure_ascii=False)

TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>因子拥挤度监控 v1 · 2026-09-06</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:"Microsoft YaHei","PingFang SC",sans-serif; background:#f5f6f8; color:#2c3e50; line-height:1.7; }
  .wrap { max-width:1100px; margin:0 auto; padding:24px 20px 60px; }
  header { background:linear-gradient(135deg,#1a3a5c 0%,#2d5f8a 100%); color:#fff; border-radius:12px; padding:28px 32px; margin-bottom:20px; }
  header h1 { font-size:22px; font-weight:600; margin-bottom:6px; }
  header .sub { font-size:13px; opacity:.85; }
  .tldr { background:#fff; border-radius:12px; padding:24px 28px; margin-bottom:20px; box-shadow:0 1px 4px rgba(0,0,0,.06); border-left:5px solid #2e7d32; }
  .tldr h2 { font-size:16px; color:#2e7d32; margin-bottom:10px; }
  .tldr li { margin:6px 0 6px 20px; font-size:14px; }
  .card { background:#fff; border-radius:12px; padding:24px 28px; margin-bottom:20px; box-shadow:0 1px 4px rgba(0,0,0,.06); }
  .card h2 { font-size:17px; color:#1a3a5c; border-bottom:2px solid #e8ecf0; padding-bottom:8px; margin-bottom:14px; }
  table { width:100%; border-collapse:collapse; font-size:13px; margin:10px 0; }
  th { background:#eef2f6; color:#1a3a5c; padding:8px 10px; text-align:left; font-weight:600; border:1px solid #dde3ea; }
  td { padding:7px 10px; border:1px solid #e5eaf0; vertical-align:top; }
  tr:nth-child(even) td { background:#fafbfc; }
  .ok { color:#2e7d32; font-weight:700; } .bad { color:#c0392b; font-weight:700; } .mid { color:#b8860b; font-weight:700; }
  .chart { width:100%; height:400px; margin:12px 0; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  @media (max-width:900px){ .grid2 { grid-template-columns:1fr; } }
  .footnote { font-size:11px; color:#8a97a5; margin-top:8px; }
  .warn { background:#fdf3f2; border-left:4px solid #c0392b; padding:12px 16px; border-radius:6px; font-size:13px; margin:10px 0; }
  .note { background:#f0f7fd; border-left:4px solid #2d5f8a; padding:12px 16px; border-radius:6px; font-size:13px; margin:10px 0; }
  .disclaim { font-size:11px; color:#95a5a6; border-top:1px solid #e5eaf0; padding-top:12px; margin-top:20px; }
  code { background:#eef2f6; padding:1px 5px; border-radius:3px; font-size:12px; }
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1>因子拥挤度监控 v1 —— 生产核心 / 量价 / 新因子九因子全景</h1>
  <div class="sub">2026-09-06 ｜ quantlab/factor/crowding.py（估值价差 + 配对相关性 + 换手分位 → 综合 z）｜ 每 5 交易日采样 ｜ 因果滚动归一（2 年窗口）｜ universe = ashare_ex</div>
</header>

<div class="tldr">
  <h2>结论（TL;DR）</h2>
  <ul>
    <li><b class="bad">SUE 高度拥挤：+1.20，自身历史 96% 分位</b>——与第一批研究已发现的"SUE 2026 IC 衰减（0.049→0.011）"互相印证。<b>模块首跑即捕获真实衰减信号</b>，方法论通过验证。SUE 时序显示拥挤自 2026-04 抬升（+0.79）、8 月再冲 +1.11：进入合成无碍（组合已分散），但 SUE_I 改进与密集监控的优先级实质提升。</li>
    <li><b class="ok">生产核心（size/amihud）当前不拥挤</b>：综合分位 24%（size 仅 5%）——2026-05/06 小盘暴跌已把拥挤洗掉（估值回落 + 缩量）。时序与事件对齐良好：<b>2023-10~2024-03 核心拥挤 92~98% 分位，恰是微盘股灾前夜</b>；2026-07 洗至 1% 分位。</li>
    <li><b>量价代表组普遍中性偏低</b>（momentum 38% / reversal 24% / volatility 22%），唯 <b class="mid">price_position_250 偏高（74%）</b>。外部情报"量价拥挤 92%"为 2026-07 回撤前口径——回撤后我们自测显示拥挤已大幅释放，这正是自建指标相对滞后研报情报的价值。</li>
    <li><b>条件融合门控当前读数偏保守</b>：w = 0.90（新因子权重仅 10%），因核心拥挤处于低位——门控逻辑"核心拥挤高位才切新因子"当前不触发。与固定 w=0.6 的 V3 相比，门控在当前时点自动选择了接近 PROD 的配置。</li>
    <li><b>v1 局限</b>：z/分位为 2 年因果滚动（SUE 仅 66 个采样点，2026-02 起有 z 值）；bp 为 finance_snapshot as-of 口径；监控是诊断信号，<b>不做动态 w 回测寻优</b>（避免在同一个样本上既造信号又验证信号）。</li>
  </ul>
</div>

<div class="card">
  <h2>一、方法论（海通"因子拥挤"系列的项目内实现）</h2>
  <table>
    <tr><th>指标</th><th>构造</th><th>拥挤方向</th></tr>
    <tr><td><b>估值价差</b></td><td>多头组 vs 空头组中位数 BP 之比取对数（方向调整后截面 top/bottom 20%，多头组 ≤100 只）；因果滚动 z</td><td>拥挤 → 多头被买贵 → BP 下降 → 价差收窄 → 贡献 = <b>−z(价差)</b></td></tr>
    <tr><td><b>配对相关性</b></td><td>多头组过去 60 日日收益两两相关均值</td><td>拥挤 → 持仓趋同同涨同跌 → 贡献 = <b>+z(相关性)</b></td></tr>
    <tr><td><b>换手分位</b></td><td>多头组换手率（turnover 因子中位数）的因果滚动分位（2 年窗口）</td><td>拥挤 → 异常放量 → 贡献 = <b>2·分位 − 1</b></td></tr>
    <tr><td><b>综合拥挤度</b></td><td colspan="2">三贡献均值（≥2 个非空才输出）；分位 = 当前值在自身历史中的位置</td></tr>
  </table>
  <div class="footnote">入口：<code>factor_crowding(name, direction, ...)</code>（已导出 quantlab.factor 包，支持 retmat 复用批量跑）。采样每 5 交易日，z/分位窗口 100 采样点（≈2 年）、最小 40。</div>
</div>

<div class="card">
  <h2>二、当前水平（最新采样：2026-09-04）</h2>
  __LATEST_TABLE__
  <div class="footnote">红 = 历史分位 &gt;80%（拥挤预警）；绿 = &lt;20%（干净）。估值价差 z 一列显示原始 z（对拥挤的贡献取其相反数）。</div>
</div>

<div class="card">
  <h2>三、拥挤度时序</h2>
  <div id="chart1" class="chart" style="height:380px;"></div>
  <div class="footnote">生产核心 + 量价代表（6 因子）：核心拥挤 2023Q4~2024Q1 冲顶（微盘股灾前夜）、2026-05 后随暴跌洗清；price_position_250 为量价组当前唯一偏高者。</div>
  <div id="chart2" class="chart" style="height:320px;"></div>
  <div class="footnote">新因子组（3 因子）：SUE 自 2026-04 起拥挤（与 IC 衰减同段）；overnight_mom 与 chip_vwap 中性。注意 SUE z 值自 2026-02 起（66 采样点，min_periods=40 的自然结果）。</div>
</div>

<div class="card">
  <h2>四、条件融合门控（示意，未回测）</h2>
  <div id="chart3" class="chart" style="height:360px;"></div>
  <table>
    <tr><th>要素</th><th>说明</th></tr>
    <tr><td>规则</td><td>核心拥挤度 = mean(size, amihud_20) 的综合拥挤度；pct = 其 2 年因果滚动分位；<b>w = 1 − 0.4·pct ∈ [0.6, 1.0]</b>（w 为核心权重，1−w 为新因子权重）</td></tr>
    <tr><td>当前读数</td><td class="ok">核心分位 24% → w = 0.90（新因子 10%）——核心不拥挤时门控自动贴近 PROD</td></tr>
    <tr><td>历史轨迹</td><td>2023-10~2024-03 核心分位 92~98% → w 最低 0.61（恰在微盘股灾前夜压缩核心暴露——机制方向的正面诊断信号）；2026-07 分位 1% → w≈1.0</td></tr>
    <tr><td><b>边界声明</b></td><td>本图为<b>规则示意而非回测结论</b>：动态 w 的完整回测需要门控与融合权重在同一样本上的联合验证，存在过拟合风险（用同一段历史既调规则又验证）。正确姿势：先纸面跟踪 w_t 序列，积累 3-6 个月前向样本后再评估</td></tr>
  </table>
</div>

<div class="card">
  <h2>五、局限与下一步</h2>
  <table>
    <tr><th>事项</th><th>说明</th></tr>
    <tr><td>归一化窗口</td><td>2 年因果滚动（100 采样点）：对慢变量稳健，但 regime 剧变后分位需要时间"忘掉"旧常态；SUE 样本仅 66 点，其分位解读置信度低于其余因子</td></tr>
    <tr><td>bp 口径</td><td>估值价差用 bp 因子（finance_snapshot as-of 回填），与库内 size/turnover 同源的温和未来数据，audit WARN 口径一致</td></tr>
    <tr><td>指标覆盖</td><td>海通系列还有"持仓集中度"（公募重仓占比）与"因子波动率"两维，v1 未实现（需基金持仓数据/因子 L-S 组合时序，后者可在 v1.1 从本模块的 pair_corr 顺带产出）</td></tr>
    <tr><td><b>下一步 1</b></td><td>接入每日流水线（数据更新 → 因子 → 拥挤度 → 信号），SUE 与 price_position_250 设 80% 分位预警线</td></tr>
    <tr><td><b>下一步 2</b></td><td>门控 w_t 序列进 tracker 纸面跟踪（与 V3 固定权重并行），3-6 个月后回看门控 vs 固定 w 的前向差异</td></tr>
    <tr><td><b>下一步 3</b></td><td>SUE_I 改进（三源合并）优先级提升——拥挤 96% 分位 + IC 衰减双重信号，改进时效性是正道</td></tr>
  </table>
  <div class="footnote">产出：quantlab/factor/crowding.py（模块，已导出）｜ scripts/crowding_monitor.py（跑批）｜ reports/crowding_monitor_20260906.json（全部时序+门控）</div>
  <div class="disclaim">免责声明：拥挤度为研究诊断指标，不构成投资建议。z 分数与分位基于历史分布，不代表未来拥挤水平；门控曲线为规则示意，未经过样本外验证。市场有风险，投资需谨慎。</div>
</div>

</div>

<script>
var D = __CHART_DATA__;

function lineSeries(names, height) {
  var s = [], legend = [];
  names.forEach(function(n){
    var ser = D.series[n];
    if (!ser) return;
    legend.push(n);
    s.push({ name: n, type: 'line', showSymbol: false,
             lineStyle: { width: (n==='size'||n==='sue')?2.4:1.4 },
             itemStyle: { color: D.color[n] || '#999' },
             connectNulls: false,
             data: ser.crowding });
  });
  return { legend: legend, series: s };
}

var c1 = echarts.init(document.getElementById('chart1'));
var g1 = lineSeries(D.pool.core.concat(D.pool.pricevol));
c1.setOption({
  tooltip: { trigger: 'axis' },
  legend: { data: g1.legend, top: 4 },
  grid: { left: 50, right: 15, top: 42, bottom: 55 },
  xAxis: { type: 'category', data: D.series['size'].dates, axisLabel: { rotate: 30, fontSize: 10 } },
  yAxis: { type: 'value', name: '综合拥挤度 z' },
  series: g1.series
});

var c2 = echarts.init(document.getElementById('chart2'));
var g2 = lineSeries(D.pool.new);
c2.setOption({
  tooltip: { trigger: 'axis' },
  legend: { data: g2.legend, top: 4 },
  grid: { left: 50, right: 15, top: 42, bottom: 55 },
  xAxis: { type: 'category', data: D.series['overnight_mom_20'].dates, axisLabel: { rotate: 30, fontSize: 10 } },
  yAxis: { type: 'value', name: '综合拥挤度 z' },
  series: g2.series
});

var c3 = echarts.init(document.getElementById('chart3'));
c3.setOption({
  tooltip: { trigger: 'axis' },
  legend: { data: ['核心拥挤度', '门控权重 w（右轴）'], top: 4 },
  grid: { left: 55, right: 55, top: 42, bottom: 55 },
  xAxis: { type: 'category', data: D.gate.dates, axisLabel: { rotate: 30, fontSize: 10 } },
  yAxis: [
    { type: 'value', name: '核心拥挤度', min: -2, max: 2 },
    { type: 'value', name: 'w', min: 0.5, max: 1.05, position: 'right',
      axisLabel: { formatter: function(v){ return v.toFixed(2); } } }
  ],
  series: [
    { name: '核心拥挤度', type: 'line', showSymbol: false,
      lineStyle: { width: 1.8, color: '#1a3a5c' }, itemStyle: { color: '#1a3a5c' },
      markLine: { symbol: 'none', silent: true, lineStyle: { color: '#c0392b', type: 'dashed' },
                  label: { formatter: '拥挤预警线' }, data: [{ yAxis: 1.0 }] },
      data: D.gate.core_crowding },
    { name: '门控权重 w（右轴）', type: 'line', yAxisIndex: 1, showSymbol: false,
      lineStyle: { width: 2.2, color: '#c0392b' }, itemStyle: { color: '#c0392b' },
      areaStyle: { color: 'rgba(192,57,43,0.08)' },
      data: D.gate.w }
  ]
});

window.addEventListener('resize', function(){ [c1,c2,c3].forEach(function(ch){ ch.resize(); }); });
</script>
</body>
</html>
"""

html = (TEMPLATE
        .replace("__LATEST_TABLE__", latest_table)
        .replace("__CHART_DATA__", chart_data))

with open("reports/crowding_monitor_20260906.html", "w", encoding="utf-8") as f:
    f.write(html)
print(f"written reports/crowding_monitor_20260906.html ({len(html)//1024} KB)")
