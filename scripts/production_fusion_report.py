"""生产融合实验报告生成器：读 JSON → reports/production_fusion_20260906.html"""
import sys
sys.path.insert(0, '.')

import json

d = json.load(open("reports/production_fusion_20260906.json", encoding="utf-8"))

TAGS = ("prod", "v1", "v2", "v3")
NAME = {"prod": "PROD（size+amihud，现行生产）", "v1": "V1（等权直加 4 因子）",
        "v2": "V2（池内精选：新因子精选）", "v3": "V3（0.6/0.4 加权融合）"}
COLOR = {"prod": "#1a3a5c", "v1": "#5b8db8", "v2": "#e67e22", "v3": "#c0392b"}


def pct(v, nd=1, sign=True):
    return f"{v:+.{nd}%}" if sign else f"{v:.{nd}%}"


def metrics_table(wname):
    rows = ["<tr><th>组合</th><th>年化</th><th>夏普</th><th>最大回撤</th>"
            "<th>Calmar</th><th>超额年化</th><th>IR</th><th>胜率</th>"
            "<th>Δ(vs PROD)</th></tr>"]
    base = d[wname]["prod"]["annual"]
    for t in TAGS:
        m = d[wname][t]
        delta = m["annual"] - base
        cls = "ok" if delta >= 0 else "bad"
        rows.append(
            f"<tr><td><b style='color:{COLOR[t]}'>{NAME[t]}</b></td>"
            f"<td>{pct(m['annual'])}</td><td>{m['sharpe']:.2f}</td>"
            f"<td>{pct(m['mdd'])}</td><td>{m['calmar']:.2f}</td>"
            f"<td>{pct(m['excess'])}</td><td>{m['ir']:+.2f}</td>"
            f"<td>{m['win']:.0%}</td><td class='{cls}'>{pct(delta)}</td></tr>")
    return "\n".join(rows)


def paired_table(wname):
    prod = d[wname]["prod"]["phase_detail"]
    rows = ["<tr><th>比较</th><th>相位胜率</th><th colspan='4'>逐相位 Δ 年化</th></tr>"]
    for t in ("v1", "v2", "v3"):
        ph = d[wname][t]["phase_detail"]
        wins = sum(1 for a, b in zip(ph, prod) if a["annual_return"] > b["annual_return"])
        cells = "".join(
            f"<td class='{'ok' if a['annual_return'] > b['annual_return'] else 'bad'}'>"
            f"{pct(a['annual_return'] - b['annual_return'])}</td>"
            for a, b in zip(ph, prod))
        rows.append(f"<tr><td><b style='color:{COLOR[t]}'>{t.upper()} vs PROD</b></td>"
                    f"<td><b>{wins}/4</b></td>{cells}</tr>")
    return "\n".join(rows)


def yearly_table(wname):
    years = sorted(d[wname]["prod"]["yearly"])
    rows = ["<tr><th>组合</th>" + "".join(f"<th>{y}</th>" for y in years) + "</tr>"]
    for t in TAGS:
        y = d[wname][t]["yearly"]
        rows.append(f"<tr><td><b style='color:{COLOR[t]}'>{NAME[t]}</b></td>" +
                    "".join(f"<td>{pct(y.get(yy, 0))}</td>" for yy in years) + "</tr>")
    return "\n".join(rows)


# 曲线（full 降采样）
step = 2
full_dates = d["full"]["prod"]["dates"][::step]
full_nav = {t: d["full"][t]["nav"][::step] for t in TAGS}
full_bm = d["full"]["prod"]["nav_bm"][::step]
focus_dates = d["focus"]["prod"]["dates"]
focus_nav = {t: d["focus"][t]["nav"] for t in TAGS}
focus_bm = d["focus"]["prod"]["nav_bm"]

