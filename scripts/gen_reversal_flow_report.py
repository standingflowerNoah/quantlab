# -*- coding: utf-8 -*-
"""从 results.json 生成反转×资金流策略 HTML 报告（静态、图片同目录相对路径）"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "reports" / "reversal_flow"
r = json.loads((OUT / "results.json").read_text(encoding="utf-8"))

ic = r["ic"]
sel = r["selection"]
bt = r["backtest"]
tm = r["timing"]
ys = r["year_split"]

ORDER = ["reversal_5", "reversal_10", "anchor_reversal_20",
         "mf_main_pct_20", "mf_small_pct_20", "mf_net_pct_20",
         "mf_smart_dumb_20", "mf_main_chg_20"]


def fmt(v, pct=False, sign=False):
    if v is None:
        return "-"
    if isinstance(v, str):
        return v
    if pct:
        return f"{v*100:+.1f}%" if sign else f"{v*100:.1f}%"
    return f"{v:+.3f}" if sign else f"{v:.3f}"


def ic_rows():
    rows = []
    for n in ORDER:
        d = ic[n]
        rows.append(
            f"<tr><td class='k'>{n}</td>"
            f"<td>{fmt(d['ic5'], sign=True)}</td>"
            f"<td>{fmt(d['ic20'], sign=True)}</td>"
            f"<td>{fmt(d['icir20'], sign=True)}</td>"
            f"<td>{fmt(d['win20'])}</td>"
            + "".join(f"<td>{fmt(d.get(f'ic20_{y}'), sign=True)}</td>"
                      for y in (2022, 2023, 2024, 2025, 2026))
            + "</tr>")
    return "\n".join(rows)


def bt_rows(keys):
    rows = []
    for k in keys:
        d = bt[k]
        rows.append(
            f"<tr><td class='k'>{k}</td>"
            f"<td>{d['factors']}<br><span class='m'>{d['method']}</span></td>"
            f"<td>{d['rebalance']}</td>"
            f"<td>{fmt(d['turnover_avg'])}</td>"
            f"<td class='b'>{fmt(d['ann'], pct=True, sign=True)}</td>"
            f"<td class='b'>{fmt(d['sharpe'])}</td>"
            f"<td>{fmt(d['mdd'], pct=True, sign=True)}</td>"
            f"<td>{fmt(d['excess_ann'], pct=True, sign=True)}</td>"
            f"<td>{fmt(d['ir'])}</td></tr>")
    return "\n".join(rows)


def tm_rows():
    rows = []
    for k, d in tm.items():
        cls = "hl" if d["avg_pos"] < 1 else ""
        rows.append(
            f"<tr class='{cls}'><td class='k'>{k.replace('__', ' × ')}</td>"
            f"<td>{fmt(d['ann'], pct=True, sign=True)}</td>"
            f"<td>{fmt(d['sharpe'])}</td>"
            f"<td>{fmt(d['mdd'], pct=True, sign=True)}</td>"
            f"<td>{fmt(d['avg_pos'])}</td>"
            f"<td>{fmt(d['excess_ann'], pct=True, sign=True)}</td></tr>")
    return "\n".join(rows)


def year_rows():
    keys = list(ys.keys())
    years = ["2022", "2023", "2024", "2025", "2026"]
    rows = []
    for k in keys:
        d = ys[k]
        rows.append(f"<tr><td class='k'>{k}</td>" + "".join(
            f"<td>{fmt(d.get(y), pct=True, sign=True)}</td>" for y in years)
            + "</tr>")
    return "\n".join(rows)


html = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>反转 × 资金流量因子策略研究 · 2026-09-14</title>
<style>
  body {{ font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
         margin: 0; background: #f6f7f9; color: #1c2733; }}
  .wrap {{ max-width: 1060px; margin: 0 auto; padding: 24px 20px 60px; }}
  h1 {{ font-size: 24px; margin: 8px 0 4px; }}
  h2 {{ font-size: 18px; margin: 34px 0 10px; border-left: 4px solid #2455a4;
       padding-left: 10px; }}
  .sub {{ color: #6b7785; font-size: 13px; margin-bottom: 20px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px;
          background: #fff; box-shadow: 0 1px 2px rgba(0,0,0,.06); }}
  th, td {{ border: 1px solid #e3e8ee; padding: 6px 8px; text-align: right; }}
  th {{ background: #eef2f7; font-weight: 600; }}
  td.k, th:first-child {{ text-align: left; }}
  td.b {{ font-weight: 700; }}
  td .m {{ color: #8a94a0; font-size: 11px; }}
  tr.hl td {{ background: #fdf6e9; }}
  .card {{ background: #fff; border-radius: 10px; padding: 16px 18px;
          margin: 14px 0; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .card h3 {{ margin: 0 0 8px; font-size: 15px; }}
  .finding {{ border-left: 4px solid #c0392b; }}
  .finding.pos {{ border-left-color: #1e8e5a; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
  img {{ width: 100%; border-radius: 8px; background: #fff;
        box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .foot {{ color: #8a94a0; font-size: 12px; margin-top: 30px; }}
  code {{ background: #eef2f7; padding: 1px 5px; border-radius: 4px;
         font-size: 12px; }}
</style>
</head>
<body>
<div class="wrap">
<h1>反转 × 资金流量因子策略研究</h1>
<div class="sub">2026-09-14 · 池 {r['meta']['pool']} · 基准中证1000 ·
成本 {r['meta']['cost_per_turnover']*100:.2f}%/次换手 ·
4 相位平均 · 严格因果择时（仓位变动全额计费） ·
<a href="../../research/反转资金流策略_20260914.md">研究文档</a> ·
<a href="results.json">原始数字</a></div>

<div class="card finding pos"><h3>✅ 核心结论 1：主力资金流是稳定的反向指标</h3>
<code>mf_main_pct_20</code>（20日主力净流入占成交额比）IC20 −0.025 / ICIR −0.44，
五年 4 负 1 零——主力持续净流入的股票后续跑输（拉高出货模式）。与龙虎榜
dragon_net_20 修复后负 IC 同向：A 股"聪明钱"标签的资金流数据方向普遍是反的。</div>

<div class="card finding pos"><h3>✅ 核心结论 2：反转+资金流等权合成有效</h3>
anchor_reversal_20 + mf_main_pct_20 等权（top100/20日调仓）：
年化 <b>12.1%</b> / 夏普 <b>0.344</b> / 回撤 −40.9% / 超额 +7.7%，
夏普较纯反转（0.269）提升 28%——资金流因子定位是<b>组合分散项</b>而非核心 alpha。</div>

<div class="card finding"><h3>⚠️ 核心结论 3：短窗反转因子死于换手成本</h3>
reversal_10 IC20 +0.064 不弱，但 10 日调仓换手 97%（因子窗与调仓窗完全错位），
年化被 ~8.5% 成本拖成 −7.3%。anchor（60 日锚）换手 65% 存活。
反转策略可行性 = 信号强度 × 信号慢速性。</div>

<div class="card finding"><h3>⚠️ 核心结论 4：反转策略上择时全败（结构性）</h3>
反转策略的回撤期=建仓期，趋势/资金流择时恰好在最深处砍仓：MA 趋势择时把
年化 11.9% 做成 −2.7%。唯一可辩护：市场主力资金流择时把回撤 −42.8%→−24.0%
减半、夏普持平（绝对收益减半）——风险预算工具而非收益增强工具。</div>

<h2>一、IC 实测（2022-01 ~ 2026-09，ashare_ex 池）</h2>
<table>
<tr><th>因子</th><th>IC5</th><th>IC20</th><th>ICIR20</th><th>胜率</th>
<th>2022</th><th>2023</th><th>2024</th><th>2025</th><th>2026</th></tr>
{ic_rows()}
</table>
<p class="foot">方向=IC 符号（样本内拟合，与库内 FACTOR_DIRECTION 同法）。
资金流族入组合门槛 |ICIR20|≥0.15：仅 mf_main_pct_20 入选；
mf_smart_dumb 与其冗余（corr 0.94），其余 IC 归零留档。</p>

<h2>二、因子截面相关性</h2>
<img src="corr_heatmap.png" alt="相关性热力图">
<p class="foot">anchor × mf_main = +0.24（轻度正相关，方向校正后同向偏好"跌过+主力流出"）；
mf 族内 main/small/smart_dumb 高度冗余（|corr| 0.79~0.95）。</p>

<h2>三、合成策略回测（top100 等权，4 相位平均，扣费后）</h2>
<h3>10 日调仓</h3>
<table>
<tr><th>组合</th><th>因子/方法</th><th>调仓</th><th>换手</th><th>年化</th>
<th>夏普</th><th>回撤</th><th>超额年化</th><th>IR</th></tr>
{bt_rows(["R_pure_rb10", "R_rev10_rb10", "F_pure_rb10", "RF_equal_rb10",
          "RF_icir_rb10", "RF_interact_rb10", "R_flowfilter90_rb10",
          "REF_size_amihud_rb10"])}
</table>
<h3>20 日调仓</h3>
<table>
<tr><th>组合</th><th>因子/方法</th><th>调仓</th><th>换手</th><th>年化</th>
<th>夏普</th><th>回撤</th><th>超额年化</th><th>IR</th></tr>
{bt_rows(["R_pure_rb20", "F_pure_rb20", "RF_equal_rb20", "RF_icir_rb20",
          "RF_interact_rb20", "REF_size_amihud_rb20"])}
</table>
<p class="foot">资金流硬过滤（剔除主力净流入前 10%/20% 分位）三档敏感性全部
持平或略负——负 IC 因子做过滤器不如做合成项。REF 为生产模型参考组。</p>
<img src="nav_combos.png" alt="组合净值对比">

<h2>四、择时叠加（严格因果：t-1 信号 → t 仓位，仓位变动全额计费）</h2>
<table>
<tr><th>策略 × 择时</th><th>年化</th><th>夏普</th><th>回撤</th>
<th>平均仓位</th><th>超额年化</th></tr>
{tm_rows()}
</table>
<div class="grid">
<img src="nav_timing.png" alt="择时净值对比">
<img src="timing_signals.png" alt="择时信号">
</div>
<img src="drawdown.png" alt="回撤对比">

<h2>五、分年拆解</h2>
<table>
<tr><th>策略</th><th>2022</th><th>2023</th><th>2024</th><th>2025</th><th>2026</th></tr>
{year_rows()}
</table>
<p class="foot">反转 alpha 集中在 2025（+38~41%）；择时（T1/T3）把 2025 砍半、
2024 恶化——结构性失败，非参数问题。</p>

<h2>六、IC 分年稳定性</h2>
<img src="ic_by_year.png" alt="IC 分年">

<h2>七、定位与待办</h2>
<div class="card"><h3>定位：研究结论归档，非生产替代</h3>
生产模型 size+amihud_20（rb20 年化 23.4%/夏普 0.70）全面占优，与 alpha191
报告结论一致——A 股小市值+低流动性溢价幅度远大于短周期量价 alpha。
本研究的价值：① 5 个资金流因子入湖（方向已登记 FACTOR_DIRECTION）；
② "资金流=反指"方法论沉淀；③ 换手成本与择时因果口径的教训。</div>
<div class="card"><h3>待办</h3>
① DuckDB 写锁释放后跑构建审查（PIT 自检）：<br>
<code>scripts/factor_audit.py --names mf_main_pct_20,mf_small_pct_20,mf_net_pct_20,mf_smart_dumb_20,mf_main_chg_20 --force</code><br>
② 确认每日流水线对 mf 因子的增量计算（update_moneyflow 已注册）；<br>
③ 2027-01 复核 mf_main_pct_20 IC 稳定性（2026 年近零需跟踪）。</div>

<div class="foot">生成：scripts/gen_reversal_flow_report.py · 数据截至 2026-09-10/11 ·
脚本 scripts/reversal_flow_strategy.py --skip-compute 可复跑</div>
</div>
</body>
</html>
"""

f = OUT / "strategy_report.html"
f.write_text(html, encoding="utf-8")
print("written:", f)
