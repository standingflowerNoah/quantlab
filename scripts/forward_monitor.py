"""纸面组合前向监控报告
=====================================
读取 signal_portfolio 台账（每日流水线决策步骤写入的目标持仓快照），
按 tracker.paper_nav 逐日盯市，与前向验证预期（回测口径）对照：

- 回测预期（size+amihud，2022-2026）：年化 ~32%、IR ~1.6、回撤 -40% 量级
- 前向跟踪的意义：回测可以被审出前视，但前向收益无法作弊——
  这是生产模型的最终裁判。

账本快照 <2 期时输出等待页（不视为错误，流水线中可安全运行）。
用法: python scripts/forward_monitor.py   → reports/forward_monitor.html
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from quantlab.config import get_logger
from quantlab.data.store import Store
from quantlab.decision.tracker import (paper_nav, paper_nav_multi,
                                       signal_dates)

log = get_logger("forward_monitor")

EXPECT = {"ann": 0.32, "ir": 1.6, "mdd": -0.40}   # 回测预期（对照带）


def _metrics(curve: pd.DataFrame) -> dict:
    ret = curve["ret"]
    years = len(curve) / 252
    nav = (1 + ret).cumprod()
    ann = nav.iloc[-1] ** (1 / years) - 1 if years > 0 else np.nan
    vol = ret.std() * 252 ** 0.5
    sharpe = (ann - 0.02) / vol if vol > 0 else np.nan
    dd = (nav / nav.cummax() - 1).min()
    return {"days": len(curve), "ann": ann, "vol": vol, "sharpe": sharpe, "mdd": dd}


def build_report() -> Path:
    dates = signal_dates()
    curve = paper_nav()
    store = Store()

    if len(dates) < 2 or curve.empty:
        html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>纸面组合前向监控</title><style>
body{{font-family:'Microsoft YaHei',sans-serif;background:#f6f8fa;color:#1f2328;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.box{{background:#fff;border:1px solid #d0d7de;border-radius:10px;padding:40px 60px;text-align:center}}
h1{{font-size:20px}} .big{{font-size:40px;margin:18px 0;color:#9a6700}}
p{{color:#57606a;font-size:14px;line-height:1.8}}</style></head><body><div class="box">
<h1>纸面组合前向监控</h1>
<div class="big">等待积累</div>
<p>台账快照 {len(dates)} 期（需 ≥2 期产生前向收益）。<br>
生产模型 size+amihud_20 已于 2026-09-05 重置账本，<br>
每个交易日 07:00 流水线自动记录当日目标持仓快照；<br>
首个前向净值将在第 2 个交易日出现。</p>
<p style="font-size:12px;color:#8b949e">生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
</div></body></html>"""
        out = Path("reports/forward_monitor.html")
        out.write_text(html, encoding="utf-8")
        log.info(f"前向监控: 台账 {len(dates)} 期，等待积累 → {out}")
        return out

    # 有前向数据：盯市曲线 + 基准
    bench = store.q(
        "SELECT date, close FROM index_kline WHERE code='000852.SH'")
    bench["date"] = pd.to_datetime(bench["date"])
    b = bench.set_index("date")["close"].pct_change()
    curve = curve.copy()
    curve["bench"] = curve["date"].map(b).fillna(0.0)
    curve["nav_bm"] = (1 + curve["bench"]).cumprod()
    m = _metrics(curve)
    years = m["days"] / 252
    MIN_DAYS = 20          # 样本不足 20 个交易日不外推年化（单日收益年化会得荒谬数字）
    bm_ann = curve["nav_bm"].iloc[-1] ** (1 / years) - 1 if years > 0 else np.nan
    ex = (curve["ret"] - curve["bench"])
    ex_nav = (1 + ex).cumprod()
    ex_ann = ex_nav.iloc[-1] ** (1 / years) - 1 if years > 0 else np.nan

    if m["days"] < MIN_DAYS:
        ann_txt = f"累计 {(1 + curve['ret']).prod() - 1:+.2%}（{m['days']} 日，样本不足不外推年化）"
        ex_txt = f"累计 {float(ex_nav.iloc[-1] - 1):+.2%}"
        sharpe_txt = mdd_note = "样本不足"
        ok = "积累中"
    else:
        ann_txt = f"{m['ann']:+.1%}（回测预期 ~{EXPECT['ann']:+.0%}）"
        ex_txt = f"{ex_ann:+.1%}"
        sharpe_txt = f"{m['sharpe']:.2f}"
        mdd_note = f"{m['mdd']:.1%}（回测 {EXPECT['mdd']:.0%}）"
        ok = "达标" if m["ann"] > 0.15 else "观察"
    rows = {
        "跟踪交易日": m["days"], "开始日期": str(curve["date"].min().date()),
        "纸面收益": ann_txt,
        "基准(中证1000)": f"{bm_ann:+.1%}" if m["days"] >= MIN_DAYS else "样本不足",
        "超额收益": ex_txt, "纸面夏普": sharpe_txt,
        "前向最大回撤": mdd_note,
        "结论": ok,
    }
    stat_rows = "".join(f"<tr><td>{k}</td><td><b>{v}</b></td></tr>"
                        for k, v in rows.items())

    # ── 候选模型对比（sue_i 切换裁决的前向证据；无快照/样本不足为正常态）──
    cand_notes = []
    cand_curves = {}
    for model in ("PROD_SI", "V3_SI"):
        c = paper_nav_multi(model)
        if c.empty:
            cand_notes.append(f"<tr><td>{model}</td><td>积累中（快照不足 2 期）</td></tr>")
            continue
        cand_curves[model] = c
        mc = _metrics(c)
        # 与生产曲线对齐日期后算相对表现
        j = c.merge(curve[["date", "ret"]], on="date", suffixes=("", "_prod"))
        if j.empty:
            cand_notes.append(f"<tr><td>{model}</td><td>{mc['days']} 日（与生产无重叠期）</td></tr>")
            continue
        rel = (1 + j["ret"]).prod() - (1 + j["ret_prod"]).prod()
        cand_notes.append(
            f"<tr><td>{model}</td><td>{mc['days']} 日 | "
            f"累计 {(1 + c['ret']).prod() - 1:+.2%} | "
            f"相对生产 {rel:+.2%}</td></tr>")
    cand_html = ""
    if cand_notes:
        cand_html = ("<h2 style='font-size:16px'>候选模型纸面对比"
                     "（sue_i 切换裁决证据，≥60 交易日再议）</h2>"
                     "<table><tbody>" + "".join(cand_notes) + "</tbody></table>")

    payload = json.dumps({
        "date": [str(d.date()) for d in curve["date"]],
        "nav": [round(float(x), 4) for x in (1 + curve["ret"]).cumprod()],
        "nav_bm": [round(float(x), 4) for x in curve["nav_bm"]],
    }, ensure_ascii=False)

    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<title>纸面组合前向监控</title><style>
