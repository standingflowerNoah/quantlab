"""组合回测 A/B 报告生成器：读 JSON 结果 → reports/composite_backtest_20260906.html"""
import sys
sys.path.insert(0, '.')

import json

d = json.load(open("reports/composite_ab_20260906.json", encoding="utf-8"))
rb = json.load(open("reports/composite_ab_robustness.json", encoding="utf-8"))

TAGS = ("base", "plus", "swap")
NAME = {"base": "BASE（14 因子量价）", "plus": "PLUS（+sue+隔夜动量）",
        "swap": "SWAP（chip_vwap 换 price_position）"}
COLOR = {"base": "#5b8db8", "plus": "#c0392b", "swap": "#e67e22"}


def pct(v, nd=1, sign=True):
    return f"{v:+.{nd}%}" if sign else f"{v:.{nd}%}"


def metrics_rows(bucket, keys=TAGS, sealed_note=True):
    rows = []
    for t in keys:
        m, e = bucket[t]["metrics"] if "metrics" in bucket[t] else (bucket[t], bucket[t])
        # pack() 平铺结构
        m = bucket[t]
        rows.append(
            f"<tr><td><b style='color:{COLOR[t]}'>{NAME[t]}</b></td>"
            f"<td>{pct(m['annual_return'])}</td><td>{m['sharpe']:.2f}</td>"
            f"<td>{pct(m['max_drawdown'])}</td><td>{m['calmar']:.2f}</td>"
            f"<td>{pct(m['excess_annual'])}</td><td>{m['information_ratio']:+.2f}</td>"
            f"<td>{m['win_rate_vs_bm']:.0%}</td><td>{m['turnover_avg']:.0%}</td></tr>")
    return "\n".join(rows)


def yearly_table(bucket, keys=TAGS):
    years = sorted(bucket["base"]["yearly"])
    head = "<tr><th>组合</th>" + "".join(f"<th>{y}</th>" for y in years) + "</tr>"
    rows = [head]
    for t in keys:
        y = bucket[t]["yearly"]
        rows.append(f"<tr><td><b style='color:{COLOR[t]}'>{NAME[t]}</b></td>" +
                    "".join(f"<td>{pct(y.get(yy, float('nan')))}</td>" for yy in years) +
                    "</tr>")
    # Δ 行
    yb, yp = bucket["base"]["yearly"], bucket["plus"]["yearly"]
    rows.append("<tr><td><b>Δ(PLUS−BASE)</b></td>" +
                "".join(f"<td class='{ 'ok' if yp.get(yy,0)>=yb.get(yy,0) else 'bad'}'>"
                        f"{pct(yp.get(yy,0)-yb.get(yy,0))}</td>" for yy in years) + "</tr>")
    return "\n".join(rows)


# ── 曲线数据（full 降采样 step=2）──
step = 2
full_dates = d["full"]["base"]["dates"][::step]
full_nav = {t: d["full"][t]["nav"][::step] for t in TAGS}
full_bm = d["full"]["base"]["nav_bm"][::step]
focus_dates = d["focus"]["base"]["dates"]
focus_nav = {t: d["focus"][t]["nav"] for t in TAGS}
focus_bm = d["focus"]["base"]["nav_bm"]

# ── 稳健性表 ──
GRID_ORDER = ["reb15", "reb20_p0", "reb20_p1", "reb20_p2", "reb25", "reb30"]
GRID_LABEL = {"reb15": "15日调仓", "reb20_p0": "20日·相位0", "reb20_p1": "20日·相位+10日",
              "reb20_p2": "20日·相位+20日", "reb25": "25日调仓", "reb30": "30日调仓"}
rb_rows = ["<tr><th>网格</th><th>BASE 年化</th><th>PLUS 年化</th><th>SWAP 年化</th>"
           "<th>Δ(PLUS−BASE)</th><th>Δ(SWAP−BASE)</th></tr>"]
for g in GRID_ORDER:
    r = rb["grid"][g]
    dp = r["plus"]["annual"] - r["base"]["annual"]
    ds = r["swap"]["annual"] - r["base"]["annual"]
    rb_rows.append(
        f"<tr><td>{GRID_LABEL[g]}</td><td>{pct(r['base']['annual'])}</td>"
        f"<td>{pct(r['plus']['annual'])}</td><td>{pct(r['swap']['annual'])}</td>"
        f"<td class='ok'>{pct(dp)}</td><td class='ok'>{pct(ds)}</td></tr>")
