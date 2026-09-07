#!/usr/bin/env python3
"""高频因子完整评估报告（自包含 HTML + 控制台摘要）
用法: python scripts/highfreq_report.py
输出: reports/highfreq_factor_report.html

评估内容：
1. RankIC：T+1 / T+5 / T+10 / T+20（ICIR、胜率）
2. 分层回测：5 分层、每 20 个交易日调仓、持有期内逐日盯市（+ 等权基准）
3. 稳健性：2025（样本内） vs 2026（准样本外）分段 IC
4. 增值性：与既有日频代表因子的平均截面秩相关
"""
import sys
import json
import pickle
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from quantlab.config import REPORTS_DIR
from quantlab.data.store import Store, query
from quantlab.factor import factor_ic

TRADING_DAYS = 252
HORIZONS = [1, 5, 10, 20]

# 高频因子 + 对照组（既有日频代表）
HF_FACTORS = [
    "hf_rv_20", "hf_rvvol_20", "hf_rjv_20", "hf_rsk_20", "hf_rku_20",
    "hf_dsem_20",
    "hf_vopen_20", "hf_vclose_20", "hf_vhhi_20", "hf_vampp_20",
    "hf_rlast30_20", "hf_rfirst30_20", "hf_hipos_20",
    "hf_amihud_20", "hf_amtrange_20", "hf_vwapbias_20",
    "hf_corr_rv_20", "hf_smartq_10", "hf_smartq_20",
    "hf_topvr_20", "hf_topvupr_20",
]
BENCH_FACTORS = ["volatility_20", "turnover", "reversal_5", "amplitude_20",
                 "overnight_mom_20"]


def ic_stats(name, horizon, start=None, end=None):
    ics = factor_ic(name, horizon=horizon, start=start, end=end)
    if ics.empty:
        return {"n_days": 0, "ic": np.nan, "icir": np.nan, "win": np.nan}
    ic = ics["ic"]
    return {"n_days": len(ic),
            "ic": round(float(ic.mean()), 4),
            "icir": round(float(ic.mean() / ic.std()), 3) if ic.std() > 0 else np.nan,
            "win": round(float((ic > 0).mean()), 3)}


def seg_ic(name, horizon):
    """2025 vs 2026 分段"""
    a = ic_stats(name, horizon, start="2025-01-01", end="2025-12-31")
    b = ic_stats(name, horizon, start="2026-01-01", end="2026-12-31")
    return a, b


def daily_returns() -> pd.DataFrame:
    """日频收益（date/code/ret）：ret = 当日复权收盘 / 昨日 - 1。

    与 _fwd_return 同源同口径（kline_daily 视图 + close*adj_factor 前复权），
    LAG 窗口函数替代 pandas shift；只取分层样本段（2024-12 下旬起）。
    """
    df = query("""
        SELECT date, code, c / c_lag - 1 AS ret
        FROM (
            SELECT date, code, close * adj_factor AS c,
                   LAG(close * adj_factor) OVER (
                       PARTITION BY code ORDER BY date) AS c_lag
            FROM kline_daily
            WHERE date >= DATE '2024-12-20'
        )
        WHERE c_lag IS NOT NULL AND c > 0 AND c_lag > 0
    """)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df[["date", "code", "ret"]].dropna()


