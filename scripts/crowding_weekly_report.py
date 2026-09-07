"""因子拥挤度/估值 周度诊断报告生成器（数据驱动，无硬编码叙事）
====================================================================
输入（均为每日流水线已产出的湖文件，只读）：
- data/lake/crowding/history.parquet  10 因子指标全时序（5 交易日采样）
- data/lake/crowding/latest.json      最新截面 + 预警 + 门控 w

输出：
- reports/crowding_weekly_<YYYYMMDD>.html

内容：
1. TL;DR：预警因子、周环比最大变动、门控 w 当前读数
2. 当前水平表（含周环比 Δ）
3. 综合拥挤度时序（core+pricevol / new 两组）
4. 估值价差 val_z 时序（全部因子，legend 可关）
5. 核心拥挤度 + 门控权重 w（诊断参考，batch13 已证动态门控无正增量）
6. 应用定位（batch14 实证结论，静态章节）

用法：
    python scripts/crowding_weekly_report.py            # 生成报告
    python scripts/crowding_weekly_report.py --stdout-name   # 仅打印输出文件名
"""
from __future__ import annotations

import sys
sys.path.insert(0, ".")

import json
from datetime import date as _date
from pathlib import Path

import numpy as np
import pandas as pd

LAKE = Path("data/lake/crowding")
GROUP_NAME = {"core": "生产核心", "pricevol": "量价代表", "new": "新因子"}
GROUP_ORDER = ["core", "pricevol", "new"]

COLOR = {
    "size": "#1a3a5c", "amihud_20": "#2d5f8a",
    "momentum_20": "#7f8c9b", "reversal_5": "#95a5a6",
    "volatility_20": "#b0bec5", "price_position_250": "#8d6e63",
    "sue": "#c0392b", "sue_i": "#e74c3c", "overnight_mom_20": "#e67e22",
    "chip_vwap_bias_250": "#2e7d32",
}


def cls_for(hp):
    if hp is None:
        return ""
    return "bad" if hp >= 0.8 else ("ok" if hp <= 0.2 else "")


def sign_class(v):
    if v is None:
        return ""
    return "bad" if v >= 0.3 else ("ok" if v <= -0.3 else "mid")