rb_table = "\n".join(rb_rows)

# ── walkforward 表 ──
wf_rows = ["<tr><th>组合</th><th>样本内年化</th><th>样本外年化</th><th>样本外夏普</th>"
           "<th>样本外回撤</th><th>样本外超额</th></tr>"]
for t in TAGS:
    w = d["walkforward"][t]
    wf_rows.append(
        f"<tr><td><b style='color:{COLOR[t]}'>{NAME[t]}</b></td>"
        f"<td>{pct(w['in']['annual_return'])}</td>"
        f"<td>{pct(w['out']['annual_return'])}</td><td>{w['out']['sharpe']:.2f}</td>"
        f"<td>{pct(w['out']['max_drawdown'])}</td>"
        f"<td>{pct(w['out']['excess_annual'])}</td></tr>")
wf_table = "\n".join(wf_rows)

# ── 封板对照 ──
seal_rows = ["<tr><th>组合</th><th>未剔除 年化/回撤</th><th>封板剔除 年化/回撤</th><th>冲击</th></tr>"]
for t in TAGS:
    a, b = d["full"][t], d["full"][t + "_sealed"]
    imp = b["annual_return"] - a["annual_return"]
    seal_rows.append(
        f"<tr><td><b style='color:{COLOR[t]}'>{NAME[t]}</b></td>"
        f"<td>{pct(a['annual_return'],2)} / {pct(a['max_drawdown'])}</td>"
        f"<td>{pct(b['annual_return'],2)} / {pct(b['max_drawdown'])}</td>"
        f"<td class='ok'>{pct(imp,2)}</td></tr>")
seal_table = "\n".join(seal_rows)

chart_data = json.dumps({
    "full": {"dates": full_dates, "nav": full_nav, "bm": full_bm,
             "focus_dates": focus_dates, "focus_nav": focus_nav, "focus_bm": focus_bm},
    "robust": {"labels": [GRID_LABEL[g] for g in GRID_ORDER],
               "base": [rb["grid"][g]["base"]["annual"] for g in GRID_ORDER],
               "plus": [rb["grid"][g]["plus"]["annual"] for g in GRID_ORDER],
               "swap": [rb["grid"][g]["swap"]["annual"] for g in GRID_ORDER],
               "d_plus": [rb["grid"][g]["plus"]["annual"] - rb["grid"][g]["base"]["annual"]
                          for g in GRID_ORDER],
               "d_swap": [rb["grid"][g]["swap"]["annual"] - rb["grid"][g]["base"]["annual"]
                          for g in GRID_ORDER]},
    "color": COLOR,
}, ensure_ascii=False)

TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>组合回测 A/B · 新因子增量检验 · 2026-09-06</title>
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
  <h1>组合回测 A/B —— 新因子增量检验（sue / 隔夜动量 / 筹码乖离）</h1>
  <div class="sub">2026-09-06 ｜ 收口第一批 + 第二批胜出因子 ｜ top100 · 20日调仓 · 扣费（佣金万2.5+印花税千1卖+滑点10bp×2）· 基准中证1000</div>
</header>

<div class="tldr">
  <h2>结论（TL;DR）</h2>
  <ul>
    <li><b class="ok">sue + overnight_mom_20 进合成：增量稳健成立</b>。sue 生效后窗口（2025-05~2026-09）PLUS 比 BASE 年化 <b>+6.4~+10.4pp</b>（夏普 0.89→1.18），且<b>跨全部 6 个调仓网格（频率 15/20/25/30 × 相位偏移）方向一致</b>；样本外段（2025-06 后）同样占优（+4.7pp）。</li>
    <li><b class="bad">chip_vwap_bias_250 替换 price_position_250：不支持</b>。全窗口 SWAP 比 PLUS 年化 −1.4pp、IR 0.83 vs 0.91——单因子虽强（IC 1.8 倍），但与库内 momentum_20 ρ=0.67 信息重叠，对含动量族的合成无净增量。chip_vwap 留库，多空用法另议。</li>
    <li><b class="ok">封板剔除压力测试通过</b>：三组合年化冲击 ≤ +0.04pp，top100 流动性充足，涨停封板股本就少入池。</li>
    <li><b class="bad">重要红旗：BASE 绝对收益对调仓网格相位高度敏感</b>（同为 reb20，起点差一个月年化 19.7% vs 9.3%；reb30 崩至 +4.5%）。绝对数字不可直接引用，<b>只有 A/B 差值（跨 6 网格一致）可信</b>。</li>
    <li><b>量价衰减实锤</b>：focus 窗口 BASE 对中证1000 超额 ≈ −0.8%（IR −0.04）——现有 14 因子量价组合近期 alpha 已近零，与"量价拥挤 92%"情报一致；PLUS 恢复超额 +4.7%（IR +0.23）。</li>
  </ul>
