# -*- coding: utf-8 -*-
"""生成红利因子研究的 HTML 可视化研报（纯内联 SVG，无外部依赖）"""
import json
import numpy as np
import pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports/dividend_factor"
HTML = ROOT / "reports" / "红利因子评估与增强建模.html"

scr = pd.read_csv(OUT / "factor_screen_official.csv")
hc = pd.read_csv(OUT / "final_model_horizon.csv")
cmp_ = pd.read_csv(OUT / "model_compare.csv")
yr = pd.read_csv(OUT / "final_model_yearly.csv", index_col=0)
curve = pd.read_csv(OUT / "curve_final.csv", index_col=0, parse_dates=True)
stats = pd.read_csv(OUT / "final_model_stats.csv")
topn = pd.read_csv(OUT / "final_model_topn.csv")
cost = pd.read_csv(OUT / "final_model_cost.csv")
rob = pd.read_csv(OUT / "pool_robustness.csv")
cfg = json.load(open(OUT / "final_model.json", encoding="utf-8"))

C_MAIN, C_ALT, C_BENCH, C_DIM = "#2563eb", "#e11d48", "#64748b", "#94a3b8"


def line_chart(series_list, xlabels, w=680, h=280, pad=(52, 20, 40, 16), ylab="", zero=True,
               yfmt="{:.0f}", xtitle=""):
    """series_list: [(name, values, color, dash?)]"""
    L, R, T, B = pad
    iw, ih = w - L - R, h - T - B
    allv = [v for _, vs, _, _ in series_list for v in vs if v == v]
    if not allv:
        return ""
    lo, hi = min(allv), max(allv)
    if zero:
        lo, hi = min(lo, 0), max(hi, 0)
    span = (hi - lo) or 1
    lo, hi = lo - span * 0.10, hi + span * 0.10
    span = hi - lo
    n = len(xlabels)
    xs = [L + (iw * i / max(n - 1, 1)) for i in range(n)]
    ys = lambda v: T + ih * (1 - (v - lo) / span)
    s = [f'<svg viewBox="0 0 {w} {h}" style="width:100%;height:auto;font-family:inherit">']
    for k in range(5):
        yv = lo + span * k / 4
        s.append(f'<line x1="{L}" y1="{ys(yv):.1f}" x2="{w-R}" y2="{ys(yv):.1f}" stroke="#e2e8f0"/>')
        s.append(f'<text x="{L-8}" y="{ys(yv)+4:.1f}" font-size="11" fill="{C_DIM}" '
                 f'text-anchor="end">{yfmt.format(yv)}</text>')
    if lo < 0 < hi:
        s.append(f'<line x1="{L}" y1="{ys(0):.1f}" x2="{w-R}" y2="{ys(0):.1f}" stroke="#cbd5e1" '
                 f'stroke-width="1.5"/>')
    for i, xl in enumerate(xlabels):
        s.append(f'<text x="{xs[i]:.1f}" y="{h-B+18}" font-size="11" fill="{C_DIM}" '
                 f'text-anchor="middle">{xl}</text>')
    for name, vs, col, dash in series_list:
        d = " ".join(f"{'M' if i == 0 else 'L'}{xs[i]:.1f},{ys(v):.1f}"
                     for i, v in enumerate(vs) if v == v)
        s.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="2.2" '
                 f'{"stroke-dasharray=\"6 4\"" if dash else ""} stroke-linejoin="round"/>')
        for i, v in enumerate(vs):
            if v == v:
                s.append(f'<circle cx="{xs[i]:.1f}" cy="{ys(v):.1f}" r="3" fill="{col}"/>')
    s.append(f'<text x="{L+iw/2:.0f}" y="{h-4}" font-size="11" fill="{C_DIM}" '
             f'text-anchor="middle">{xtitle}</text>')
    s.append(f'<text x="14" y="{T+ih/2:.0f}" font-size="11" fill="{C_DIM}" text-anchor="middle" '
             f'transform="rotate(-90 14 {T+ih/2:.0f})">{ylab}</text>')
    lg = " ".join(f'<span class="lg"><i style="background:{c}"></i>{n}</span>'
                  for n, _, c, _ in series_list)
    s.append("</svg>")
    return f'<div class="chart">{"".join(s)}</div><div class="legend">{lg}</div>'