def layer_backtest(ret_df, dates_rt, store, name, horizon=20, n_q=5):
    """分层日频回测：每 horizon 个交易日按因子值五等分重组，持有期内逐日盯市。

    层内等权（当日收益 = 成分股日收益均值）；调仓日 = 因子截面日 [::horizon]；
    调仓日 t 的持仓 accruing 于 t+1 .. t+horizon（与 fwd 口径一致，无前视）。
    统计（年化/IR/分年）全部由日频收益推导，年化基数 252。
    """
    fv = store.read_factor(name)
    if fv.empty:
        return None
    fv["date"] = pd.to_datetime(fv["date"])
    fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
    fv = fv[np.isfinite(fv["value"])]
    if fv.empty:
        return None

    pos = {d: i for i, d in enumerate(dates_rt)}
    fdates = pd.DatetimeIndex(np.sort(fv["date"].unique()))
    rdates = fdates[::horizon]
    by_date = {d: g for d, g in fv.groupby("date")}

    parts = []
    for t in rdates:
        i0 = pos.get(t)
        if i0 is None or i0 + 1 >= len(dates_rt):
            continue
        seg = dates_rt[i0 + 1: i0 + 1 + horizon]
        cs = by_date[t]
        try:
            q = pd.qcut(cs["value"], n_q, labels=False, duplicates="drop")
        except Exception:
            continue
        if q.notna().sum() == 0:  # 截面全部并列等退化情形
            continue
        qmap = pd.Series(q.to_numpy(), index=cs["code"].to_numpy())
        sub = ret_df.loc[seg[0]:seg[-1]]
        if sub.empty:
            continue
        tmp = pd.DataFrame({"date": sub.index.to_numpy(),
                            "q": sub["code"].map(qmap).to_numpy(),
                            "ret": sub["ret"].to_numpy()})
        tmp = tmp.dropna(subset=["q"])
        if tmp.empty:
            continue
        tmp["q"] = tmp["q"].astype(int)
        parts.append(tmp.groupby(["date", "q"])["ret"].mean().unstack()
                     .reindex(columns=range(n_q)))

    if not parts:
        return None
    lr = pd.concat(parts).sort_index()  # 日频 × 层 收益矩阵
    lr = lr[~lr.index.duplicated(keep="first")]

    q_ann = [float((1 + lr[q].mean()) ** TRADING_DAYS - 1) for q in range(n_q)]
    ls_d = (lr[n_q - 1] - lr[0]).dropna()
    if ls_d.empty:
        return None
    ls_ann = float((1 + ls_d.mean()) ** TRADING_DAYS - 1)
    ls_ir = (float(ls_d.mean() / ls_d.std() * np.sqrt(TRADING_DAYS))
             if ls_d.std() > 0 else float("nan"))
    mono = pd.Series(q_ann).rank().corr(pd.Series(range(n_q)))
    yr_ann = ls_d.groupby(ls_d.index.year).apply(
        lambda s: float((1 + s).prod() ** (TRADING_DAYS / max(len(s), 1)) - 1))
    ls_nav = (1 + (lr[n_q - 1] - lr[0]).fillna(0)).cumprod()
    return {"q_ann": [round(v, 4) for v in q_ann],
            "ls_ann": round(ls_ann, 4),
            "ls_ir": round(ls_ir, 2), "mono": round(float(mono), 3),
            "curve": [{"date": str(d.date()), "nav": round(float(v), 4)}
                      for d, v in ls_nav.items()],
            "layer_curves": {
                f"Q{q + 1}": [{"date": str(d.date()), "nav": round(float(v), 4)}
                              for d, v in (1 + lr[q].fillna(0)).cumprod().items()]
                for q in range(n_q)},
            "yr_ls": {int(k): round(v, 4) for k, v in yr_ann.items()}}


def cross_corr(store, names, bench):
    """因子间平均截面秩相关（高频 vs 既有日频）"""
    out = {}
    mats = {}
    for n in list(names) + list(bench):
        fv = store.read_factor(n)
        if fv.empty:
            continue
        fv["date"] = pd.to_datetime(fv["date"])
        fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
        mats[n] = fv.dropna(subset=["value"])
    for n in names:
        if n not in mats:
            continue
        cors = []
        for b in bench:
            if b not in mats or b == n:
                continue
            m = mats[n].merge(mats[b], on=["date", "code"],
                              suffixes=("_a", "_b"))
            if m.empty:
                continue
            cc = (m.groupby("date")
                    .apply(lambda g: g["value_a"].corr(g["value_b"],
                                                       method="spearman"))
                    if len(m) > 0 else pd.Series(dtype=float))
            cc = cc.dropna()
            if len(cc):
                cors.append(float(cc.mean()))
        out[n] = round(float(np.mean(cors)), 3) if cors else np.nan
    return out