body{{font-family:'Microsoft YaHei',sans-serif;background:#f6f8fa;color:#1f2328;margin:0}}
.wrap{{max-width:1100px;margin:0 auto;padding:24px}}
h1{{font-size:20px}} table{{border-collapse:collapse;background:#fff;margin:16px 0}}
th,td{{border:1px solid #d0d7de;padding:6px 14px;font-size:13.5px}}
th{{background:#f6f8fa;text-align:left}}
#chart{{background:#fff;border:1px solid #d0d7de;border-radius:8px;height:380px}}
.note{{font-size:13px;color:#57606a;margin-top:14px}}</style></head><body><div class="wrap">
<h1>纸面组合前向监控（size+amihud_20 生产模型）</h1>
<table><tbody>{stat_rows}</tbody></table>
{cand_html}
<div id="chart"></div>
<p class="note">纸面组合 = 每日流水线记录的目标持仓快照逐日盯市（无成本假设差异、无前视空间）。
回测可以被审出前视，前向收益无法作弊——本页是生产模型的最终裁判。
生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
</div><script>
const P = {payload};
const axis = {{axisLine:{{lineStyle:{{color:'#e6e8ee'}}}},axisLabel:{{color:'#7a8194'}},splitLine:{{lineStyle:{{color:'#eef0f4'}}}}}};
echarts.init(document.getElementById('chart')).setOption({{
  grid:{{left:70,right:30,top:30,bottom:60}},
  tooltip:{{trigger:'axis'}},
  legend:{{data:['纸面净值','中证1000'],top:0}},
  xAxis:{{type:'category',data:P.date,...axis}},
  yAxis:{{type:'value',scale:true,...axis}},
  series:[{{name:'纸面净值',type:'line',data:P.nav,showSymbol:false,lineStyle:{{width:2,color:'#d8463e'}}}},
          {{name:'中证1000',type:'line',data:P.nav_bm,showSymbol:false,lineStyle:{{width:1.5,color:'#7a8194'}}}}]
}});
</script></body></html>"""
    out = Path("reports/forward_monitor.html")
    out.write_text(html, encoding="utf-8")
    log.info(f"前向监控: {m['days']} 个交易日，纸面累计 {(1 + curve['ret']).prod() - 1:+.2%} → {out}")
    return out


if __name__ == "__main__":
    build_report()
