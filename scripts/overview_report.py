#!/usr/bin/env python3
"""生成五层系统总览报告（自包含 HTML）
整合 L1 数据 → L2 因子 → L3 模型 → L4 优化 → L5 决策 的完整成果。
用法: python scripts/overview_report.py
输出: reports/overview_report.html

2026-09-09 重构：模型层改为十模型并列（统一地位/统一口径），
  每模型含定位标签 + 完整介绍 + 同窗回测曲线与指标；纸面前向账本
  全模型同图；L1/L2 压缩为折叠区块。
"""
import sys
import json
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import pandas as pd

from quantlab.config import REPORTS_DIR
from quantlab.data.store import Store
from quantlab.factor import list_factors
from quantlab.model import build_composite
from quantlab.optimize import run_optimized_backtest
from quantlab.decision import load_state, generate_target

# 统一回测窗口：hf_amihud_20 分钟因子 2025-01 起才入湖，
# 全模型共用 2025-02 起的同窗口径保证可比性。
BT_START = "2025-02-01"


# ─────────────────────────── 模型登记表 ───────────────────────────
# 定位标签决定呈现地位：现役 / 候选 / 观察 / 卫星，全部为生产编制模型
def _v3_score(core: pd.DataFrame, si: pd.DataFrame) -> pd.DataFrame:
    m = core.rename(columns={"score": "s1"}).merge(
        si.rename(columns={"score": "s2"}), on=["date", "code"], how="inner")
    m["r1"] = m.groupby("date")["s1"].rank(pct=True)
    m["r2"] = m.groupby("date")["s2"].rank(pct=True)
    m["score"] = 0.6 * m["r1"] + 0.4 * m["r2"]
    return m[["date", "code", "score"]]


def build_model_scores(store: Store) -> dict[str, pd.DataFrame]:
    """一次性构建全部模型评分（共享中间合成，避免重复读因子湖）"""
    from quantlab.model.pool_select import build_dual_score
    from quantlab.data.universe import get_universe
    uni = set(get_universe("ashare_ex"))

    core = build_composite(["size", "amihud_20"], universe="ashare_ex")
    si2 = build_composite(["sue_i", "overnight_mom_20"], universe="ashare_ex")
    hfa = build_composite(["size", "amihud_20", "sue_i", "hf_amihud_20"],
                          universe="ashare_ex")
    return {
        "PROD": core.copy(),
        "PROD_SI": build_composite(
            ["size", "amihud_20", "sue_i", "overnight_mom_20"],
            universe="ashare_ex"),
        "V3_SI": _v3_score(core, si2),
        "PROD_DUAL": build_dual_score(),
        "EQ3": build_composite(["size", "amihud_20", "sue_i"],
                               universe="ashare_ex"),
        "PROD_HF": build_composite(["size", "amihud_20", "hf_amihud_20"],
                                   universe="ashare_ex"),
        "PROD_HFA": hfa.copy(),
        "EQ3_HFA_ICW": build_composite(
            ["size", "amihud_20", "sue_i", "hf_amihud_20"],
            universe="ashare_ex", method="ic_weighted"),
        "PROD_HFA_W3": hfa.copy(),          # 同分，仅调仓频率不同
        "DIV10": __import__(
            "quantlab.decision.dividend_pool", fromlist=["build_div10_score"]
        ).build_div10_score(store, universe=uni),
    }