def _compute(store):
    """全部重计算：IC 表 / 日频分层回测 / 分段稳健性 / 增值性 / 基准曲线。"""
    # 0) 日频收益 + 等权基准（全市场逐日等权均值，累计净值）
    ret_df = daily_returns().sort_values(["date", "code"]).set_index("date")
    if ret_df.empty:
        raise SystemExit("日频收益为空，检查 kline_daily 视图")
    dates_rt = pd.DatetimeIndex(ret_df.index.unique())
    bench_d = ret_df.groupby(level=0)["ret"].mean()
    bench_d = bench_d[bench_d.index >= pd.Timestamp("2025-01-01")]
    bench_curve = [{"date": str(d.date()), "nav": round(float(v), 4)}
                   for d, v in (1 + bench_d).cumprod().items()]

    # 1) IC 表（多 horizon）
    ic_tables = {h: [] for h in HORIZONS}
    for n in HF_FACTORS:
        for h in HORIZONS:
            ic_tables[h].append({"factor": n, **ic_stats(n, h)})
    ic_dfs = {h: pd.DataFrame(v).set_index("factor") for h, v in ic_tables.items()}

    # 2) 分层回测（日频盯市）
    bt = {}
    for n in HF_FACTORS:
        bt[n] = layer_backtest(ret_df, dates_rt, store, n)

    # 3) 分段（T+5 口径，主要评估 horizon）
    seg = {}
    for n in HF_FACTORS:
        a, b = seg_ic(n, 5)
        seg[n] = {"ic25": a["ic"], "icir25": a["icir"],
                  "ic26": b["ic"], "icir26": b["icir"]}

    # 4) 增值性
    cc = cross_corr(store, HF_FACTORS, BENCH_FACTORS)
    return ic_dfs, bt, seg, cc, bench_curve


def main():
    store = Store()
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    cache = REPORTS_DIR / "_hf_report_cache.pkl"
    blob = None
    if cache.exists():
        try:
            with open(cache, "rb") as f:
                blob = pickle.load(f)
        except Exception:
            blob = None
    if blob is not None and blob.get("day") == today:
        print(f"命中当日计算缓存（{today}），跳过重算")
        ic_dfs, bt, seg, cc, bench_curve = blob["data"]
    else:
        ic_dfs, bt, seg, cc, bench_curve = _compute(store)
        try:
            with open(cache, "wb") as f:
                pickle.dump({"day": today,
                             "data": (ic_dfs, bt, seg, cc, bench_curve)}, f)
            print(f"计算缓存已写入（{today}）")
        except Exception:
            pass

    # 控制台摘要
    t5 = ic_dfs[5]
    print("\n=== T+5 RankIC 总表（按 |ICIR| 排序）===")
    order = t5.reindex(t5["icir"].abs().sort_values(ascending=False).index)
    for n, r in order.iterrows():
        s = seg.get(n, {})
        b = bt.get(n) or {}
        print(f"{n:18s} IC={r['ic']:+.4f} ICIR={r['icir']:+.3f} "
              f"win={r['win']:.2f} | 25'{s.get('ic25', float('nan')):+.4f} "
              f"26'{s.get('ic26', float('nan')):+.4f} | "
              f"LS={b.get('ls_ann', float('nan')):+.2%} "
              f"mono={b.get('mono', float('nan')):.2f} "
              f"corr={cc.get(n, float('nan')):+.2f}")

    # HTML 报告
    html = render(ic_dfs, bt, seg, cc, HF_FACTORS, bench_curve)
    out = REPORTS_DIR / "highfreq_factor_report.html"
    out.write_text(html, encoding="utf-8")
    print(f"\n报告已生成: {out}")