chart_data = json.dumps({
    "full": {"dates": full_dates, "nav": full_nav, "bm": full_bm,
             "fd": focus_dates, "fnav": focus_nav, "fbm": focus_bm},
    "phases": {"labels": [f"相位{k+1}" for k in range(4)],
               "annual": {t: [round(p["annual_return"], 4) for p in d["focus"][t]["phase_detail"]]
                          for t in TAGS},
               "avg": {t: round(d["focus"][t]["annual"], 4) for t in TAGS}},
    "yearly": {"years": sorted(d["full"]["prod"]["yearly"]),
               "vals": {t: [round(d["full"][t]["yearly"].get(str(y) if str(y) in d["full"][t]["yearly"] else y, 0), 4)
                            for y in sorted(d["full"]["prod"]["yearly"])] for t in TAGS}},
    "color": COLOR,
}, ensure_ascii=False)

TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>生产融合实验 · size+amihud 核心接入新因子 · 2026-09-06</title>
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
  <h1>生产融合实验 —— size+amihud 核心如何接入 sue / 隔夜动量</h1>
  <div class="sub">2026-09-06 ｜ 新增 run_multiphase_backtest（4 相位平均）｜ top100 · 20日调仓 · 扣费 · 基准中证1000 ｜ 生产模型已于 09-05 回退为纯 size+amihud（dragon 前视修复移出）</div>
</header>

<div class="tldr">
  <h2>结论（TL;DR）</h2>
  <ul>
    <li><b>工程成果：多相位回测引擎上线</b>（<code>run_multiphase_backtest</code>）：4 个相位错开子组合等资金合成，把调仓网格运气（单相位年化波动 ±5pp）平均掉——本报告所有对比均为多相位口径。</li>
    <li><b class="ok">sue 生效后（2025-05~2026-08）：三种融合方式全部改善生产</b>。V2 池内精选 +5.8pp（年化 46.7%→52.5%，IR 0.60→0.78，回撤 −26.6%→−23.7%）；V3 加权融合 +5.3pp（IR 0.79，配对相位 3/4 胜）；V1 等权直加最弱（+2.0pp，2/4 胜——核心暴露稀释一半的代价）。</li>
    <li><b class="bad">全窗口（2022-07 起）：三种融合全部伤害（0/4 相位胜，−4.2~−5.5pp）</b>。伤害集中在 2023 小盘黄金年（PROD +45.1% vs V3 +37.8%）与 2024——size+amihud 核心在被稀释的年份是王者，"核心风格稀释"警示应验。</li>
    <li><b>本质：regime 押注而非免费午餐</b>。年度分解显示收益全部来自 2026（+3.0~+4.6pp），伤害全部来自 2022-2024；两段证据方向相反，样本外无法裁决 regime 归属。</li>
    <li><b>建议：生产暂不切换</b>。采纳「条件融合」路线：V3 权重 w 作为拥挤度门控旋钮（量价/核心拥挤高位 → 降 w 提升新因子权重），待拥挤度监控模块上线后回测动态 w 曲线；同时用 tracker 纸面并行跟踪 V3 积累前向样本。</li>
  </ul>
</div>

<div class="card">
  <h2>一、实验设计</h2>
  <table>
    <tr><th>组合</th><th>构造</th><th>检验的问题</th></tr>
    <tr><td><b style="color:#1a3a5c">PROD</b></td><td>rank(size) + rank(amihud_20) 等权（现行生产）</td><td>基线</td></tr>
    <tr><td><b style="color:#5b8db8">V1</b></td><td>四因子等权直加</td><td>最朴素接入——代价是核心暴露从 100% 稀释到 50%</td></tr>
    <tr><td><b style="color:#e67e22">V2</b></td><td>池内精选：size+amihud 选 400 只候选池 → sue+隔夜动量池内二次排序</td><td>保留池的风格约束，精选层换血</td></tr>
    <tr><td><b style="color:#c0392b">V3</b></td><td>0.6×rank(核心) + 0.4×rank(新因子)</td><td>加权折中——核心保留 60% 主导权</td></tr>
  </table>
  <div class="footnote">回测：run_multiphase_backtest（phases=4，相位间隔 5 交易日），top100、20 日调仓、扣费（单次完整换手 ~0.40%）、基准中证1000、universe=ashare_ex。窗口：focus=2025-05-01（sue 生效后干净对比）；full=2022-07-01（参考，含核心黄金年代）。多相位合并口径：公共区间各相位日收益等权平均（隐含相位间日度再平衡，成本二阶小量忽略，相位内成本全额计入）。</div>