def scatter(rows, w=680, h=340, pad=(56, 20, 46, 22)):
    L, R, T, B = pad
    iw, ih = w - L - R, h - T - B
    vs = [r["x"] for r in rows] + [r["y"] for r in rows]
    m = max(abs(min(vs)), abs(max(vs))) * 1.15
    xs = lambda v: L + iw * (v + m) / (2 * m)
    ys = lambda v: T + ih * (1 - (v + m) / (2 * m))
    s = [f'<svg viewBox="0 0 {w} {h}" style="width:100%;height:auto;font-family:inherit">']
    for k in range(-3, 4):
        v = m * k / 3
        s.append(f'<line x1="{xs(v):.1f}" y1="{T}" x2="{xs(v):.1f}" y2="{h-B}" stroke="#eef2f7"/>')
        s.append(f'<line x1="{L}" y1="{ys(v):.1f}" x2="{w-R}" y2="{ys(v):.1f}" stroke="#eef2f7"/>')
        s.append(f'<text x="{L-6}" y="{ys(v)+4:.1f}" font-size="10" fill="{C_DIM}" '
                 f'text-anchor="end">{v:.1f}</text>')
        s.append(f'<text x="{xs(v):.1f}" y="{h-B+16}" font-size="10" fill="{C_DIM}" '
                 f'text-anchor="middle">{v:.1f}</text>')
    s.append(f'<line x1="{L}" y1="{ys(0):.1f}" x2="{w-R}" y2="{ys(0):.1f}" stroke="#94a3b8" '
             f'stroke-width="1.4"/>')
    s.append(f'<line x1="{xs(0):.1f}" y1="{T}" x2="{xs(0):.1f}" y2="{h-B}" stroke="#94a3b8" '
             f'stroke-width="1.4"/>')
    # y=x 参考
    s.append(f'<path d="M{xs(-m):.1f},{ys(-m):.1f} L{xs(m):.1f},{ys(m):.1f}" stroke="#cbd5e1" '
             f'stroke-dasharray="5 4" fill="none"/>')
    for r in rows:
        col = "rgba(148,163,184,.45)" if not r["hi"] else r["col"]
        rad = 7 if r["hi"] else 3.2
        s.append(f'<circle cx="{xs(r["x"]):.1f}" cy="{ys(r["y"]):.1f}" r="{rad}" fill="{col}" '
                 f'{"stroke=\"#fff\" stroke-width=\"1.5\"" if r["hi"] else ""}/>')
        if r["hi"]:
            s.append(f'<text x="{xs(r["x"])+9:.1f}" y="{ys(r["y"])+4:.1f}" font-size="11.5" '
                     f'fill="#0f172a" font-weight="600">{r["label"]}</text>')
    s.append(f'<text x="{L+iw/2:.0f}" y="{h-6}" font-size="11.5" fill="#475569" '
             f'text-anchor="middle">IS 段头部价差 t 值</text>')
    s.append(f'<text x="15" y="{T+ih/2:.0f}" font-size="11.5" fill="#475569" text-anchor="middle" '
             f'transform="rotate(-90 15 {T+ih/2:.0f})">OOS 段头部价差 t 值</text>')
    s.append("</svg>")
    return "".join(s)


# ---------- 散点：IS vs OOS（h=20） ----------
f = scr[(scr.coverage > 0.8) & (scr.n_dates > 150)]
a = f[f.segment == "is"].set_index(["factor", "horizon"])[["head_t_nw", "category"]]
b = f[f.segment == "oos"].set_index(["factor", "horizon"])["head_t_nw"]
J = a.join(b.rename("t_oos")).reset_index()
J = J[J.horizon == 20]
HI = {"amihud_20": C_MAIN, "ep_size_dom": C_ALT, "alpha187": C_ALT, "alpha168": C_ALT,
      "alpha109": C_ALT, "ep": C_ALT, "net_margin": C_ALT, "momentum_20": C_MAIN,
      "turnover": C_MAIN, "size": C_MAIN}
rows = [dict(x=float(r.head_t_nw), y=float(r.t_oos), hi=r.factor in HI,
             col=HI.get(r.factor, C_DIM), label=r.factor.replace("_", " "))
        for r in J.itertuples() if r.head_t_nw == r.head_t_nw and r.t_oos == r.t_oos]
sc_html = scatter(rows)