def render(ic_dfs, bt, seg, cc, names, bench_curve):
    def ic_rows(df):
        return "".join(
            f"<tr><td><code>{i}</code></td><td>{r['n_days']}</td>"
            f"<td class=\"{'pos' if r['ic'] > 0 else 'neg'}\">{r['ic']:+.4f}</td>"
            f"<td>{r['icir']:+.3f}</td><td>{r['win']:.2f}</td></tr>"
            for i, r in df.iterrows())

    t5 = ic_dfs[5]
    order = list(t5.reindex(t5["icir"].abs().sort_values(ascending=False).index).index)

    bt_rows = "".join(
        f"<tr><td><code>{n}</code></td>"
        + "".join(f"<td>{bt[n]['q_ann'][q] * 100:.1f}%</td>" for q in range(5))
        + f"<td class=\"{'pos' if bt[n]['ls_ann'] > 0 else 'neg'}\">"
          f"{bt[n]['ls_ann'] * 100:+.1f}%</td><td>{bt[n]['ls_ir']:+.2f}</td>"
          f"<td>{bt[n]['mono']:.2f}</td>"
          f"<td>{bt[n]['yr_ls'].get(2025, float('nan')) * 100:+.0f}%/"
          f"{bt[n]['yr_ls'].get(2026, float('nan')) * 100:+.0f}%</td></tr>"
        for n in order if bt.get(n))

    seg_rows = "".join(
        f"<tr><td><code>{n}</code></td>"
        f"<td class=\"{'pos' if seg[n]['ic25'] > 0 else 'neg'}\">{seg[n]['ic25']:+.4f}</td>"
        f"<td>{seg[n]['icir25']:+.3f}</td>"
        f"<td class=\"{'pos' if seg[n]['ic26'] > 0 else 'neg'}\">{seg[n]['ic26']:+.4f}</td>"
        f"<td>{seg[n]['icir26']:+.3f}</td>"
        f"<td>{cc.get(n, float('nan')):+.2f}</td></tr>"
        for n in order)

    curves = {n: bt[n]["curve"] for n in order[:6] if bt.get(n)}
    layers = {n: {"LS": bt[n]["curve"], **bt[n]["layer_curves"]}
              for n in order if bt.get(n)}
    payload = {
        "ls": [{"name": n, "v": round(bt[n]["ls_ann"] * 100, 1)}
               for n in order if bt.get(n)],
        "curves": curves,
        "layers": layers,
        "bench": bench_curve,
    }

    return HTML_TMPL.format(
        n_factors=len(names),
        ic1_rows=ic_rows(ic_dfs[1]), ic5_rows=ic_rows(ic_dfs[5]),
        ic10_rows=ic_rows(ic_dfs[10]), ic20_rows=ic_rows(ic_dfs[20]),
        bt_rows=bt_rows, seg_rows=seg_rows,
        payload=json.dumps(payload, ensure_ascii=False))