MODEL_META = {
    # name: (定位标签, 简称, 回测口径备注)
    "PROD": ("现役", "size+amihud_20 双因子",
             "20 交易日调仓 · 逆波动加权 · top100"),
    "PROD_SI": ("观察仓", "核心+sue_i+overnight_mom_20 直加",
                "20 日调仓 · 逆波动 · top100"),
    "V3_SI": ("观察仓", "条件融合 0.6×核心+0.4×SI",
              "20 日调仓 · 逆波动 · top100"),
    "PROD_DUAL": ("观察仓", "两块式 0.9×核心+0.1×风格卫星块",
                  "20 日调仓 · 逆波动 · top100"),
    "EQ3": ("切换候选", "核心+sue_i 等权合成",
            "20 日调仓 · 逆波动 · top100"),
    "PROD_HF": ("观察仓", "核心+hf_amihud_20 分钟流动性精化",
                "20 日调仓 · 逆波动 · top100"),
    "PROD_HFA": ("切换候选", "核心+sue_i+hf_amihud_20",
                 "20 日调仓 · 逆波动 · top100"),
    "EQ3_HFA_ICW": ("稳健变体", "PROD_HFA 同因子 ICIR 加权",
                    "20 日调仓 · 逆波动 · top100"),
    "PROD_HFA_W3": ("周调口径", "PROD_HFA 周三周度调仓",
                    "每 5 交易日近似周调 · 逆波动 · top100"),
    "DIV10": ("红利卫星仓", "红利官方规则池内 rc_prod Top10 等权",
              "月末截面月调 · 等权 · top10（高股息暴露，非直接替换候选）"),
}


def _score_ic(store: Store, score: pd.DataFrame, min_n: int = 300) -> dict:
    """同窗 IC20 / ICIR20（与 prodmodel 系列脚本同口径）"""
    IC_SQL = """
    WITH fwd AS (
        SELECT date, code, c_lead / c - 1 AS fwd
        FROM (
            SELECT date, code, close * adj_factor AS c,
                   LEAD(close * adj_factor, 20) OVER (
                       PARTITION BY code ORDER BY date) AS c_lead
            FROM kline_daily)
        WHERE c_lead IS NOT NULL AND c > 0),
    j AS (
        SELECT CAST(s.date AS DATE) AS date, s.score, w.fwd
        FROM score_df s JOIN fwd w ON CAST(s.date AS DATE) = w.date
             AND s.code = w.code),
    rk AS (
        SELECT date, score, fwd,
               rank() OVER (PARTITION BY date ORDER BY score) AS rv,
               rank() OVER (PARTITION BY date ORDER BY fwd) AS rf,
               count(*) OVER (PARTITION BY date) AS n
        FROM j WHERE score IS NOT NULL)
    SELECT date, corr(rv, rf) AS ic FROM rk WHERE n >= {min_n} GROUP BY date
    """.format(min_n=min_n)
    sc = score.copy()
    sc["date"] = pd.to_datetime(sc["date"]).dt.date
    store.con.register("score_df", sc)
    ic = store.q(IC_SQL)["ic"].dropna()
    return {"ic": round(float(ic.mean()), 4) if len(ic) else None,
            "icir": (round(float(ic.mean() / ic.std()), 2)
                     if len(ic) > 2 else None)}


# ─────────────────────────── 主流程 ───────────────────────────
def l1_data(store):
    tables = {
        "kline_daily": "date",
        "finance_snapshot": "report_date",
        "instruments": "updated_at",
        "trade_calendar": "trade_date",
        "daily_snapshot": "date",
        "index_kline": "date",
    }
    rows = []
    for t, date_col in tables.items():
        try:
            if date_col:
                df = store.q(
                    f"SELECT COUNT(*) n, MAX({date_col}) mx FROM {t}")
                rows.append({"table": t, "rows": int(df.iloc[0, 0]),
                             "latest": str(df.iloc[0, 1])[:10]})
            else:
                df = store.q(f"SELECT COUNT(*) n FROM {t}")
                rows.append({"table": t, "rows": int(df.iloc[0, 0]),
                             "latest": "-"})
        except Exception:
            rows.append({"table": t, "rows": 0, "latest": "-"})
    return rows