# ---------- 持有期曲线 ----------
hs = [str(int(x)) for x in hc.horizon]
hc_html = line_chart(
    [("IS", hc.is_net.tolist(), C_MAIN, False),
     ("OOS", hc.oos_net.tolist(), C_ALT, False),
     ("全样本", hc.full_net.tolist(), "#0f172a", False),
     ("全样本毛超额", hc.gross_full.tolist(), "#94a3b8", True)],
    hs, ylab="净超年化 %", xtitle="持有期（交易日）")

# ---------- 净值曲线 ----------
curve = curve[~curve.index.duplicated()]
idx = curve.index
step = max(1, len(idx) // 220)
sub = curve.iloc[::step]
cv_html = line_chart(
    [("复合组合 top10", (sub.port.tolist()), C_MAIN, False),
     ("池基准（等权）", (sub.bench.tolist()), C_BENCH, False)],
    [d.strftime("%y-%m") for d in sub.index], ylab="净值（起点=1）", yfmt="{:.1f}", zero=False)

# ---------- 分年柱 ----------
years = [str(int(i)) for i in yr.index]
port = yr.iloc[:, 0].tolist()
bench = yr["池基准"].tolist()
bars = []
w, h, L, T, B = 680, 240, 52, 20, 42
ih = h - T - B
allv = port + bench
hi, lo = max(allv) * 1.15, min(allv) * 1.25
span = hi - lo
ys = lambda v: T + ih * (1 - (v - lo) / span)
gw = (w - L - 20) / len(years)
for i, y in enumerate(years):
    x0 = L + gw * i
    for j, (v, c) in enumerate([(port[i], C_MAIN), (bench[i], C_BENCH)]):
        bw = gw * 0.3
        x = x0 + gw * 0.14 + j * (bw + gw * 0.06)
        yv = ys(v)
        y0 = ys(0)
        bars.append(f'<rect x="{x:.1f}" y="{min(yv,y0):.1f}" width="{bw:.1f}" '
                    f'height="{abs(y0-yv):.1f}" fill="{c}" rx="2"/>')
        bars.append(f'<text x="{x+bw/2:.1f}" y="{(min(yv,y0)-5 if v>=0 else max(yv,y0)+13):.1f}" '
                    f'font-size="10" fill="#334155" text-anchor="middle">{v:.0f}</text>')
    bars.append(f'<text x="{x0+gw/2:.1f}" y="{h-B+18}" font-size="11.5" fill="{C_DIM}" '
                f'text-anchor="middle">{y}</text>')
bars.append(f'<line x1="{L}" y1="{ys(0):.1f}" x2="{w-20}" y2="{ys(0):.1f}" stroke="#94a3b8"/>')
yr_html = (f'<div class="chart"><svg viewBox="0 0 {w} {h}" style="width:100%;height:auto">'
           f'{"".join(bars)}</svg></div>'
           f'<div class="legend"><span class="lg"><i style="background:{C_MAIN}"></i>复合组合 top10</span>'
           f'<span class="lg"><i style="background:{C_BENCH}"></i>池基准</span></div>')

# ---------- 表格 ----------
def tbl(df, cols=None, fmt=None, hl=None, first_left=True):
    d = df[cols] if cols else df
    head = "".join(f"<th>{c}</th>" for c in d.columns)
    body = ""
    for i, r in enumerate(d.itertuples(index=False)):
        cls = ' class="hl"' if hl and i in hl else ""
        tds = ""
        for j, v in enumerate(r):
            if isinstance(v, float):
                if v != v:
                    t = "—"
                else:
                    t = fmt.format(v) if fmt else f"{v:,.2f}"
            else:
                t = str(v)
            tds += f"<td>{t}</td>"
        body += f"<tr{cls}>{tds}</tr>"
    return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


ms = pd.read_csv(OUT / "model_summary.csv")
ms["badge"] = np.where(ms.oos_at_is_h > 10, "✅ 稳健",
                       np.where(ms.oos_at_is_h > 5, "⚠️ 一般", "❌ 证伪"))
m1 = ms[["model", "best_h_is", "is_net", "oos_at_is_h", "full_at_is_h", "oos_t", "badge"]].copy()
m1.columns = ["模型", "IS 最优持有期", "IS 净超 %", "OOS 净超 %", "全样本净超 %", "OOS t", "判定"]

piv = cmp_[cmp_.seg == "OOS"].pivot(index="model", columns="horizon", values="net_ann").round(1)
piv.columns = [f"h={c}" for c in piv.columns]
piv = piv.reset_index().rename(columns={"model": "模型"})

st = stats.copy()
st.columns = ["组合", "年化 %", "夏普", "回撤 %", "换手", "净超额 %", "Δ下限", "Δ上限"]
tn = topn.copy()
tn.columns = ["持股数", "年化 %", "夏普", "回撤 %", "换手", "净超额 %"]
cs = cost.copy()
cs["cost_bp"] = cs["cost_bp"].astype(int).astype(str) + "bp"
cs.columns = ["往返成本", "毛超额 %", "净超额(全) %", "净超额(IS) %", "净超额(OOS) %"]
rb = rob.copy()
share = rb[rb.horizon == 20][["pool", "model", "is_net", "oos_net", "full_net"]].copy()
share["pool"] = share["pool"].map({"official": "官方规则池", "approx_daily": "近似池"})
share.columns = ["池", "模型", "IS 净超 %", "OOS 净超 %", "全样本净超 %"]

yrt = yr.round(1).reset_index().rename(columns={"index": "年份"})

_h = hc.rename(columns={"horizon": "持有期(交易日)", "is_net": "IS 净超 %", "oos_net": "OOS 净超 %",
                        "full_net": "全样本净超 %", "gross_full": "全样本毛超 %",
                        "turnover": "网格换手", "t_full": "t(全)", "chosen": "选中"})
_h["选中"] = np.where(_h["选中"], "★", "")
_h = _h[["持有期(交易日)", "IS 净超 %", "OOS 净超 %", "全样本净超 %", "全样本毛超 %",
         "网格换手", "t(全)", "选中"]]
hc_tbl = tbl(_h, fmt="{:.2f}")

m1_tbl = tbl(m1, fmt="{:.2f}", hl=[1])
piv_tbl = tbl(piv, fmt="{:.1f}")
tn_tbl = tbl(tn, fmt="{:.2f}")
cs_tbl = tbl(cs, fmt="{:.2f}")
share_tbl = tbl(share, fmt="{:.1f}")
yrt_tbl = tbl(yrt, fmt="{:.1f}")

pg = cfg["phase_gate_summary"]
ann = [s for s in cfg["stats"] if "10" in s["port"]][0]

html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>红利指数成分内因子湖评估与增强建模</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#f8fafc;color:#0f172a;
 font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
 line-height:1.65;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:1180px;margin:0 auto;padding:32px 24px 64px}}