HTML_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QuantLab 高频因子评估报告（分钟数据 → 日频因子）</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
:root{{--bg:#0f1115;--card:#171a21;--text:#e6e8ec;--muted:#9aa3b2;
--line:#2a2f3a;--red:#e24b4a;--green:#1d9e75;--accent:#378add;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--text);font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
line-height:1.6;padding:40px 24px;}}
.wrap{{max-width:1180px;margin:0 auto;}}
h1{{font-size:26px;font-weight:600;}}
h2{{font-size:18px;font-weight:600;margin:40px 0 16px;padding-left:12px;border-left:3px solid var(--accent);}}
.sub{{color:var(--muted);font-size:14px;margin-top:6px;}}
table{{width:100%;border-collapse:collapse;margin:12px 0;font-size:13px;}}
th,td{{padding:7px 10px;text-align:right;border-bottom:1px solid var(--line);}}
th{{color:var(--muted);font-weight:500;}}
td:first-child,th:first-child{{text-align:left;}}
code{{font-family:ui-monospace,"SF Mono",Consolas,monospace;color:#7fb2e5;}}
.pos{{color:var(--red);}}
.neg{{color:var(--green);}}
.chart{{width:100%;height:380px;margin:8px 0;}}
.note{{color:var(--muted);font-size:12px;margin-top:8px;}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:24px;}}
</style>
</head>
<body>
<div class="wrap">
<h1>高频因子评估报告</h1>
<p class="sub">分钟数据（kline_1min）→ 日频特征宽表（minute_feat）→ 21 个高频低频化因子 ·
RankIC / 分层 / 分段稳健性 / 增值性 / 每因子分层曲线 · 样本 2025-01 ~ 2026-09</p>

<h2>一、RankIC —— T+1</h2>
<table><thead><tr><th>因子</th><th>天数</th><th>IC</th><th>ICIR</th><th>胜率</th></tr></thead>
<tbody>{ic1_rows}</tbody></table>

<h2>二、RankIC —— T+5</h2>
<table><thead><tr><th>因子</th><th>天数</th><th>IC</th><th>ICIR</th><th>胜率</th></tr></thead>
<tbody>{ic5_rows}</tbody></table>

<h2>三、RankIC —— T+10</h2>
<table><thead><tr><th>因子</th><th>天数</th><th>IC</th><th>ICIR</th><th>胜率</th></tr></thead>
<tbody>{ic10_rows}</tbody></table>

<h2>四、RankIC —— T+20</h2>
<table><thead><tr><th>因子</th><th>天数</th><th>IC</th><th>ICIR</th><th>胜率</th></tr></thead>
<tbody>{ic20_rows}</tbody></table>

<h2>五、分层回测（5 层 · 每 20 个交易日调仓 · 日频盯市）</h2>
<p class="sub">Q1 低因子值 → Q5 高因子值；层内等权，持有期内逐日盯市（成分股日收益均值，
年化基数 252）；多空 = Q5−Q1；25'/26' = 分年多空年化。基准 = 全市场等权。</p>
<table><thead><tr><th>因子</th><th>Q1</th><th>Q2</th><th>Q3</th><th>Q4</th><th>Q5</th>
<th>多空年化</th><th>多空IR</th><th>单调性</th><th>25'/26'</th></tr></thead>
<tbody>{bt_rows}</tbody></table>

<h2>六、分段稳健性与增值性（T+5）</h2>
<p class="sub">2025（样本内） vs 2026（准样本外）IC；corr = 与既有日频代表因子
（volatility_20/turnover/reversal_5/amplitude_20/intraday_mom_5/overnight_mom_20）
的平均截面秩相关，越接近 0 增值性越强。</p>
<table><thead><tr><th>因子</th><th>IC 2025</th><th>ICIR 2025</th>
<th>IC 2026</th><th>ICIR 2026</th><th>与日频因子相关</th></tr></thead>
<tbody>{seg_rows}</tbody></table>

<h2>七、多空年化总览与净值（Top 6）</h2>
<div id="chartLs" class="chart"></div>
<div id="chartNav" class="chart" style="height:420px"></div>

<h2>八、分层回测曲线（每因子 · Q1-Q5 + 多空 + 基准）</h2>
<p class="sub">日频盯市、每 20 个交易日调仓（与第五节一致）；Q1=低因子值 → Q5=高因子值；
多空 = Q5−Q1（红=正、绿=负，A 股配色）；灰色虚线 = 全市场等权基准。</p>
<div id="layers" class="grid2"></div>

<p class="note" style="margin-top:24px">数据：kline_1min（free-stockdb，2025-01-02 起）。
IC 为日频截面 Spearman 秩相关；分层为等权、未计费用。仅供研究，不构成投资建议。</p>
</div>

<script>
const P = {payload};
const red='#e24b4a', green='#1d9e75', gray='#2a2f3a';
const axis={{axisLine:{{lineStyle:{{color:gray}}}},axisLabel:{{color:'#9aa3b2'}},splitLine:{{lineStyle:{{color:gray}}}}}};
(function(){{
  const items=[...P.ls].sort((a,b)=>b.v-a.v);
  echarts.init(document.getElementById('chartLs')).setOption({{
    grid:{{left:150,right:50,top:20,bottom:30}},
    tooltip:{{trigger:'axis',axisPointer:{{type:'shadow'}}}},
    xAxis:{{type:'value',...axis,axisLabel:{{color:'#9aa3b2',formatter:v=>(v>0?'+':'')+v+'%'}}}},
    yAxis:{{type:'category',data:items.map(x=>x.name),...axis,
           axisLabel:{{color:'#e6e8ec',fontSize:12,fontFamily:'Consolas'}}}},
    series:[{{type:'bar',data:items.map(x=>({{value:x.v,
      itemStyle:{{color:x.v>=0?red:green,borderRadius:3}}}})),barWidth:13}}]
  }});
}})();
(function(){{
  const names=Object.keys(P.curves);
  const series=names.map(k=>({{name:k,type:'line',smooth:true,symbol:'none',
    lineStyle:{{width:2}},data:P.curves[k].map(p=>[p.date,p.nav])}}));
  echarts.init(document.getElementById('chartNav')).setOption({{
    grid:{{left:60,right:30,top:40,bottom:40}},
    tooltip:{{trigger:'axis'}},
    legend:{{textStyle:{{color:'#9aa3b2'}},top:0}},
    xAxis:{{type:'time',...axis}},
    yAxis:{{type:'value',...axis,scale:true}},
    series
  }});
}})();
(function(){{
  const QC=['#85b7eb','#5d9dd8','#378add','#2472bd','#185fa5'];
  const wrap=document.getElementById('layers');
  Object.keys(P.layers).sort((a,b)=>{{
    const va=P.ls.find(x=>x.name===a), vb=P.ls.find(x=>x.name===b);
    return (vb?vb.v:0)-(va?va.v:0);
  }}).forEach((n,i)=>{{
    const cs=P.layers[n];
    const lsEnd=cs.LS.length?cs.LS[cs.LS.length-1].nav:1;
    const lsColor=lsEnd>=1?red:green;
    const div=document.createElement('div');
    div.className='chart'; div.style.height='340px'; div.id='layer_'+i;
    wrap.appendChild(div);
    const entries=Object.keys(cs).map(k=>({{k,v:cs[k]}}));
    if(P.bench) entries.push({{k:'BENCH',v:P.bench}});
    const series=entries.map(({{k,v}})=>{{
      const isB=k==='BENCH', isLS=k==='LS';
      const color=isB?'#8a93a3':(isLS?lsColor:QC[+k[1]-1]);
      return {{
        name:isB?'基准':k, type:'line', smooth:true, symbol:'none',
        lineStyle:isB?{{width:1.5,color:color,type:'dashed'}}
                     :{{width:isLS?2.5:1.5,color:color}},
        itemStyle:{{color:color}},
        data:v.map(p=>[p.date,p.nav])
      }};
    }});
    echarts.init(div).setOption({{
      title:{{text:n,textStyle:{{color:'#e6e8ec',fontSize:13,fontWeight:500}},left:8,top:2}},
      grid:{{left:52,right:16,top:34,bottom:30}},
      tooltip:{{trigger:'axis',valueFormatter:v=>v.toFixed(3)}},
      legend:{{textStyle:{{color:'#9aa3b2',fontSize:11}},top:2,right:8,itemWidth:14}},
      xAxis:{{type:'time',...axis}},
      yAxis:{{type:'value',...axis,scale:true,
             axisLabel:{{color:'#9aa3b2',formatter:v=>v.toFixed(1)}}}},
      series
    }});
  }});
}})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