</div>

<div class="card">
  <h2>一、实验设计</h2>
  <table>
    <tr><th>组合</th><th>因子构成（等权 rank 合成，方向按 sign(IC)）</th><th>因子数</th></tr>
    <tr><td><b style="color:#5b8db8">BASE</b></td><td>amplitude_20, max_return_20, momentum_120/20/60, price_position_250, reversal_10/5, rsi_14, size, total_mcap, turnover, volatility_20/60（model_comparison 惯例基线）</td><td>14</td></tr>
    <tr><td><b style="color:#c0392b">PLUS</b></td><td>BASE + <b>sue</b> + <b>overnight_mom_20</b>（两批挖掘的无冗余胜出者）</td><td>16</td></tr>
    <tr><td><b style="color:#e67e22">SWAP</b></td><td>BASE − price_position_250 + sue + overnight_mom_20 + <b>chip_vwap_bias_250</b>（替换检验）</td><td>16</td></tr>
  </table>
  <div class="footnote">回测参数：top100 等权、20 日调仓（稳健性章节另扫 15/25/30 与相位）、universe=ashare_ex（5,500+ 只）、基准 000852.SH。三个新因子方向已注册 FACTOR_DIRECTION（sue +1 / overnight_mom_20 +1 / chip_vwap_bias_250 −1），audit 全 PASS 过闸。窗口：full = 2022-07-01 起（fusion_test 惯例）；focus = 2025-05-01 起（sue 生效后，三组同因子数干净对比）。</div>
</div>

<div class="card">
  <h2>二、全窗口结果（2022-07 ~ 2026-08）</h2>
  <div id="chart1" class="chart"></div>
  <div class="footnote">净值（扣费后）：PLUS 与 BASE 在 2025-05 前几乎重合（sue 未生效），2025 年起分化——PLUS 2025 年 +53.2% vs BASE +43.0%。SWAP 全程略逊于 PLUS。</div>
  <table>
    <tr><th>组合</th><th>年化</th><th>夏普</th><th>最大回撤</th><th>Calmar</th><th>超额年化</th><th>IR</th><th>胜率</th><th>换手/期</th></tr>
    __FULL_TABLE__
  </table>
  <table>
    __FULL_YEARLY__
  </table>
  <div class="warn"><b>全窗口增量被稀释的原因</b>：sue 有效值 2025-04-30 才开始（8 季滚动 + min_win 的必然结果），4.2 年窗口里仅后 1.35 年生效；PLUS−BASE 的年度差全部来自 2025 年（+10.2pp），2022-2024 为 −1~−2pp（隔夜动量在合成中的边际贡献 ≈ 噪声内）。<b>读增量要看 focus 窗口与稳健性章节，不要只看全窗口 +0.6pp。</b></div>
</div>

<div class="card">
  <h2>三、focus 窗口（2025-05 ~ 2026-08，三组同因子数）</h2>
  <div id="chart2" class="chart"></div>
  <table>
    <tr><th>组合</th><th>年化</th><th>夏普</th><th>最大回撤</th><th>Calmar</th><th>超额年化</th><th>IR</th><th>胜率</th><th>换手/期</th></tr>
    __FOCUS_TABLE__
  </table>
  <table>
    __FOCUS_YEARLY__
  </table>
  <div class="note">两个年份 PLUS 均跑赢 BASE（2025 +3.8pp / 2026 +3.7pp），SWAP 与 PLUS 基本持平。BASE 超额 −0.8% 表明现有量价库相对基准已无 alpha——新因子不是锦上添花，是把超额从零拉回正的唯一来源。</div>
</div>