</div>

<div class="card">
  <h2>二、focus 窗口（2025-05 ~ 2026-08）：融合全面占优</h2>
  <div id="chart1" class="chart"></div>
  __FOCUS_METRICS__
  __FOCUS_PAIRED__
  <div class="note">V2 与 V3 收益相近（52.5% / 52.0%），但 V3 的逐相位 Δ 更稳（−0.8~+6.9pp vs V2 的 −1.1~+11.7pp）且单相位区间更窄（[47.4, 53.2] vs [43.0, 54.8]）——加权融合的平滑性更好。V1 的两个负相位 Δ（−4.8/−5.8pp）证明等权直加在最不利相位下会输给纯核心。</div>
</div>

<div class="card">
  <h2>三、full 窗口（2022-07 ~ 2026-08）：稀释代价全员一致</h2>
  <div id="chart2" class="chart"></div>
  __FULL_METRICS__
  __FULL_PAIRED__
  __FULL_YEARLY__
  <div class="warn"><b>伤害定位</b>：2023（小盘+低流动性黄金年，PROD +45.1%，任何稀释都贵）与 2024（隔夜动量在池内重排为净负贡献）。2025 两边打平，2026 融合开始反超（PROD +13.9% vs V3 +17.5%）。<b>两段窗口给出相反裁决 = regime 依赖</b>：切换与否实质是押"小盘核心时代"还是"新因子时代"延续。</div>
</div>

<div class="card">
  <h2>四、多相位引擎：把网格运气变成可度量对象</h2>
  <div class="grid2">
    <div><div id="chart3" class="chart" style="height:340px;"></div>
    <div class="footnote">focus 窗口各变体单相位年化（柱）与 4 相位平均（虚线）。单相位间最大差 11.2pp（PROD）——上篇报告的相位敏感红旗在此量化。</div></div>
    <div><div id="chart4" class="chart" style="height:340px;"></div>
    <div class="footnote">full 窗口年度收益：2023 的核心黄金年与 2026 的融合反超一目了然。</div></div>
  </div>
  <table>
    <tr><th>引擎能力</th><th>说明</th></tr>
    <tr><td><code>run_multiphase_backtest(score, phases=4)</code></td><td>K 相位子组合（间隔 rebalance//K）等资金合成；返回各相位单独绩效 phase_detail，配对比较可做相位胜率检验</td></tr>
    <tr><td>治理落点</td><td>① 后续所有 A/B 回测默认多相位口径；② 生产实盘可按 4 相位各 1/4 资金分批调仓，直接消掉网格运气；③ 单相位离散度（本例 ±5pp）应作为策略脆弱性指标纳入监控</td></tr>
  </table>
</div>

<div class="card">
  <h2>五、结论与路线</h2>
  <table>
    <tr><th>路线</th><th>内容</th><th>适用条件</th></tr>
    <tr><td><b>保守（推荐立即执行）</b></td><td>生产维持 PROD 不变；V3 进入 tracker 纸面并行跟踪，积累 3-6 个月前向样本</td><td>两段窗口证据相反时的默认选择——不赌 regime</td></tr>
    <tr><td><b>条件融合（拥挤度门控）</b></td><td>V3 的 w 做成旋钮：核心因子拥挤度高位 → w 降（提升新因子权重）；拥挤度模块上线后回测动态 w 曲线验证</td><td>需要拥挤度监控模块（既定下一步，优先级最高）</td></tr>
    <tr><td><b>激进（不推荐）</b></td><td>直接切 V2/V3</td><td>仅当接受"2022-2024 式核心黄金年代不再"的强假设</td></tr>
  </table>
  <div class="footnote">产出：reports/production_fusion_20260906.json（指标+相位明细+曲线）｜ scripts/production_fusion_test.py ｜ quantlab/model/backtest.py 新增 run_multiphase_backtest（已导出 model 包）</div>
  <div class="disclaim">免责声明：以上内容基于本项目管线的历史数据回测，仅供量化研究参考，不构成投资建议。组合收益为扣费口径但未含冲击成本；focus 窗口样本仅 16 个月且含 regime 切换，统计功效有限。市场有风险，投资需谨慎。过往表现不预示未来收益。</div>