def main(write: bool = True) -> str:
    hist = pd.read_parquet(LAKE / "history.parquet")
    hist["date"] = pd.to_datetime(hist["date"])
    with open(LAKE / "latest.json", encoding="utf-8") as f:
        latest = json.load(f)

    pool = latest.get("pool", {})
    alerts = latest.get("alerts", [])
    gate = latest.get("gate", {})
    w_cur = gate.get("w_current")
    pct_cur = gate.get("pct_current")
    gen_at = latest.get("generated_at", "")[:10]

    # ── 周环比：每因子最新两个采样点 ────────────────────────────
    delta = {}
    for n, g in hist.groupby("factor"):
        c = g.dropna(subset=["crowding"]).sort_values("date")["crowding"]
        if len(c) >= 2:
            delta[n] = float(c.iloc[-1] - c.iloc[-2])
    movers = sorted(delta.items(), key=lambda kv: abs(kv[1]), reverse=True)

    # ── TL;DR 列表 ─────────────────────────────────────────────
    tldr = []
    if alerts:
        tldr.append(f"<b class='bad'>拥挤预警：{'、'.join(alerts)}</b>"
                    f"（综合拥挤度历史分位 ≥ {latest.get('alert_pct', 0.8):.0%}）——"
                    "按 batch14 定位，此为<b>关注/归因信号</b>，不是减仓指令。")
    else:
        tldr.append("<b class='ok'>本周无拥挤预警</b>：全部监控因子综合拥挤度"
                    "历史分位低于 80% 预警线。")
    if movers:
        n, v = movers[0]
        direction = "上升" if v > 0 else "回落"
        tldr.append(f"周环比变动最大：<b>{n}</b>（{v:+.2f}，{direction}）；"
                    f"前五：{'、'.join(f'{k} {vv:+.2f}' for k, vv in movers[:5])}。")
    if w_cur is not None:
        tldr.append(f"核心（size+amihud）拥挤度因果分位 {pct_cur:.0%} → 门控读数 "
                    f"w = {w_cur:.2f}（诊断参考——batch13 回测已证动态门控无正增量，"
                    "生产不采用）。")
    tldr.append("数据截至 <b>" + str(
        max(latest["latest"][n]["date"] for n in latest["latest"])) +
        "</b>（最新采样日），报告生成于 " + str(_date.today()) + "。")

    # ── 当前水平表 ─────────────────────────────────────────────
    rows = ["<tr><th>因子</th><th>组</th><th>最新采样日</th><th>综合拥挤度</th>"
            "<th>周环比</th><th>历史分位</th><th>估值价差 z</th>"
            "<th>配对相关 z</th><th>换手分位</th></tr>"]
    for grp in GROUP_ORDER:
        for n in pool.get(grp, []):
            lt = latest["latest"].get(n)
            if not lt:
                continue
            d = delta.get(n)
            dz = "—" if d is None else f"{d:+.2f}"
            vz = "—" if lt["val_z"] is None else f"{lt['val_z']:+.2f}"
            cz = "—" if lt["corr_z"] is None else f"{lt['corr_z']:+.2f}"
            vz_cls = "" if lt["val_z"] is None else sign_class(-lt["val_z"])
            c_cls = cls_for(lt["crowding"])
            c_cell = (f"<td class='{c_cls}'><b>{lt['crowding']:+.2f}</b></td>"
                      if c_cls else f"<td>{lt['crowding']:+.2f}</td>")
            rows.append(
                f"<tr><td><b style='color:{COLOR.get(n, '#333')}'>{n}</b></td>"
                f"<td>{GROUP_NAME[grp]}</td><td>{lt['date']}</td>"
                f"{c_cell}"
                f"<td>{dz}</td><td>{lt['hist_pct']:.0%}</td>"
                f"<td class='{vz_cls}'>{vz}</td><td>{cz}</td>"
                f"<td>{lt['turnover_pct']:.0%}</td></tr>")
    latest_table = "\n".join(rows)

    # ── 图表数据 ───────────────────────────────────────────────
    series = {}
    for n, g in hist.groupby("factor"):
        g = g.sort_values("date")
        series[n] = {
            "dates": [str(pd.Timestamp(d).date()) for d in g["date"]],
            "crowding": [None if pd.isna(v) else round(float(v), 3)
                         for v in g["crowding"]],
            "val_z": [None if pd.isna(v) else round(float(v), 3)
                      for v in g["val_z"]],
        }

    chart_data = json.dumps({
        "series": series, "gate": gate, "color": COLOR, "pool": pool,
    }, ensure_ascii=False)

    names_all = [n for grp in GROUP_ORDER for n in pool.get(grp, [])]

    TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>因子拥挤度/估值 周度诊断 · __TITLE_DATE__</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:"Microsoft YaHei","PingFang SC",sans-serif; background:#f5f6f8; color:#2c3e50; line-height:1.7; }
  .wrap { max-width:1100px; margin:0 auto; padding:24px 20px 60px; }
  header { background:linear-gradient(135deg,#1a3a5c 0%,#2d5f8a 100%); color:#fff; border-radius:12px; padding:26px 32px; margin-bottom:20px; }
  header h1 { font-size:21px; font-weight:600; margin-bottom:6px; }
  header .sub { font-size:13px; opacity:.85; }
  .tldr { background:#fff; border-radius:12px; padding:22px 28px; margin-bottom:20px; box-shadow:0 1px 4px rgba(0,0,0,.06); border-left:5px solid #2d5f8a; }
  .tldr h2 { font-size:16px; color:#1a3a5c; margin-bottom:10px; }
  .tldr li { margin:6px 0 6px 20px; font-size:14px; }
  .card { background:#fff; border-radius:12px; padding:22px 28px; margin-bottom:20px; box-shadow:0 1px 4px rgba(0,0,0,.06); }
  .card h2 { font-size:17px; color:#1a3a5c; border-bottom:2px solid #e8ecf0; padding-bottom:8px; margin-bottom:14px; }
  table { width:100%; border-collapse:collapse; font-size:13px; margin:10px 0; }
  th { background:#eef2f6; color:#1a3a5c; padding:8px 10px; text-align:left; font-weight:600; border:1px solid #dde3ea; }
  td { padding:7px 10px; border:1px solid #e5eaf0; vertical-align:top; }
  tr:nth-child(even) td { background:#fafbfc; }
  .ok { color:#2e7d32; font-weight:700; } .bad { color:#c0392b; font-weight:700; } .mid { color:#b8860b; font-weight:700; }
  .chart { width:100%; height:380px; margin:12px 0; }
  .footnote { font-size:11px; color:#8a97a5; margin-top:8px; }
  .note { background:#f0f7fd; border-left:4px solid #2d5f8a; padding:12px 16px; border-radius:6px; font-size:13px; margin:10px 0; }
  .disclaim { font-size:11px; color:#95a5a6; border-top:1px solid #e5eaf0; padding-top:12px; margin-top:20px; }
  code { background:#eef2f6; padding:1px 5px; border-radius:3px; font-size:12px; }
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1>因子拥挤度 / 估值 周度诊断报告</h1>
  <div class="sub">__TITLE_DATE__ ｜ quantlab/factor/crowding.py（估值价差 + 配对相关性 + 换手分位）｜ 每 5 交易日采样，因果滚动归一 ｜ universe = ashare_ex ｜ 监控池 __N_FACTORS__ 因子</div>
</header>

<div class="tldr">
  <h2>本周要点</h2>
  <ul>
__TLDR__
  </ul>
</div>

<div class="card">
  <h2>一、当前水平与周环比</h2>
  __LATEST_TABLE__
  <div class="footnote">红 = 综合拥挤度历史分位 ≥80%（预警）；绿 = ≤20%（干净）。周环比 = 最新采样点 vs 上一采样点（≈5 个交易日）。估值价差 z 着色：<span class="ok">绿 = 价差收窄（多头被买贵，贡献拥挤）</span>，<span class="bad">红 = 价差走阔（多头便宜）</span>，仅 |z|≥0.3 着色。</div>
</div>

<div class="card">
  <h2>二、综合拥挤度时序（生产核心 + 量价代表）</h2>
  <div id="chart1" class="chart"></div>
  <h2 style="margin-top:18px;">新因子组</h2>
  <div id="chart2" class="chart"></div>
  <div class="footnote">时序读取指南：2023Q4~2024Q1 核心组冲 92~98% 分位（微盘股灾前夜）；2026-05/06 暴跌洗盘后核心拥挤释放；SUE 自 2026-04 抬升并与 IC 衰减同段。</div>
</div>

<div class="card">
  <h2>三、因子估值：多空两端 BP 价差 z 时序</h2>
  <div id="chart3" class="chart" style="height:420px;"></div>
  <div class="footnote">val_z = log(多头组 BP 中位数 / 空头组 BP 中位数) 的因果滚动 z。低 = 多头被买贵（价差收窄、贡献拥挤）；高 = 多头端便宜。点击图例可开关因子。</div>
</div>

<div class="card">
  <h2>四、核心拥挤度与门控权重（诊断参考）</h2>
  <div id="chart4" class="chart"></div>
  <div class="note"><b>定位声明（batch14 实证结论，2026-09-07）</b>：拥挤度/估值指标在本项目样本（2022-2026，10 因子）中的系统性检验结论——①"拥挤→衰减"不成立：综合拥挤度与未来 20 日因子收益为弱<b>正相关</b>（池化 IC +0.08~0.12，t 1.5~1.8），且分年变号；②低拥挤因子的后验收益反而更差（−0.75% vs 无条件 +1.78%）；③按"估值便宜/低拥挤"轮动选因子与拥挤动态门控均跑输静态基线。因此<b>本报告为诊断与暴露监控工具：预警 = 关注/归因，不是减仓指令；生产组合切换不引入拥挤门控</b>。详见 <code>research/因子拥挤度与因子估值.md</code>。</div>
</div>

<div class="card">
  <h2>五、口径与局限</h2>
  <table>
    <tr><th>事项</th><th>说明</th></tr>
    <tr><td>指标定义</td><td>估值价差 val_spread = log(多头组 BP 中位数/空头组 BP 中位数)，z 归一后取反贡献；配对相关性 = 多头组 60 日收益两两相关均值，+z 贡献；换手分位 = 多头组换手中位数 2 年滚动分位，2·pct−1 贡献；综合 = 三贡献均值（≥2 非空）</td></tr>
    <tr><td>因果性</td><td>z/分位只用过去 100 个采样点（最少 40），无前视；可直接用于回测</td></tr>
    <tr><td>bp 口径</td><td>finance_snapshot as-of 回填，与 bp 因子同源（audit WARN 同源口径）</td></tr>
    <tr><td>窗口效应</td><td>2 年滚动分位在 regime 剧变后需时间"忘掉"旧常态；新因子（sue 系）采样点少，其分位置信度偏低</td></tr>
    <tr><td>数据链路</td><td>每日流水线"拥挤度监控"步骤更新 <code>data/lake/crowding/</code>；本报告只读湖文件生成，不触碰因子计算</td></tr>
  </table>
  <div class="disclaim">免责声明：拥挤度/估值为研究诊断指标，不构成投资建议。市场有风险，投资需谨慎。生成器：scripts/crowding_weekly_report.py（数据驱动，每次运行自动取最新湖数据）。</div>
</div>

</div>

<script>
var D = __CHART_DATA__;

function mkSeries(names, key) {
  var s = [];
  names.forEach(function(n){
    if (!D.series[n]) return;
    s.push({ name: n, type: 'line', showSymbol: false, connectNulls: false,
             lineStyle: { width: (n==='size'||n==='sue')?2.4:1.4 },
             itemStyle: { color: D.color[n] || '#999' },
             data: D.series[n][key] });
  });
  return s;
}
function datesOf(n) { return D.series[n] ? D.series[n].dates : []; }
function mkChart(id, names, key, yName) {
  var ch = echarts.init(document.getElementById(id));
  ch.setOption({
    tooltip: { trigger: 'axis' },
    legend: { top: 4, type: 'scroll' },
    grid: { left: 50, right: 20, top: 42, bottom: 55 },
    xAxis: { type: 'category', data: datesOf(names[0]), axisLabel: { rotate: 30, fontSize: 10 } },
    yAxis: { type: 'value', name: yName },
    series: mkSeries(names, key)
  });
  return ch;
}
var charts = [];
charts.push(mkChart('chart1', D.pool.core.concat(D.pool.pricevol), 'crowding', '综合拥挤度 z'));
charts.push(mkChart('chart2', D.pool.new, 'crowding', '综合拥挤度 z'));
charts.push(mkChart('chart3', __ALL_NAMES__, 'val_z', '估值价差 z'));

var c4 = echarts.init(document.getElementById('chart4'));
c4.setOption({
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
                  label: { formatter: '预警参考' }, data: [{ yAxis: 1.0 }] },
      data: D.gate.core_crowding },
    { name: '门控权重 w（右轴）', type: 'line', yAxisIndex: 1, showSymbol: false,
      lineStyle: { width: 2.2, color: '#c0392b' }, itemStyle: { color: '#c0392b' },
      data: D.gate.w }
  ]
});
charts.push(c4);
window.addEventListener('resize', function(){ charts.forEach(function(ch){ ch.resize(); }); });
</script>
</body>
</html>
"""

    html = (TEMPLATE
            .replace("__TITLE_DATE__", str(_date.today()))
            .replace("__N_FACTORS__", str(len(names_all)))
            .replace("__ALL_NAMES__", json.dumps(names_all))
            .replace("__TLDR__", "\n".join(f"    <li>{t}</li>" for t in tldr))
            .replace("__LATEST_TABLE__", latest_table)
            .replace("__CHART_DATA__", chart_data))

    out = Path(f"reports/crowding_weekly_{_date.today():%Y%m%d}.html")
    if write:
        out.write_text(html, encoding="utf-8")
        print(f"written {out} ({len(html)//1024} KB)")
    return str(out)


if __name__ == "__main__":
    main()