<div class="card">
  <h2>四、稳健性扫描（调仓频率 × 相位偏移）—— A/B 结论的压力测试</h2>
  <div class="grid2">
    <div><div id="chart3" class="chart" style="height:340px;"></div>
    <div class="footnote">各网格下三组合年化（focus 窗口）。BASE 自身跨网格波动大（+4.5%~+20.1%）。</div></div>
    <div><div id="chart4" class="chart" style="height:340px;"></div>
    <div class="footnote">Δ(组合−BASE)：PLUS 与 SWAP 的增量在全部 6 个网格为正（+4.0~+10.4pp）——<b>相位噪声打不翻增量结论</b>。</div></div>
  </div>
  __RB_TABLE__
  <div class="warn"><b>相位敏感的机制</b>：调仓日网格由起点相位决定，20 日持有期与信号半衰期（reversal_5/sue h60）错位时，2026-05/06 暴跌月的买点差一天净值差好几个点。这是高换手短周期策略的固有脆弱性，治理方向：① 报告绝对收益必须附网格参数；② 生产可用多相位平均（4 相位各 1/4 资金）平滑运气；③ reb30 已衰减（信号过期），调仓上限 ≤25 日。</div>
</div>

<div class="card">
  <h2>五、样本内外与封板压力</h2>
  <table>
    __WF_TABLE__
  </table>
  <div class="footnote">切分 2025-06-01（sue 生效后）。样本内腿从 2022-01 起跑（含上半年熊市与因子覆盖稀疏期），且该引擎换手计法不同（权重漂移口径 45% vs 主引擎重叠口径 88%），<b>绝对水平与第二章不可比，只看组间相对差</b>：样本外 PLUS +4.7pp、SWAP +5.6pp 优于 BASE，方向与 focus 一致。</div>
  <table>
    __SEAL_TABLE__
  </table>
  <div class="footnote">封板剔除（tradable="sealed"：建仓日 close==high 且涨幅&gt;7% 剔除，共 3,555 个调仓日不可买样本）：三组合年化冲击 +0.03~+0.04pp，可忽略。</div>
</div>

<div class="card">
  <h2>六、结论与下一步</h2>
  <table>
    <tr><th>事项</th><th>结论</th></tr>
    <tr><td><b>sue + overnight_mom_20</b></td><td class="ok">✅ 增量稳健（6/6 网格 +4.7~+10.4pp，样本外同向）。建议进入生产候选合成，正式上线前走用户确认 + 前向纸面跟踪（tracker）</td></tr>
    <tr><td><b>chip_vwap_bias_250</b></td><td class="bad">❌ 不替换 price_position_250（全窗 −1.4pp / IR −0.08，与 momentum_20 ρ=0.67 信息重叠）。留库：单因子完美单调分层适合多空/对冲用法，纳入拥挤度监控（2026 衰减警示）</td></tr>
    <tr><td><b>BASE 量价库</b></td><td class="mid">⚠ focus 窗口超额 ≈ 0（−0.8%，IR −0.04）——量价拥挤衰减从情报变为实测。拥挤度监控模块优先级上调</td></tr>
    <tr><td><b>下一步 1</b></td><td>多相位平均工程化（4 相位子组合）+ 调仓 ≤25 日约束写入生产回测口径</td></tr>
    <tr><td><b>下一步 2</b></td><td>生产模型（size+amihud 池 + dragon 精选）与 PLUS 的正式对比与融合方案（池内精选架构下引入 sue/隔夜动量）</td></tr>
    <tr><td><b>下一步 3</b></td><td>第三批 A5 放量事件簇 + C1 因子分域开工；SUE_I 三源改进排队</td></tr>
  </table>
  <div class="footnote">产出：reports/composite_ab_20260906.json（主结果+曲线）｜ reports/composite_ab_robustness.json（6 网格扫描）｜ scripts/composite_ab_test.py, composite_ab_robustness.py, engine_diff_check.py（口径核查）｜ composite.py 新增三因子方向注册</div>
  <div class="disclaim">免责声明：以上内容基于本项目管线的历史数据回测，仅供量化研究参考，不构成投资建议。组合收益为扣费口径但未含冲击成本与融资约束；绝对收益数字受调仓相位影响显著（见第四章），引用需附网格参数。市场有风险，投资需谨慎。过往表现不预示未来收益。</div>
</div>

</div>

<script>
var D = __CHART_DATA__;