</div>

</div>

<script>
var D = __CHART_DATA__;

function navOption(dates, series, bm) {
  var names = { prod: 'PROD(生产)', v1: 'V1 等权直加', v2: 'V2 池内精选', v3: 'V3 加权融合' };
  var legend = [], s = [];
  ['prod','v1','v2','v3'].forEach(function(k){
    legend.push(names[k]);
    s.push({ name: names[k], type: 'line', showSymbol: false,
             lineStyle: { width: k==='prod'?2.5:(k==='v3'?2.2:1.3) },
             itemStyle: { color: D.color[k] }, data: series[k] });
  });
  legend.push('中证1000');
  s.push({ name: '中证1000', type: 'line', showSymbol: false,
           lineStyle: { width: 1, type: 'dashed', color: '#8a97a5' },
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
c1.setOption(navOption(D.full.fd, D.full.fnav, D.full.fbm));

var c2 = echarts.init(document.getElementById('chart2'));
c2.setOption(navOption(D.full.dates, D.full.nav, D.full.bm));

var c3 = echarts.init(document.getElementById('chart3'));
var names3 = { prod: 'PROD', v1: 'V1', v2: 'V2', v3: 'V3' };
c3.setOption({
  tooltip: { trigger: 'axis' },
  legend: { top: 4 },
  grid: { left: 55, right: 10, top: 42, bottom: 32 },
  xAxis: { type: 'category', data: D.phases.labels },
  yAxis: { type: 'value', name: '年化 %', min: 30 },
  series: ['prod','v1','v2','v3'].map(function(k){
    return { name: names3[k], type: 'bar', itemStyle: { color: D.color[k] },
             markLine: { symbol: 'none', lineStyle: { color: D.color[k], type: 'dashed' },
                         label: { formatter: '均值' },
                         data: [{ yAxis: (D.phases.avg[k]*100).toFixed(1) }] },
             data: D.phases.annual[k].map(function(v){ return (v*100).toFixed(1); }) };
  })
});

var c4 = echarts.init(document.getElementById('chart4'));
c4.setOption({
  tooltip: { trigger: 'axis' },
  legend: { top: 4 },
  grid: { left: 55, right: 10, top: 42, bottom: 32 },
  xAxis: { type: 'category', data: D.yearly.years.map(String) },
  yAxis: { type: 'value', name: '年度收益 %' },
  series: ['prod','v1','v2','v3'].map(function(k){
    return { name: names3[k], type: 'bar', itemStyle: { color: D.color[k] },
             data: D.yearly.vals[k].map(function(v){ return (v*100).toFixed(1); }) };
  })
});

window.addEventListener('resize', function(){ [c1,c2,c3,c4].forEach(function(ch){ ch.resize(); }); });
</script>
</body>
</html>
"""

html = (TEMPLATE
        .replace("__FOCUS_METRICS__", metrics_table("focus"))
        .replace("__FOCUS_PAIRED__", paired_table("focus"))
        .replace("__FULL_METRICS__", metrics_table("full"))
        .replace("__FULL_PAIRED__", paired_table("full"))
        .replace("__FULL_YEARLY__", yearly_table("full"))
        .replace("__CHART_DATA__", chart_data))

with open("reports/production_fusion_20260906.html", "w", encoding="utf-8") as f:
    f.write(html)
print(f"written reports/production_fusion_20260906.html ({len(html)//1024} KB)")