def main():
    t0 = time.time()
    store = Store()

    # L1
    data_rows = l1_data(store)
    # L2
    flist = list_factors()

    # ── 模型层：十模型并列 ──
    scores = build_model_scores(store)
    # 共享价格宽表（全部回测只读一次 kline_daily）
    px = store.q("SELECT date, code, close*adj_factor AS c FROM kline_daily")
    px["date"] = pd.to_datetime(px["date"])
    pmat = px.pivot(index="date", columns="code", values="c").sort_index()

    model_rows = []
    payload_bt = []
    payload_bm = None
    for name, score in scores.items():
        tag, short, note = MODEL_META[name]
        n = 10 if name == "DIV10" else 100
        rb = 5 if name == "PROD_HFA_W3" else (1 if name == "DIV10" else 20)
        # DIV10 评分仅含月末截面，每个截面即调仓点 → rebalance=1
        method = "equal" if name == "DIV10" else "inverse_vol"
        try:
            r = run_optimized_backtest(score.copy(), n_stocks=n, rebalance=rb,
                                       method=method, start=BT_START,
                                       pmat=pmat)
            m = r["metrics"]
            ic = _score_ic(store, score[score["date"] >= BT_START],
                           min_n=50 if name == "DIV10" else 300)
            curve = r["curve"]
            series = [[str(pd.Timestamp(d).date()), round(float(v), 4)]
                      for d, v in zip(curve["date"], curve["nav"])]
            if payload_bm is None:
                payload_bm = [[str(pd.Timestamp(d).date()), round(float(v), 4)]
                              for d, v in zip(curve["date"], curve["nav_bm"])]
            payload_bt.append([name, series])
            model_rows.append({
                "name": name, "tag": tag, "short": short, "note": note,
                "ann": m["annual_return"], "sharpe": m["sharpe"],
                "mdd": m["max_drawdown"],
                "turnover": r["turnover_avg"],
                "ic": ic["ic"], "icir": ic["icir"], "err": None,
                "desc": _model_desc(name)})
        except Exception as e:
            model_rows.append({
                "name": name, "tag": tag, "short": short, "note": note,
                "ann": None, "sharpe": None, "mdd": None, "turnover": None,
                "ic": None, "icir": None, "err": str(e)[:120],
                "desc": _model_desc(name)})

    # 现役模型全历史视角（2022-06 起，生产评分口径）
    try:
        long_r = run_optimized_backtest(
            scores["PROD"].copy(), n_stocks=100, rebalance=20,
            method="inverse_vol", pmat=pmat)
        lc = long_r["curve"]
        payload_long = {
            "date": [str(pd.Timestamp(d).date()) for d in lc["date"]],
            "nav": [round(float(v), 4) for v in lc["nav"]],
            "bm": [round(float(v), 4) for v in lc["nav_bm"]],
        }
        long_m = long_r["metrics"]
    except Exception:
        payload_long, long_m = None, {}

    # L5 决策：目标持仓（现役）
    cur = load_state()
    tgt = generate_target(scores["PROD"])

    # 纸面前向账本：全模型（共享价格宽表）
    from quantlab.decision.tracker import _mark_to_market, paper_summary
    sig_all = store.q(
        "SELECT model, date, code, weight FROM signal_portfolio_multi "
        "ORDER BY model, date")
    paper_curves: dict[str, pd.DataFrame] = {}
    if not sig_all.empty:
        for mdl, g in sig_all.groupby("model"):
            c = _mark_to_market(g[["date", "code", "weight"]], px=px)
            if not c.empty:
                paper_curves[mdl] = c
    prod_curve = paper_curves.get("PROD")

    # 拥挤度监控
    try:
        from quantlab.factor.crowding import monitor_pool
        _, alerts, gate = monitor_pool()
        gate_w = gate.get("w_current")
    except Exception:
        alerts, gate_w = [], None

    html = render(data_rows, flist, model_rows, payload_bt, payload_bm,
                  payload_long, long_m, cur, tgt, paper_curves, prod_curve,
                  alerts, gate_w)
    out = REPORTS_DIR / "overview_report.html"
    out.write_text(html, encoding="utf-8")
    print(f"报告已生成: {out} ({time.time()-t0:.0f}s)")


def _model_desc(name: str) -> str:
    from quantlab.decision.tracker import TRACKED_MODELS
    return TRACKED_MODELS.get(name, "")