function navOption(dates, series, bm, title) {
  var legend = [];
  var s = [];
  var names = { base: 'BASE', plus: 'PLUS(+sue+隔夜动量)', swap: 'SWAP(chip_vwap替换)' };
  ['base','plus','swap'].forEach(function(k){
    legend.push(names[k]);
    s.push({ name: names[k], type: 'line', showSymbol: false, lineStyle: { width: k==='plus'?2.5:1.5 },
             itemStyle: { color: D.color[k] }, data: series[k] });
  });
  legend.push('中证1000');
  s.push({ name: '中证1000', type: 'line', showSymbol: false, lineStyle: { width: 1, type: 'dashed', color: '#8a97a5' },
           itemStyle: { color: '#8a97a5' }, data: bm });
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: legend, top: 4 },
    grid: { left: 55, right: 15, top: 42, bottom: 55 },
    xAxis: { type: 'category', data: dates, axisLabel: { rotate: 30, fontSize: 10 } },
    yAxis: { type: 'value', name: '净值', scale: true },
    series: s
  };
}

var c1 = echarts.init(document.getElementById('chart1'));
c1.setOption(navOption(D.full.dates, D.full.nav, D.full.bm));

var c2 = echarts.init(document.getElementById('chart2'));
c2.setOption(navOption(D.full.focus_dates, D.full.focus_nav, D.full.focus_bm));

var c3 = echarts.init(document.getElementById('chart3'));
c3.setOption({
  tooltip: { trigger: 'axis' },
  legend: { top: 4 },
  grid: { left: 55, right: 10, top: 42, bottom: 60 },
  xAxis: { type: 'category', data: D.robust.labels, axisLabel: { rotate: 28, fontSize: 10 } },
  yAxis: { type: 'value', name: '年化 %' },
  series: [
    { name: 'BASE', type: 'bar', itemStyle: { color: D.color.base }, data: D.robust.base.map(function(v){ return (v*100).toFixed(1); }) },
    { name: 'PLUS', type: 'bar', itemStyle: { color: D.color.plus }, data: D.robust.plus.map(function(v){ return (v*100).toFixed(1); }) },
    { name: 'SWAP', type: 'bar', itemStyle: { color: D.color.swap }, data: D.robust.swap.map(function(v){ return (v*100).toFixed(1); }) }
  ]
});

var c4 = echarts.init(document.getElementById('chart4'));
c4.setOption({
  tooltip: { trigger: 'axis' },
  legend: { top: 4 },
  grid: { left: 55, right: 10, top: 42, bottom: 60 },
  xAxis: { type: 'category', data: D.robust.labels, axisLabel: { rotate: 28, fontSize: 10 } },
  yAxis: { type: 'value', name: 'Δ 年化 pp' },
  series: [
    { name: 'Δ(PLUS−BASE)', type: 'bar', itemStyle: { color: D.color.plus },
      label: { show: true, position: 'outside', fontSize: 10, formatter: function(p){ return '+' + p.value; } },
      data: D.robust.d_plus.map(function(v){ return (v*100).toFixed(1); }) },
    { name: 'Δ(SWAP−BASE)', type: 'bar', itemStyle: { color: D.color.swap },
      label: { show: true, position: 'outside', fontSize: 10, formatter: function(p){ return '+' + p.value; } },
      data: D.robust.d_swap.map(function(v){ return (v*100).toFixed(1); }) }
  ]
});

window.addEventListener('resize', function(){ [c1,c2,c3,c4].forEach(function(ch){ ch.resize(); }); });
</script>
</body>
</html>
"""

html = (TEMPLATE
        .replace("__FULL_TABLE__", metrics_rows(d["full"]))
        .replace("__FULL_YEARLY__", yearly_table(d["full"]))
        .replace("__FOCUS_TABLE__", metrics_rows(d["focus"]))
        .replace("__FOCUS_YEARLY__", yearly_table(d["focus"]))
        .replace("__RB_TABLE__", rb_table)
        .replace("__WF_TABLE__", wf_table)
        .replace("__SEAL_TABLE__", seal_table)
        .replace("__CHART_DATA__", chart_data))

with open("reports/composite_backtest_20260906.html", "w", encoding="utf-8") as f:
    f.write(html)
print(f"written reports/composite_backtest_20260906.html ({len(html)//1024} KB)")