header{{background:linear-gradient(135deg,#1e3a8a,#2563eb 55%,#3b82f6);color:#fff;
 border-radius:16px;padding:30px 34px;margin-bottom:26px;box-shadow:0 10px 30px rgba(37,99,235,.22)}}
header h1{{margin:0 0 8px;font-size:26px;letter-spacing:.3px}}
header .sub{{opacity:.9;font-size:13.5px}}
header .tags{{margin-top:16px;display:flex;gap:8px;flex-wrap:wrap}}
.tag{{background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.3);
 border-radius:999px;padding:3px 12px;font-size:12px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-bottom:28px}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:13px;padding:16px 18px;
 box-shadow:0 1px 3px rgba(15,23,42,.05)}}
.card .k{{font-size:12px;color:#64748b;margin-bottom:6px}}
.card .v{{font-size:25px;font-weight:700;letter-spacing:-.5px}}
.card .n{{font-size:11.5px;color:#94a3b8;margin-top:5px}}
.pos{{color:#dc2626}} .neg{{color:#059669}}
section{{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:24px 26px;
 margin-bottom:20px;box-shadow:0 1px 3px rgba(15,23,42,.05)}}
h2{{font-size:19px;margin:0 0 6px;display:flex;align-items:center;gap:10px}}
h2 .num{{background:#eff6ff;color:#2563eb;border-radius:7px;font-size:13px;
 padding:2px 9px;font-weight:700}}
h3{{font-size:15px;margin:22px 0 8px;color:#1e293b}}
p,li{{font-size:14px;color:#334155}}
.small{{font-size:12.5px;color:#64748b}}
.chart{{margin:14px 0 4px}}
.legend{{display:flex;gap:18px;flex-wrap:wrap;font-size:12.5px;color:#475569;margin-bottom:6px}}
.lg{{display:flex;align-items:center;gap:6px}}
.lg i{{width:14px;height:3px;border-radius:2px;display:inline-block}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin:12px 0}}
th{{background:#f1f5f9;color:#475569;font-weight:600;text-align:right;padding:9px 11px;
 border-bottom:1px solid #e2e8f0;white-space:nowrap}}
th:first-child,td:first-child{{text-align:left}}
td{{padding:8px 11px;text-align:right;border-bottom:1px solid #f1f5f9}}
tr.hl td{{background:#eff6ff;font-weight:650}}
tbody tr:hover td{{background:#f8fafc}}
tr.hl:hover td{{background:#dbeafe}}
.kpi{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:14px 0}}
.kpi>div{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px}}
.kpi .k{{font-size:11.5px;color:#64748b}} .kpi .v{{font-size:20px;font-weight:700}}
.warn{{background:#fff7ed;border:1px solid #fed7aa;border-left:4px solid #ea580c;
 border-radius:9px;padding:14px 18px;margin:14px 0;font-size:13.5px;color:#7c2d12}}
.ok{{background:#f0fdf4;border:1px solid #bbf7d0;border-left:4px solid #16a34a;
 border-radius:9px;padding:14px 18px;margin:14px 0;font-size:13.5px;color:#14532d}}
.note{{background:#f8fafc;border:1px dashed #cbd5e1;border-radius:9px;padding:13px 17px;
 font-size:13px;color:#475569;margin:12px 0}}
ul{{padding-left:20px}} li{{margin:4px 0}}
code{{background:#f1f5f9;padding:1.5px 6px;border-radius:5px;font-size:12.5px;color:#0f172a}}
footer{{text-align:center;color:#94a3b8;font-size:12px;padding-top:12px}}
</style></head><body><div class="wrap">

<header>
 <h1>红利指数成分内：因子湖全量评估 → 持有期与组合选择 → 增强建模</h1>
 <div class="sub">QuantLab 红利研究第 4 轮 · 236 个因子 × 5 个持有期 × IS/OOS 分段 · 2022-12 ~ 2026-09</div>
 <div class="tags"><span class="tag">官方编制规则复现池 49/100</span>
  <span class="tag">916 个交易日</span><span class="tag">含息总收益（adj_factor）</span>
  <span class="tag">重叠口径 + Newey-West</span><span class="tag">IS / OOS 单次切分</span></div>
</header>

<div class="cards">
 <div class="card"><div class="k">最终模型</div><div class="v" style="font-size:17px">池内 size+amihud top10</div>
  <div class="n">每 20 交易日调仓 · rank 等权</div></div>
 <div class="card"><div class="k">组合年化（相位中位）</div>
  <div class="v pos">27.7%</div><div class="n">池基准 10.1%</div></div>
 <div class="card"><div class="k">净超额（全样本）</div>
  <div class="v pos">+17.6%</div><div class="n">t = 2.61</div></div>
 <div class="card"><div class="k">净超额（OOS）</div>
  <div class="v pos">+16.0%</div><div class="n">t = 2.20 · 400 重叠观测</div></div>
 <div class="card"><div class="k">多相位闸门</div>
  <div class="v pos">20/20 全正</div><div class="n">Δ 13.0 ~ 21.0 pp</div></div>
 <div class="card"><div class="k">IS 最优组合的 OOS</div>
  <div class="v neg">+1.3%</div><div class="n">t = 0.38 → 被证伪</div></div>
</div>

<section>
 <h2><span class="num">01</span>研究设计</h2>
 <p>沿用中证红利官方编制方案（年调 + 缓冲区）本地复现股票池，<b>校准 49/100</b>；
 收益改用 <code>adj_factor</code> 还原含息总收益（自动处理送转）；
 因长持有期在短样本中会样本枯竭，改用<b>重叠口径毛超额 + 网格换手</b>的评估引擎——
 毛超额与持有期解耦（~850 个观测），成本按实际调仓网格估算，各期限因此可比。</p>
 <div class="kpi">
  <div><div class="k">因子池规模</div><div class="v">{scr.factor.nunique()}</div>
   <div class="small">覆盖 2022-12 起</div></div>
  <div><div class="k">评估记录</div><div class="v">{len(scr):,}</div>
   <div class="small">因子 × 期限 × 分段</div></div>
  <div><div class="k">IS / OOS</div><div class="v" style="font-size:17px">25 / 21 个月</div>
   <div class="small">2024-12 切分</div></div>
  <div><div class="k">成本假设</div><div class="v" style="font-size:17px">30bp</div>
   <div class="small">往返 · 敏感性 15~60bp</div></div>
 </div>
</section>

<section>
 <h2><span class="num">02</span>核心发现：IS 最强的因子，样本外全部归零</h2>
 <p>下图每个点是一个因子在 <b>h=20</b> 的头部价差 t 值（横轴 IS、纵轴 OOS）。
 落在对角线上方 = 样本外更强；贴近横轴 = 样本外失效。</p>
 {sc_html}
 <div class="warn"><b>红色点（价值 / 质量 / Alpha191 价值族）全部贴近横轴</b>：
 <code>ep_size_dom</code> IS t=+2.83 → OOS t=−0.07；<code>ep</code> +2.13 → +0.06；
 <code>alpha187</code> +2.12 → +0.48；<code>alpha168</code> +1.92 → +0.15。
 这些因子在 IS 段看起来非常强，样本外却完全失效。</div>
 <div class="ok"><b>蓝色点（流动性 / 规模 / 换手）贯穿两段</b>：
 <code>amihud_20</code> IS +1.16 → OOS <b>+2.11</b>（样本外更强）；
 <code>turnover</code>、<code>momentum_20</code> 在 OOS 才显现（低换手、低动量占优）。</div>
 <div class="note"><b>为什么？</b>红利池本身就是"高股息 + 连续三年分红"的价值/质量过滤器——
 池化之后 EP、净利率这类维度已被股息率筛选吸收，池内再排序没有增量信息。
 剩下的只有"流动性/规模"这一维度。</div>
</section>

<section>
 <h2><span class="num">03</span>最佳持有期限：20 个交易日</h2>
 <p>以最终模型（池内 size+amihud，top10）为例。毛超额几乎不随期限变化（灰虚线），
 期限差异几乎全部来自换手成本。</p>
 {hc_html}
 {hc_tbl}
 <ul>
  <li><b>毛超额 18.0 ~ 19.3%，对持有期不敏感</b> → size + 不流动性溢价的信号半衰期跨过 60 个交易日</li>
  <li><b>用 IS 选期限 → h=20</b>（IS 净超 +18.9%），<b>OOS 验证 +16.0%（t=2.20）</b></li>
  <li>h=5 因年换手 50 次被成本吃掉约 2.5%/年；h=20 约 1.35%/年；h=60 约 0.6%/年 → <b>净超额在 h=20~40 形成平台</b></li>
 </ul>
</section>

<section>
 <h2><span class="num">04</span>因子组合：IS 贪心被样本外证伪</h2>
 <p>每个模型先按 IS 选最优持有期，再看 OOS 表现。</p>
 {m1_tbl}
 <h3>OOS 段 · 各模型 × 持有期净超年化 %</h3>
 {piv_tbl}
 <div class="warn"><b>M4（纯 IS 贪心）</b>选出 <code>ep_size_dom + alpha187 + lockup_pressure_60</code>，
 IS 净超 <b>+18.5%（t=2.55）</b>，OOS 只剩 <b>+1.3%（t=0.38）</b>；五个持有期的 OOS 净超仅 +1.3~+5.0%。
 这是本项目内"纯样本内选因子"的第 N 次证伪。</div>
 <div class="ok"><b>M2（size+amihud，与现役生产模型同构）</b>是全部候选里<b>唯一</b>在 5 个持有期上
 IS 与 OOS 两段全部为正的模型。加因子（M3 加 momentum60、M5 加 block_premium）IS 更好看，OOS 反而更差。</div>
 <div class="note">⚠️ 另一条重要警告：某些因子（如 <code>alpha058</code>、<code>block_premium_20</code>）
 在"因子层面"两段都显著，但转成 top10 组合后净超额为负/很弱——
 尾部价差由极少数股票驱动，组合吃不到。<b>因子层 t 值不能外推为组合超额。</b></div>
</section>

<section>
 <h2><span class="num">05</span>最终模型与回测</h2>
 <div class="note"><code>score = 0.5 × rank_pct(−size) + 0.5 × rank_pct(amihud_20)</code>（池内截面）<br>
 持仓 = Top10 等权 · 调仓 = 每 20 交易日 · 成本 = 30bp/次往返</div>
 <div class="kpi">
  <div><div class="k">组合年化</div><div class="v pos">{ann['ann']:.1f}%</div>
   <div class="small">相位区间 23.0~32.2%</div></div>
  <div><div class="k">夏普</div><div class="v">{ann['sharpe']:.2f}</div>
   <div class="small">池基准 ~0.70</div></div>
  <div><div class="k">最大回撤</div><div class="v">{ann['mdd']:.1f}%</div>
   <div class="small">换手 {ann['turnover']*100:.0f}% / 20日</div></div>
  <div><div class="k">相位 Δ 区间</div>
   <div class="v pos">+{pg['min']:.1f}~+{pg['max']:.1f}</div>
   <div class="small">pp · {pg['n']}/{pg['n']} 相位全正</div></div>
 </div>
 {cv_html}
 <h3>分年收益（%，相位平均，扣成本）</h3>
 {yrt_tbl}
 <h3>TopN 与成本敏感性</h3>
 {tn_tbl}
 {cs_tbl}
 <div class="ok"><b>成本容忍度高</b>：即便按 60bp 往返（偏悲观），OOS 净超额仍有 +14.7%/年。
 推荐 <b>top10</b>（夏普 1.06 最优、回撤 −17.5%；top5 年化 37.9% 但回撤 −22.6%）。</div>
</section>

<section>
 <h2><span class="num">06</span>稳健性：换一个池定义</h2>
 <p>用月度重构的近似池（与官方成分重叠仅 25/100）复算同一批模型。</p>
 {share_tbl}
 <ul>
  <li><b>定性完全一致</b>：两个池里 IS 贪心都崩、size+amihud 都最优、amihud_20 都稳</li>
  <li><b>定量不稳健</b>：绝对水平差近一倍（18.5% vs 31.9%）→
   对外报告应采用官方规则池的保守口径，并把"依赖池定义"写入风险声明</li>
 </ul>
</section>

<section>
 <h2><span class="num">07</span>诚实风险声明</h2>
 <ul>
  <li><b>样本极短</b>：仅 3.75 年 / 916 个交易日，IS 25 个月 / OOS 21 个月，单次切分，无 walk-forward</li>
  <li><b>Regime 偏差</b>：覆盖 2023 微盘行情与 2024–2025 红利共振，缺 2019–2021 红利弱势期；
   size 腿超额高度依赖微盘 regime</li>
  <li><b>幸存者偏差</b>：kline 镜像疑似不含退市股，池构造又要求"连续三年分红"，天然排除暴雷股</li>
  <li><b>池近似度 49/100</b>：属官方规则高保真近似，非严格中证红利增强</li>
  <li><b>容量</b>：池内红利股流动性显著优于全市场微盘，容量好于现役 PROD，但 top10 单票 10% 权重
   仍需按 1% 成交额约束估算</li>
 </ul>
</section>

<section>
 <h2><span class="num">08</span>结论与建议</h2>
 <ul>
  <li><b>最佳持有期限 = 20 个交易日</b>（IS 选出，OOS +16.0% 确认）；净超额在 h=20~40 形成平台</li>
  <li><b>最佳因子组合 = 池内 size + amihud_20 等权 top10</b>，即生产模型在红利池内的子集应用，
   而非任何新挖掘的 Alpha191 / 价值因子</li>
  <li><b>IS 最优的 3 因子组合被样本外证伪</b>（+18.5% → +1.3%）</li>
  <li><b>红利池本身是有效的"价值+质量"过滤器</b>：池化后 EP / 净利率 / Alpha191 价值族全部失效，
   只剩流动性维度 alpha</li>
  <li><b>建议</b>：① DIV10 观察仓改用"每 20 交易日 + 相位平均"生产口径，与现役五模型并行记账；
   ② 本轮新因子一律不进生产；③ 后续用 1min 估实际冲击成本、改滚动 walk-forward</li>
 </ul>
</section>

<footer>QuantLab · 红利因子研究第 4 轮 · 生成于 2026-09-11 · 数据窗口 2022-12-01 ~ 2026-09-09</footer>
</div></body></html>"""

HTML.write_text(html, encoding="utf-8")
print(f"写出 {HTML} ({len(html)/1024:.0f} KB)")