def _latest_snapshot(model: str | None):
    """取模型最新信号日目标持仓快照（model=None 为生产账本）→ (日期, DataFrame)"""
    store = Store()
    if model is None:
        df = store.q(
            "SELECT date, code, name, weight FROM signal_portfolio "
            "WHERE date = (SELECT MAX(date) FROM signal_portfolio)")
    else:
        df = store.q(
            "SELECT date, code, name, weight FROM signal_portfolio_multi "
            "WHERE model=? AND date="
            "(SELECT MAX(date) FROM signal_portfolio_multi WHERE model=?)",
            [model, model])
    if df.empty:
        return None
    df = df.sort_values("weight", ascending=False).reset_index(drop=True)
    return str(pd.Timestamp(df["date"].iloc[0]).date()), df


# ─────────────────────────── 渲染 ───────────────────────────
TAG_CLS = {"现役": "tag-live", "切换候选": "tag-cand", "稳健变体": "tag-cand",
           "观察仓": "tag-watch", "周调口径": "tag-watch",
           "红利卫星仓": "tag-sat"}


def render(data_rows, flist, model_rows, payload_bt, payload_bm,
           payload_long, long_m, cur, tgt, paper_curves, prod_curve,
           alerts, gate_w):
    data_tr = "".join(
        f"<tr><td><code>{r['table']}</code></td><td>{r['rows']:,}</td>"
        f"<td>{r['latest']}</td></tr>" for r in data_rows)
    flist_tr = "".join(
        f"<tr><td><code>{f['name']}</code></td><td>{f['category']}</td>"
        f"<td>{f['description']}</td></tr>" for f in flist.to_dict("records"))

    # ── 模型一览（地位平等，统一格式）──
    intro_rows = ""
    for m in model_rows:
        cls = TAG_CLS.get(m["tag"], "tag-watch")
        intro_rows += (
            f"<tr><td><b>{m['name']}</b></td>"
            f"<td><span class='tag {cls}'>{m['tag']}</span></td>"
            f"<td>{m['short']}</td><td class='lt'>{m['desc']}</td></tr>")

    # ── 同窗回测指标表 ──
    def _fmt(v, pct=True, na="—"):
        if v is None:
            return na
        return f"{v:.1%}" if pct else f"{v:.2f}"

    bt_rows = ""
    for m in model_rows:
        if m["err"]:
            bt_rows += (f"<tr><td><b>{m['name']}</b></td>"
                        f"<td colspan='7' class='neg'>回测失败: {m['err']}</td></tr>")
            continue
        sharpe_cls = "pos" if (m["sharpe"] or 0) >= 1 else ""
        bt_rows += (
            f"<tr><td><b>{m['name']}</b>"
            f"<span class='tag {TAG_CLS.get(m['tag'], 'tag-watch')}'>{m['tag']}</span>"
            f"</td><td class='pos'>{_fmt(m['ann'])}</td>"
            f"<td class='{sharpe_cls}'>{_fmt(m['sharpe'], pct=False)}</td>"
            f"<td class='neg'>{_fmt(m['mdd'])}</td>"
            f"<td>{_fmt(m['turnover'], pct=False)}</td>"
            f"<td>{_fmt(m['ic'], pct=False)}</td>"
            f"<td>{_fmt(m['icir'], pct=False)}</td></tr>")

    # ── 分模型介绍卡片 ──
    detail_blocks = ""
    for i, m in enumerate(model_rows):
        if m["err"]:
            perf_line = f"<span class='neg'>回测失败: {m['err']}</span>"
        else:
            perf_line = (f"年化 <b class='pos'>{_fmt(m['ann'])}</b> · "
                         f"夏普 <b>{_fmt(m['sharpe'], pct=False)}</b> · "
                         f"回撤 <b class='neg'>{_fmt(m['mdd'])}</b> · "
                         f"IC20 <b>{_fmt(m['ic'], pct=False)}</b> · "
                         f"ICIR20 <b>{_fmt(m['icir'], pct=False)}</b>")
        detail_blocks += (
            f"<details><summary><span class='tag "
            f"{TAG_CLS.get(m['tag'], 'tag-watch')}'>{m['tag']}</span>"
            f"　<b>{m['name']}</b>　{m['short']}"
            f"<span class='muted'>{m['note']}</span></summary>"
            f"<p style='margin:8px 0 4px'>{perf_line}</p>"
            f"<p class='sub' style='margin:0'>{m['desc']}</p></details>")

    # ── 纸面前向账本 ──
    paper_payload = []
    for name, c in paper_curves.items():
        pts = [[str(pd.Timestamp(d).date()), round(float(v), 4)]
               for d, v in zip(c["date"], c["nav"])]
        paper_payload.append([name, pts])
    paper_meta = {}
    prod_cum = None
    for name, c in paper_curves.items():
        cum = float(c["nav"].iloc[-1] - 1)
        if name == "PROD":
            prod_cum = cum
        paper_meta[name] = {"days": len(c), "cum": cum,
                            "start": str(pd.Timestamp(c['date'].iloc[0]).date()),
                            "end": str(pd.Timestamp(c['date'].iloc[-1]).date())}
    paper_rows = ""
    for m in model_rows:
        name = m["name"]
        info = paper_meta.get(name)
        if info is None:
            paper_rows += (f"<tr><td><b>{name}</b></td>"
                           f"<td>积累中（快照不足 2 期）</td><td>—</td><td>—</td></tr>")
            continue
        rel = (info["cum"] - prod_cum
               if prod_cum is not None and name != "PROD" else None)
        rel_s = "—" if rel is None else f"{rel:+.2%}"
        cum_cls = "pos" if info["cum"] >= 0 else "neg"
        paper_rows += (
            f"<tr><td><b>{name}</b>"
            f"<span class='tag {TAG_CLS.get(m['tag'], 'tag-watch')}'>{m['tag']}</span></td>"
            f"<td>{info['days']} 日（{info['start']}~）</td>"
            f"<td class='{cum_cls}'>{info['cum']:+.2%}</td>"
            f"<td>{rel_s}</td></tr>")
    track_days = paper_meta.get("PROD", {}).get("days", 0)

    # ── 各模型目标持仓明细 ──
    hold_blocks = []
    for m in model_rows:
        name = m["name"]
        snap = _latest_snapshot(None if name == "PROD" else name)
        if snap is None:
            continue
        d, h = snap
        rows_html = "".join(
            f"<tr><td><code>{r['code']}</code></td><td>{r['name']}</td>"
            f"<td>{r['weight']:.2%}</td></tr>" for r in h.to_dict("records"))
        hold_blocks.append(
            f"<details><summary><span class='tag "
            f"{TAG_CLS.get(m['tag'], 'tag-watch')}'>{m['tag']}</span>"
            f"　<b>{name}</b>　{len(h)} 只 · 信号日 {d}"
            f"<span class='muted'>{m['short']}</span></summary>"
            "<table><thead><tr><th>代码</th><th>名称</th><th>权重</th></tr></thead>"
            f"<tbody>{rows_html}</tbody></table></details>")
    hold_section = (
        '<h2>六、各模型目标持仓（最新信号日）</h2>'
        '<p class="sub">每日流水线决策步骤记录的各模型目标持仓，按权重降序，'
        '点击展开明细。PROD_HFA_W3 仅周三记录（周度调仓口径）；'
        'DIV10 为月末截面月调。</p>'
        + "".join(hold_blocks))

    # ── 拥挤度 ──
    if alerts:
        alert_html = ", ".join(alerts)
    else:
        alert_html = "无拥挤预警"
    gate_s = f"（门控 w={gate_w:.2f}）" if gate_w is not None else ""
    crowd_section = (
        '<h2>七、风险监控</h2>'
        f'<p>拥挤度预警（≥80% 分位）：{alert_html}{gate_s}'
        '　<span class="muted">预警=关注暴露，非减仓信号</span></p>')

    # ── 头部卡片 ──
    sig_day = max((m for m in paper_meta.values()),
                  key=lambda x: x["end"], default=None)
    sig_day_s = sig_day["end"] if sig_day else "—"

    long_section = ""
    long_cards = ""
    if payload_long:
        long_cards = (
            f"<div class='cards'>"
            f"<div class='card'><div class='l'>现役 PROD 全历史年化</div>"
            f"<div class='v pos'>{long_m.get('annual_return', 0):.1%}</div></div>"
            f"<div class='card'><div class='l'>全历史夏普</div>"
            f"<div class='v'>{long_m.get('sharpe', 0):.2f}</div></div>"
            f"<div class='card'><div class='l'>全历史最大回撤</div>"
            f"<div class='v neg'>{long_m.get('max_drawdown', 0):.1%}</div></div>"
            f"</div>")
        long_section = (
            '<h3>现役 PROD 全历史视角（2022-06 起，非同窗口径）</h3>'
            '<p class="sub">同窗对比受分钟因子覆盖约束从 2025-02 起；'
            '本图为现役模型的全历史参考曲线（含 2023 弱年）。</p>'
            + long_cards + '<div id="chartLong" class="chart"></div>')

    top10 = tgt.head(10)[["code", "name", "weight"]]
    top10_tr = "".join(
        f"<tr><td><code>{r['code']}</code></td><td>{r['name']}</td>"
        f"<td>{r['weight']:.2%}</td></tr>" for r in top10.to_dict("records"))

    payload = json.dumps({
        "bt": payload_bt, "bm": payload_bm,
        "long": payload_long, "paper": paper_payload,
    }, ensure_ascii=False)

    return TMPL.format(
        n_factors=len(flist), n_models=len(model_rows),
        data_tr=data_tr, flist_tr=flist_tr,
        intro_rows=intro_rows, bt_rows=bt_rows, detail_blocks=detail_blocks,
        paper_rows=paper_rows, hold_section=hold_section,
        crowd_section=crowd_section, long_section=long_section,
        top10_tr=top10_tr, track_days=track_days,
        sig_day=sig_day_s, tgt_n=len(tgt),
        payload=payload)


TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QuantLab 每日总览报告</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
:root{{--bg:#0f1115;--card:#171a21;--text:#e6e8ec;--muted:#9aa3b2;
--line:#2a2f3a;--red:#e24b4a;--green:#1d9e75;--accent:#378add;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--text);font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.6;padding:40px 24px;}}
.wrap{{max-width:1180px;margin:0 auto;}}
h1{{font-size:26px;font-weight:600;}}
h2{{font-size:18px;font-weight:600;margin:40px 0 16px;padding-left:12px;border-left:3px solid var(--accent);}}
h3{{font-size:15px;font-weight:600;margin:24px 0 10px;color:#b9c2d0;}}
.sub{{color:var(--muted);font-size:14px;margin-top:6px;}}
.cards{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:24px 0;}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;}}
.card .l{{color:var(--muted);font-size:13px;}}
.card .v{{font-size:22px;font-weight:600;margin-top:4px;}}
table{{width:100%;border-collapse:collapse;margin:12px 0;font-size:13px;}}
th,td{{padding:8px 12px;text-align:right;border-bottom:1px solid var(--line);}}
th{{color:var(--muted);font-weight:500;}}
td:first-child,th:first-child{{text-align:left;}}
td.lt{{text-align:left;color:var(--muted);font-size:12px;line-height:1.5;}}
code{{font-family:ui-monospace,Consolas,monospace;color:#7fb2e5;}}
.chart{{width:100%;height:420px;margin:8px 0;}}
.note{{color:var(--muted);font-size:12px;margin-top:8px;}}
.layer{{display:inline-block;background:#171a21;border:1px solid #2a2f3a;border-radius:6px;
padding:2px 10px;font-size:12px;color:#7fb2e5;margin-bottom:16px;}}
details{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 16px;margin:10px 0;}}
summary{{cursor:pointer;font-size:14px;}}
summary:hover{{color:#7fb2e5;}}
details table{{margin:10px 0 4px;}}
.muted{{color:var(--muted);font-size:12px;font-weight:400;margin-left:8px;}}
.tag{{display:inline-block;border-radius:5px;padding:1px 8px;font-size:11px;margin-left:6px;vertical-align:1px;}}
.tag-live{{background:#3a1d22;color:#ff8a8a;border:1px solid #5c2a30;}}
.tag-cand{{background:#15301f;color:#5ad492;border:1px solid #24513a;}}
.tag-watch{{background:#1a2433;color:#7fb2e5;border:1px solid #2b3d55;}}
.tag-sat{{background:#322a15;color:#e0b45c;border:1px solid #554622;}}
.pos{{color:var(--red);}}
.neg{{color:var(--green);}}
</style>
</head>
<body>
<div class="wrap">
<h1>QuantLab 每日总览报告</h1>
<p class="sub">A 股量化研究系统 · 十模型并列编制 · 信号日 {sig_day} · 前向账本已实现 {track_days} 交易日</p>

<div class="cards">
  <div class="card"><div class="l">信号日</div><div class="v" style="font-size:18px">{sig_day}</div></div>
  <div class="card"><div class="l">编制模型</div><div class="v">{n_models}</div></div>
  <div class="card"><div class="l">现役模型</div><div class="v" style="font-size:18px">PROD</div></div>
  <div class="card"><div class="l">双闸门裁决</div><div class="v" style="font-size:18px">2026-12</div></div>
  <div class="card"><div class="l">现役目标持仓</div><div class="v">{tgt_n} 只</div></div>
</div>

<span class="layer">L1 数据层</span>
<h2>一、数据水位</h2>
<details open><summary>核心表水位（点击折叠）</summary>
<table>
<thead><tr><th>表</th><th>行数</th><th>最新日期</th></tr></thead>
<tbody>{data_tr}</tbody>
</table></details>
<details><summary>内置因子清单（{n_factors} 个，点击展开）</summary>
<table>
<thead><tr><th>因子</th><th>类别</th><th>说明</th></tr></thead>
<tbody>{flist_tr}</tbody>
</table>
<p class="sub">78 个为每日流水线编制因子；另有 180 个 Alpha191 研究因子按需批量计算（不入日常编制）。</p>
</details>

<span class="layer">L3 模型层 · 十模型并列</span>
<h2>二、模型一览</h2>
<p class="sub">全部生产编制模型统一地位呈现：现役=当前实盘口径；切换候选=双闸门裁决对象；
观察仓=积累前向样本；周调口径/卫星仓=差异化配置研究。各模型相互独立，按统一格式介绍。</p>
<table>
<thead><tr><th>模型</th><th>定位</th><th>构成</th><th>介绍</th></tr></thead>
<tbody>{intro_rows}</tbody>
</table>

<h2>三、同窗回测对比（{bt_start} 起 · top100 · 中证1000 基准）</h2>
<p class="sub">统一窗口受分钟因子覆盖约束（hf_amihud_20 于 2025-01 入湖）。
点击图例可单独聚焦任一模型；DIV10 为月调 top10 等权、高股息暴露，曲线仅作横向参考。</p>
<div id="chartBT" class="chart" style="height:460px"></div>
<table>
<thead><tr><th>模型</th><th>年化</th><th>夏普</th><th>最大回撤</th>
<th>换手/期</th><th>IC20</th><th>ICIR20</th></tr></thead>
<tbody>{bt_rows}</tbody>
</table>
<details>
<h3 style="margin-top:4px">分模型介绍</h3>
{detail_blocks}
</details>
{long_section}

<span class="layer">L5 决策层</span>
<h2>四、当前目标持仓 Top10（现役 PROD）</h2>
<p class="sub">size + amihud_20 top100 + 逆波动加权 + 个股/行业风控，共 {tgt_n} 只；
全模型持仓见第四节。</p>
<table>
<thead><tr><th>代码</th><th>名称</th><th>权重</th></tr></thead>
<tbody>{top10_tr}</tbody>
</table>

<h2>五、纸面前向账本（全模型）</h2>
<p class="sub">信号快照记录于每日流水线，逐日盯市，与回测独立的前向验证账本。
账本自 2026-09-07 同日起算，满 60 交易日后（约 2026-12）双闸门裁决；
所有模型同图呈现，点击图例聚焦。</p>
<div id="chartPaper" class="chart"></div>
<table>
<thead><tr><th>模型</th><th>纸面天数</th><th>累计收益</th><th>相对现役 PROD</th></tr></thead>
<tbody>{paper_rows}</tbody>
</table>

{hold_section}

{crowd_section}

<p class="note" style="margin-top:24px">本报告由 QuantLab 五层流水线自动生成（scripts/overview_report.py，十模型并列版）。仅供研究，不构成投资建议。</p>
</div>

<script>
const P = {payload};
const COLORS = {{PROD:'#e24b4a','PROD_SI':'#e08c4a','V3_SI':'#d9c04a','PROD_DUAL':'#9ecf4a',
'EQ3':'#4ad499','PROD_HF':'#4ac9d4','PROD_HFA':'#4a8fe0','EQ3_HFA_ICW':'#7a6ce0',
'PROD_HFA_W3':'#c66ce0',DIV10:'#e0b45c'}};
const gray='#9aa3b2', line='#2a2f3a';
const axis={{axisLine:{{lineStyle:{{color:line}}}},axisLabel:{{color:gray}},splitLine:{{lineStyle:{{color:line}}}}}};
const BT_START_JS = '{bt_start}';

// ── 同窗回测：全模型曲线 ──
(function(){{
  const series = P.bt.map(([name, pts]) => ({{
    name: name, type: 'line', smooth: true, symbol: 'none',
    lineStyle: {{width: name==='PROD' ? 2.5 : 1.5, color: COLORS[name] || '#888'}},
    itemStyle: {{color: COLORS[name] || '#888'}},
    emphasis: {{focus: 'series', lineStyle: {{width: 3}}}},
    data: pts
  }}));
  series.push({{name: '中证1000', type: 'line', smooth: true, symbol: 'none',
    lineStyle: {{width: 1.5, color: gray, type: 'dashed'}},
    itemStyle: {{color: gray}}, emphasis: {{focus: 'series'}},
    data: P.bm || []}});
  echarts.init(document.getElementById('chartBT')).setOption({{
    grid:{{left:60,right:30,top:60,bottom:40}},
    tooltip:{{trigger:'axis',order:'valueDesc'}},
    legend:{{textStyle:{{color:gray}},top:0,type:'scroll'}},
    xAxis:{{type:'time',...axis}},
    yAxis:{{type:'value',...axis,scale:true}},
    series
  }});
}})();

// ── 现役全历史 ──
(function(){{
  if(!P.long) return;
  echarts.init(document.getElementById('chartLong')).setOption({{
    grid:{{left:60,right:30,top:40,bottom:40}},
    tooltip:{{trigger:'axis'}},
    legend:{{textStyle:{{color:gray}},top:0}},
    xAxis:{{type:'time',...axis}},
    yAxis:{{type:'value',...axis,scale:true}},
    series:[
      {{name:'PROD',type:'line',smooth:true,symbol:'none',
        lineStyle:{{width:2,color:'#e24b4a'}},
        data:P.long.date.map((d,i)=>[d,P.long.nav[i]])}},
      {{name:'中证1000',type:'line',smooth:true,symbol:'none',
        lineStyle:{{width:1.5,color:gray,type:'dashed'}},
        data:P.long.date.map((d,i)=>[d,P.long.bm[i]])}}
    ]
  }});
}})();

// ── 纸面前向账本：全模型 ──
(function(){{
  if(!P.paper || !P.paper.length) return;
  const series = P.paper.map(([name, pts]) => ({{
    name: name, type: 'line', smooth: false, symbol: 'circle', symbolSize: 5,
    lineStyle: {{width: name==='PROD' ? 2.5 : 1.5, color: COLORS[name] || '#888'}},
    itemStyle: {{color: COLORS[name] || '#888'}},
    emphasis: {{focus: 'series'}},
    data: pts
  }}));
  echarts.init(document.getElementById('chartPaper')).setOption({{
    grid:{{left:60,right:30,top:60,bottom:40}},
    tooltip:{{trigger:'axis'}},
    legend:{{textStyle:{{color:gray}},top:0,type:'scroll'}},
    xAxis:{{type:'time',...axis}},
    yAxis:{{type:'value',...axis,scale:true}},
    series
  }});
}})();
</script>
</body>
</html>
""".replace("{bt_start}", BT_START)


if __name__ == "__main__":
    main()
