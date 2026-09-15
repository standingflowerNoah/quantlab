# -*- coding: utf-8 -*-
"""全因子总览看板构建器（factor universe dashboard）
======================================================================
把因子库全部因子的审计快照 / FDR / 机制登记 / L0 近端动态聚成
一个单文件 HTML 总览看板——与 build_dashboard.py（单因子七区深钻）
互补：先在总览看板横向扫描定位，再点进单因子看板纵向深挖。

用法：
  python scripts/factor_eval/build_universe_dashboard.py \
      [--pool ashare_ex] [--recent 60] [--start 2022-01-01]

产出：
  docs/factor_universe.html（单文件无外部依赖，浏览器直接打开）
  reports/factor_eval/index.json 登记 type=dashboard name=factor_universe

数据源全只读（不碰主库 DuckDB，可与流水线并行）：
  data/lake/factor_audit/*.json      PIT/IC 快照（v2 双类型 schema）
  reports/fdr_factor_ranking.json    多重检验 t_adj/q/HLZ
  quantlab.factor.registry           类别/频率/描述/机制登记
  data/lake/factor_metric_daily      L0 近 N 日动态 IC/ICIR（双池）
  reports/factor_eval/index.json     最新评估红绿灯
  docs/factor_center_*.html          单因子看板跳转链接
"""
from __future__ import annotations

import argparse
import ast
import inspect
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:  # Windows 控制台 GBK 兜底
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

from batch_metrics import (  # noqa: E402
    AUDIT_DIR, load_audit, load_fdr, load_latest_reports, load_recent,
    load_rationales,
)

DOCS = ROOT / "docs"
INDEX = ROOT / "reports" / "factor_eval" / "index.json"


def collect_rows() -> list[dict]:
    sys.path.insert(0, str(ROOT))
    from quantlab.factor.registry import list_factors  # noqa: PLC0415
    reg_df = list_factors()
    reg = {r["name"]: r for _, r in reg_df.iterrows()}

    fdr = load_fdr()
    reports = load_latest_reports()
    names = sorted(Path(f).stem for f in AUDIT_DIR.glob("*.json"))
    center_exists = {
        n: (DOCS / f"factor_center_{n}.html").exists() for n in names}

    rows = []
    for n in names:
        a = load_audit(n)
        if a is None:
            continue
        ic = a.get("checks", {}).get("ic", {}) or {}
        pit = a.get("checks", {}).get("pit", {}) or {}
        f = fdr.get(n, {})
        rep = reports.get(n, {})
        g = reg.get(n, {})
        cov = a.get("checks", {}).get("coverage", {}) or {}
        rows.append({
            "factor": n,
            "category": g.get("category", "未分类"),
            "freq": g.get("freq", ""),
            "direction": ic.get("direction"),
            "ric1": ic.get("rank_ic1"), "ric5": ic.get("rank_ic5"),
            "ric20": ic.get("rank_ic20"),
            "pic5": ic.get("pearson_ic5"), "pic20": ic.get("pearson_ic20"),
            "icir5": ic.get("rank_icir5"), "icir20": ic.get("rank_icir20"),
            "win5": ic.get("win5"),
            "n_days": cov.get("n_days"),
            "t_adj": f.get("t"), "q": f.get("q"),
            "harvey": bool(f.get("harvey_pass")) if f.get("harvey_pass") is not None else None,
            "pit": pit.get("status") or a.get("verdict"),
            "light": rep.get("light"),
            "rationale": (g.get("economic_rationale") or "未登记"),
            "desc": g.get("description") or "",
            "latest_date": str(a.get("latest_date") or ""),
            "has_center": center_exists.get(n, False),
        })
    return rows


def attach_recent(rows: list[dict], pool: str, n_days: int, prefix: str) -> None:
    rec = load_recent(pool, n_days)
    for r in rows:
        d = rec.get(r["factor"], {})
        r[f"{prefix}_ic20"] = d.get("recent_ic20")
        r[f"{prefix}_icir20"] = d.get("recent_icir20")
        r[f"{prefix}_days"] = d.get("recent_days")
        r[f"{prefix}_date"] = d.get("recent_date", "")
        # 衰减比 = |近端 ICIR20| / |全史 ICIR20|（全史为审计快照口径）
        a, b = r[f"{prefix}_icir20"], r.get("icir20")
        if a is not None and b not in (None, 0):
            r[f"{prefix}_decay"] = round(abs(a) / max(abs(b), 1e-9), 2)
        else:
            r[f"{prefix}_decay"] = None


def load_recent_full(pool: str, n_days: int) -> dict[str, dict]:
    """L0 近 n_days 日全指标聚合（明细表 1Y 口径数据源）。

    返回 {factor: {ric1,ric5,ric20,icir20,win5,q1_ann,q5_ann,ls_ann,days,date}}。
    口径与 metric_store 一致：q5 = 因子值最高组、q1 = 最低组（NTILE(5)）；
    q*_ret = 组内 h=1 前瞻绝对收益（复权收盘）均值；q*_ann = 日均 × 244 简单年化。
    """
    from quantlab.factor.metric_store import read_l0
    try:
        df = read_l0(pool)
    except Exception as e:  # pragma: no cover
        print(f"[warn] L0 读取失败：{e}", file=sys.stderr)
        return {}
    if df.empty:
        return {}
    recent = df.sort_values("date").groupby("factor").tail(n_days)
    out: dict[str, dict] = {}
    for f, sub in recent.groupby("factor"):
        d: dict = {}
        ic20 = sub["rank_ic20"].dropna()
        d["days"] = int(len(ic20))
        d["date"] = str(sub["date"].max())
        for col, key in (("rank_ic1", "ric1"), ("rank_ic5", "ric5"),
                         ("rank_ic20", "ric20")):
            v = sub[col].dropna()
            d[key] = round(float(v.mean()), 4) if len(v) >= 5 else None
        d["icir20"] = (round(float(ic20.mean() / ic20.std()), 3)
                       if len(ic20) >= 5 and ic20.std() > 0 else None)
        v5 = sub["rank_ic5"].dropna()
        d["win5"] = round(float((v5 > 0).mean()), 3) if len(v5) >= 5 else None
        for q in (1, 5):
            v = sub[f"q{q}_ret"].dropna()
            d[f"q{q}_ann"] = (round(float(v.mean()) * 244, 4)
                              if len(v) >= 60 else None)
        vl = sub["ls_ret"].dropna()
        d["ls_ann"] = (round(float(vl.mean()) * 244, 4)
                       if len(vl) >= 60 else None)
        out[f] = d
    return out


# ── 计算逻辑/源码收集（第四区块：每个因子的具体计算逻辑或代码） ──────────
IMPL_SCRIPTS = sorted(
    set(ROOT.glob("scripts/phase*.py"))
    | {ROOT / "scripts" / n for n in (
        "overnight_q3_factor.py", "lgbm_all.py", "lgbm_domain.py",
        "lgbm_rep168.py", "lgbm_top64.py", "gru_seq.py")}
    | {ROOT / "panda_pull" / "replicate_factors.py"}
    | {ROOT / "quantlab" / "factor" / "library" / "a191_composite.py"}
)
# ML 分数因子 / 动态拼名因子的训练或实现脚本精确映射（防跨脚本互引误锚）
NAME_HINT = {
    "lgbm_all_score": "scripts/lgbm_all.py",
    "lgbm_domain_score": "scripts/lgbm_domain.py",
    "lgbm_rep168_score": "scripts/lgbm_rep168.py",
    "lgbm_top64_score": "scripts/lgbm_top64.py",
    "gru_seq_score": "scripts/gru_seq.py",
    "radar_up_20": "scripts/phase1b_radar.py",
    "radar_down_20": "scripts/phase1b_radar.py",
    "radar_vol_20": "scripts/phase1b_radar.py",
}
_KIND_LABEL = {"class": "类源码", "alpha": "公式源码", "func": "函数源码",
               "script": "脚本实现", "model": "模型训练"}


def _docstring_head(path: Path) -> str:
    """脚本模块 docstring 首个非空行（作为实现说明）"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        doc = ast.get_docstring(tree) or ""
        for ln in doc.splitlines():
            ln = ln.strip()
            if ln:
                return ln
    except Exception:
        pass
    return ""


def _find_anchor(name: str) -> str | None:
    """在候选实现脚本里定位因子名的赋值/定义行（引用行不算），返回 文件:行号"""
    nm = r"['\"]" + re.escape(name) + r"['\"]"
    pat = re.compile(
        nm + r"(\s*\]\s*=|\s*:)"                          # ev["name"] = / {"name": ...
        r"|\bdef\s+\w*" + re.escape(name) + r"\w*\s*\("   # def ...name...(
        r"|append_factor_lake\(\s*" + nm)                 # append_factor_lake("name"
    ordered = IMPL_SCRIPTS
    hint = NAME_HINT.get(name)
    if hint:
        p = ROOT / hint
        ordered = [p] + [q for q in IMPL_SCRIPTS if q != p]
    for p in ordered:
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for i, ln in enumerate(lines, 1):
            if pat.search(ln):
                return f"{p.relative_to(ROOT).as_posix()}:{i}"
    # fallback：strict 形态未命中（动态拼名/落湖调用），在 hint 文件里取
    # 因子名字面量首现行
    if hint:
        try:
            lines = (ROOT / hint).read_text(encoding="utf-8").splitlines()
            lit = re.compile(re.escape(name))
            for i, ln in enumerate(lines, 1):
                if lit.search(ln):
                    return f"{hint}:{i}"
        except Exception:
            pass
    return None


def _func_src(path: Path, names: set[str]) -> dict[str, str]:
    """ast 抽取文件内指定函数名的源码（pa_* 复刻等模块级函数因子）"""
    out: dict[str, str] = {}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        src = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return out
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name in names:
            seg = "\n".join(src[node.lineno - 1: node.end_lineno])
            out[node.name] = seg
    return out


def collect_code(names: list[str]) -> dict[str, dict]:
    """为每个因子收集计算逻辑来源：
    kind=class  registry 注册类源码（含 SQL/compute）
    kind=alpha  Alpha191 公式函数源码 + confidence（high/mid）
    kind=func   模块级函数源码（panda_pull/replicate_factors.py 复刻）
    kind=script 研报复刻批量脚本（公式内嵌 main()，给 文件:行号 锚点+说明）
    kind=model  ML 模型分数（训练脚本锚点+模型说明）
    """
    from quantlab.factor.registry import all_factors
    reg = all_factors()
    try:
        from quantlab.factor.alpha191_formulas import ALPHAS
    except Exception:
        ALPHAS = {}

    need_func = {n for n in names if n not in reg and n not in ALPHAS}
    func_src: dict[str, str] = {}
    for p in IMPL_SCRIPTS:
        if not need_func:
            break
        func_src.update(_func_src(p, need_func))

    out: dict[str, dict] = {}
    for n in names:
        if n in reg:
            f = reg[n]
            try:
                src = inspect.getsource(type(f))
            except Exception:
                src = None
            out[n] = {
                "kind": "class",
                "src": src,
                "anchor": f"quantlab/factor/{type(f).__module__.split('.')[-1]}.py",
                "note": f"{type(f).__name__}（registry 注册实现，模块 "
                        f"{type(f).__module__.split('.')[-1]}）",
            }
        elif n in ALPHAS:
            fn, conf = ALPHAS[n]
            try:
                src = inspect.getsource(fn)
            except Exception:
                src = None
            out[n] = {
                "kind": "alpha", "src": src, "conf": conf,
                "anchor": "quantlab/factor/alpha191_formulas.py",
                "note": "国泰君安 Alpha191 研报公式转译（宽表算子表达式，"
                        f"转译置信度 {conf}）",
            }
        elif n in func_src:
            out[n] = {
                "kind": "func", "src": func_src[n],
                "anchor": "panda_pull/replicate_factors.py",
                "note": "PandaAI 平台因子本地复刻（panda_pull/replicate_factors.py）",
            }
        else:
            anchor = _find_anchor(n)
            note = ""
            if anchor:
                p = ROOT / anchor.split(":")[0]
                note = _docstring_head(p)
                if "lgbm" in n or "gru" in n:
                    kind, label = "model", "模型训练"
                else:
                    kind, label = "script", "脚本实现"
            else:
                kind, label = "script", "脚本实现"
            out[n] = {"kind": kind, "src": None, "anchor": anchor,
                      "note": note or "实现内嵌于批量脚本（公式未单点函数化）"}
    return out


def build_html(rows: list[dict], X: dict) -> str:
    payload = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>因子库总览 · __N__ 因子</title>
<style>
:root{
  --bg:#1a1a1a; --panel:#232323; --panel2:#2a2a2a; --line:#3a3a3a;
  --txt:#e8e8e8; --sub:#9a9a9a; --gold:#e6b455; --red:#e05d5d;
  --green:#5dc98e; --blue:#5da9e0; --purple:#b48ee6; --cyan:#55c4c4;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);
  font-family:"PingFang SC","Microsoft YaHei","Segoe UI",sans-serif;
  font-size:14px;line-height:1.6;padding-bottom:60px}
.wrap{max-width:1280px;margin:0 auto;padding:0 20px}
header{padding:26px 0 10px;border-bottom:1px solid var(--line)}
h1{font-size:21px;font-weight:700}
h1 .badge{font-size:11px;background:var(--gold);color:#1a1a1a;border-radius:4px;
  padding:2px 8px;vertical-align:3px;margin-left:10px;font-weight:700}
.meta{color:var(--sub);font-size:12px;margin-top:6px}
h2{font-size:16px;font-weight:700;margin:30px 0 6px}
.desc{color:var(--sub);font-size:12px;margin-bottom:12px}
.grid{display:grid;gap:12px}
.g3{grid-template-columns:1fr 1fr 1fr}
.g4{grid-template-columns:repeat(4,1fr)}
.g6{grid-template-columns:repeat(6,1fr)}
@media(max-width:960px){.g3,.g4,.g6{grid-template-columns:1fr 1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.metric{background:var(--panel2);border-radius:8px;padding:10px 12px}
.metric .k{color:var(--sub);font-size:11px;letter-spacing:.4px}
.metric .v{font-size:20px;font-weight:700;margin-top:2px;font-variant-numeric:tabular-nums}
.metric .s{color:var(--sub);font-size:11px}
table{width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums}
th{color:var(--sub);text-align:right;font-weight:500;padding:6px 7px;
  border-bottom:1px solid var(--line);white-space:nowrap;cursor:pointer;user-select:none}
th:hover{color:var(--txt)}
th.nosort{cursor:default}
td{text-align:right;padding:5px 7px;border-bottom:1px solid #2e2e2e;white-space:nowrap}
th:first-child,td:first-child,th.l,td.l{text-align:left}
.pos{color:var(--red)} .neg{color:var(--green)}
.ok{color:var(--green);font-weight:700}.warn{color:var(--gold);font-weight:700}
.chip{display:inline-block;font-size:11px;border-radius:10px;padding:1px 10px;
  border:1px solid var(--line);color:var(--sub);margin:2px}
a{color:var(--blue);text-decoration:none}
a:hover{text-decoration:underline}
.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:10px 0 8px}
.controls input[type=text]{background:var(--panel2);border:1px solid var(--line);
  color:var(--txt);border-radius:6px;padding:6px 10px;font-size:12.5px;width:240px}
.controls select{background:var(--panel2);border:1px solid var(--line);
  color:var(--txt);border-radius:6px;padding:6px 8px;font-size:12.5px}
.btn{font-size:12px;border-radius:14px;padding:3px 12px;border:1px solid var(--line);
  background:transparent;color:var(--sub);cursor:pointer}
.btn.on{background:var(--gold);color:#1a1a1a;border-color:var(--gold);font-weight:700}
.rank td{border-bottom:none;padding:3px 7px}
.bar{height:8px;border-radius:4px;background:var(--panel2);overflow:hidden;display:flex;margin-top:6px}
.bar i{display:block;height:100%}
.note{color:var(--sub);font-size:11.5px;margin-top:8px}
.rationale{max-width:260px;overflow:hidden;text-overflow:ellipsis}
footer{margin-top:44px;padding-top:16px;border-top:1px solid var(--line);
  color:var(--sub);font-size:11.5px}
.count{color:var(--sub);font-size:12px;margin-left:6px}
.codebtn{cursor:pointer;background:var(--panel2);border:1px solid var(--line);
  border-radius:6px;padding:1px 8px;font-size:11px;color:var(--blue);white-space:nowrap}
.codebtn:hover{border-color:var(--blue)}
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;z-index:50;
  align-items:flex-start;justify-content:center;padding:44px 16px;overflow:auto}
.modal-bg.show{display:flex}
.modal{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  max-width:880px;width:100%;padding:18px 20px}
.modal h3{font-size:15px}
.modal .msrc{color:var(--sub);font-size:11.5px;margin:4px 0 10px}
.modal pre{background:#141414;border:1px solid var(--line);border-radius:8px;
  padding:12px;overflow:auto;max-height:62vh;font-size:12px;line-height:1.55;
  font-family:Consolas,Menlo,"Courier New",monospace;color:#d8d8d8;margin:0}
.modal .mbtn{background:var(--panel2);border:1px solid var(--line);color:var(--txt);
  border-radius:6px;padding:4px 12px;font-size:12px;cursor:pointer}
.modal .mbtn:hover{border-color:var(--gold);color:var(--gold)}
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1>因子库总览看板<span class="badge">__N__ 因子</span></h1>
  <div class="meta">全库审计快照 + FDR 批表 + 机制登记 + L0 近端动态（__RECENT__ 交易日）｜审计口径 v2 双类型（Rank/Pearson × h=1/5/20）｜<b>明细表主指标 = 近 1 年（L0 近 __YEAR1__ 交易日，__POOL__ 池）；t_adj/q = 全史 FDR 参照</b>｜L0 池：__POOLS__｜L0 数据更新至 __L0_DATE__｜生成 __GENERATED__</div>
</header>

<h2>库级概览</h2>
<div class="grid g6">
  <div class="metric"><div class="k">因子总数（已审计）</div><div class="v">__N__</div><div class="s">factor_audit JSON</div></div>
  <div class="metric"><div class="k">🟢 绿灯</div><div class="v" style="color:var(--green)">__GREEN__</div><div class="s">最新评估无标记</div></div>
  <div class="metric"><div class="k">🟡 黄灯</div><div class="v" style="color:var(--gold)">__YELLOW__</div><div class="s">≥2 项风险标记</div></div>
  <div class="metric"><div class="k">🔴 红灯</div><div class="v" style="color:var(--red)">__RED__</div><div class="s">PIT FAIL 等</div></div>
  <div class="metric"><div class="k">FDR 幸存 q&lt;0.05 / q&lt;0.25</div><div class="v">__Q05__ / __Q25__</div><div class="s">BH + 自相关折减</div></div>
  <div class="metric"><div class="k">HLZ t&gt;3 / 机制已登记</div><div class="v">__HLZ__ / __REG__</div><div class="s">多重检验门槛 / 经济机制</div></div>
</div>
<div class="card" style="margin-top:12px">
  <b style="font-size:12.5px">红绿灯 × PIT 分布</b>
  <div class="bar">__LIGHT_BAR__</div>
  <div class="note">红绿灯为最新一次 run_eval / 看板评估的初筛判定（未评估因子无灯）；红灯 = PIT/结构审计 FAIL 或盈亏平衡为负。红绿灯只是初筛，入生产候选须组合层 A/B 终审。</div>
</div>

<h2>榜单</h2>
<div class="desc">全史强度 = 审计快照 t_adj（BH 校正后）；近端活性 = L0 近 __RECENT__ 日动态（__POOL__ 池，信号日口径）；近 1 年强度 = L0 近 __YEAR1__ 交易日（约一年）；衰减比 = |近端 ICIR<sub>20</sub>| / |全史 ICIR<sub>20</sub>|，&lt;0.5 提示信号衰减</div>
<div class="grid g4">
  <div class="card"><b>全史强度 · |t_adj| Top 20</b>
    <table class="rank" id="tbl_t"></table>
    <div class="note">t_adj 来自 FDR 批表（BH + ρ 折减），与单因子看板 t_adj 同源。</div>
  </div>
  <div class="card"><b>近 1 年强度 · |ICIR<sub>20</sub> 近 __YEAR1__ 日| Top 20</b>
    <table class="rank" id="tbl_r1y"></table>
    <div class="note">L0 近 __YEAR1__ 交易日（约一年）动态 ICIR，窗口内成熟样本 ≥60 才入榜。</div>
  </div>
  <div class="card"><b>近端活性 · |ICIR<sub>20</sub> 近 __RECENT__ 日| Top 20</b>
    <table class="rank" id="tbl_recent"></table>
    <div class="note">IC 好≠组合好：此榜是统计活性排行，非可交易性排行。</div>
  </div>
  <div class="card"><b>衰减预警 · 近端/全史 ICIR 比最低 Top 20</b>
    <table class="rank" id="tbl_decay"></table>
    <div class="note">只列全史 |ICIR20|≥0.05 且近端 ≥5 个成熟样本的因子。</div>
  </div>
</div>

<h2>类别分布</h2>
<div class="card"><div id="tbl_cat"></div></div>

<h2>全因子明细表</h2>
<div class="desc">点击表头排序；因子名可点跳转单因子看板（灰名 = 尚无看板，跑 build_dashboard.py 生成）。<b>主指标（ric1/ric5/ric20/icir20/win5/头部年化）全部为近 1 年口径</b>（L0 近 __YEAR1__ 交易日聚合，__POOL__ 池）：头部年化 = 因子头部组 h=1 绝对收益日均 × 244（direction&gt;0 取高值组 q5、&lt;0 取低值组 q1，hover 显示多空年化）；t_adj/q = 全史 FDR 参照（多重检验仅全史有意义）；r1y_decay = 近 1 年 ICIR<sub>20</sub> ÷ 全史 ICIR<sub>20</sub>；r20* = L0 近 __RECENT__ 日。「代码」列点开计算逻辑弹窗：__CODE_SRC_N__ 个因子内嵌源码（🧩 类源码 / ∑ Alpha191 公式（含转译置信度）/ ƒ 函数源码），其余 __CODE_ANCHOR_N__ 个给实现脚本 文件:行号 锚点。</div>
<div class="controls">
  <input type="text" id="q" placeholder="搜索因子名 / 机制 / 描述…">
  <select id="f_cat"><option value="">全部类别</option></select>
  <select id="f_light">
    <option value="">全部红绿灯</option><option value="🟢">🟢 绿</option>
    <option value="🟡">🟡 黄</option><option value="🔴">🔴 红</option>
    <option value="none">未评估</option>
  </select>
  <label style="font-size:12px;color:var(--sub)"><input type="checkbox" id="f_fdr"> 仅 FDR 幸存（q&lt;0.25）</label>
  <label style="font-size:12px;color:var(--sub)"><input type="checkbox" id="f_center"> 仅已有单因子看板</label>
  <span class="count" id="shown"></span>
</div>
<div class="card" style="padding:6px 10px;overflow-x:auto">
  <table id="tbl_main">
    <thead><tr>
      <th class="l nosort">因子</th><th class="nosort">类别</th><th class="nosort">方向</th>
      <th>ric1·1Y</th><th>ric5·1Y</th><th>ric20·1Y</th><th>icir20·1Y</th><th>win5·1Y</th>
      <th>头部年化·1Y</th>
      <th>t_adj·全史</th><th>q·全史</th><th class="nosort">PIT</th><th class="nosort">灯</th>
      <th>r1y_decay</th>
      <th>r20_icir</th><th>r20_decay</th><th class="nosort">代码</th>
      <th class="nosort">机制</th>
    </tr></thead>
    <tbody id="tbody"></tbody>
  </table>
</div>

<footer>
  口径附注：审计快照 = factor_audit JSON（v2 双类型 schema，Rank/Pearson × h=1/5/20，legacy ic5 等同 rank 值），窗口 = <b>各因子审计时其湖内全部历史</b>（多数 2022 年初起约 1000~1140 个交易日，多数审计至 2026-09-14，Alpha191 批次 09-05 审计停于 09-04），跨因子起止不完全对齐；<b>明细表主指标 = metric_store L0 近 __YEAR1__ 交易日聚合（__POOL__ 池，信号日口径，h=20 成熟边界 T-20 起非空，未成熟日不计入；ric*/icir20/win5 = Rank IC 聚合，r20* 同源取近 __RECENT__ 日）</b>；<b>头部年化·1Y = 因子头部组 h=1 绝对收益（复权收盘前瞻）日均 × 244 简单年化</b>——direction&gt;0 取高值组 q5、&lt;0 取低值组 q1（metric_store NTILE(5) 定义），direction 缺失 __DIR_FALLBACK__ 个按近 1 年 ICIR20 符号兜底；hover 显示多空年化（q5−q1 同窗口同法年化）；<b>注意为绝对收益口径，未减池均值/无成本，非可交易回测</b>；FDR q = BH 校正 + 自相关折减（库级全史 h=20 快照，非本窗重算）；t_adj 同源；衰减比 = |近端窗口 rank_icir20| ÷ |审计快照 rank_icir20|（r1y_decay 用近 1 年、r20_decay 用近 __RECENT__ 日），两窗口与池不完全同轴（审计为全史、L0 为近端 __POOL__ 池），仅作衰减方向性提示；机制登记 = quantlab.factor.registry。IC 好≠组合好，红绿灯只是初筛，入生产候选须组合层 A/B 终审。<br>
  数据源：因子湖审计快照 + FDR 批表 + registry + metric_store L0（全只读，不触主库写锁）｜生成：__GENERATED__｜脚本 scripts/factor_eval/build_universe_dashboard.py（耗时 __ELAPSED__s）
</footer>
</div>

<div class="modal-bg" id="modalBg" onclick="if(event.target===this)closeCode()">
  <div class="modal">
    <h3 id="mTitle">—</h3>
    <div class="msrc" id="mSrc">—</div>
    <pre id="mBody">—</pre>
    <div style="margin-top:10px;display:flex;gap:8px;justify-content:flex-end">
      <button class="mbtn" onclick="copyCode()">复制源码</button>
      <button class="mbtn" onclick="closeCode()">关闭（Esc）</button>
    </div>
  </div>
</div>

<script>
const DATA = __DATA__;
const esc = s => String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt = (v,d=4) => v==null?'—':(v>0?'+':'')+Number(v).toFixed(d);
const cls = v => v==null?'':(v>0?'pos':'neg');

/* ---- 库级 KPI 分布条 ---- */
(function(){
  const n=DATA.length;
  const g=DATA.filter(r=>r.light==='🟢').length,
        y=DATA.filter(r=>r.light==='🟡').length,
        r=DATA.filter(r=>r.light==='🔴').length,
        x=n-g-y-r;
  const parts=[['🟢 '+g,'var(--green)',g],['🟡 '+y,'var(--gold)',y],
               ['🔴 '+r,'var(--red)',r],['未评估 '+x,'#555',x]]
    .filter(p=>p[2]>0)
    .map(p=>'<i title="'+p[0]+'" style="width:'+(p[2]/n*100)+'%;background:'+p[1]+'"></i>').join('');
  document.querySelector('#tbl_cat').closest('.card').insertAdjacentHTML('afterbegin','');
  const bar=document.querySelector('.bar'); if(bar) bar.innerHTML=parts;
})();

/* ---- 类别分布 ---- */
(function(){
  const m={};
  DATA.forEach(r=>{m[r.category]=(m[r.category]||0)+1;});
  const ent=Object.entries(m).sort((a,b)=>b[1]-a[1]);
  const mx=Math.max(...ent.map(e=>e[1]));
  let html='<table class="rank">';
  ent.forEach(([c,v])=>{
    html+='<tr><td class="l" style="width:160px">'+esc(c)+'</td>'+
      '<td style="width:70%;text-align:left"><i style="display:inline-block;height:10px;border-radius:3px;background:var(--blue);opacity:.75;width:'+(v/mx*100)+'%"></i> '+v+'</td>'+
      '<td>'+(v/DATA.length*100).toFixed(0)+'%</td></tr>';});
  html+='</table>';
  document.getElementById('tbl_cat').innerHTML=html;
})();

/* ---- 榜单 ---- */
function rankTable(id, list, col, fmtFn){
  let html='';
  list.forEach((r,i)=>{
    const name = r.has_center
      ? '<a href="factor_center_'+encodeURIComponent(r.factor)+'.html">'+esc(r.factor)+'</a>'
      : '<span style="color:#888">'+esc(r.factor)+'</span>';
    html+='<tr><td style="color:var(--sub);width:26px">'+(i+1)+'</td>'+
      '<td class="l">'+name+'</td><td class="'+cls(r[col])+'">'+fmtFn(r[col])+'</td>'+
      '<td style="color:var(--sub)">'+(r.light||'—')+'</td></tr>';});
  document.getElementById(id).innerHTML=html;
}
rankTable('tbl_t', [...DATA].filter(r=>r.t_adj!=null)
    .sort((a,b)=>Math.abs(b.t_adj)-Math.abs(a.t_adj)).slice(0,20),
  't_adj', v=>fmt(v,2));
rankTable('tbl_r1y', [...DATA].filter(r=>r.r1y_icir20!=null && (r.r1y_days||0)>=60)
    .sort((a,b)=>Math.abs(b.r1y_icir20)-Math.abs(a.r1y_icir20)).slice(0,20),
  'r1y_icir20', v=>fmt(v,3));
rankTable('tbl_recent', [...DATA].filter(r=>r.recent_icir20!=null)
    .sort((a,b)=>Math.abs(b.recent_icir20)-Math.abs(a.recent_icir20)).slice(0,20),
  'recent_icir20', v=>fmt(v,3));
rankTable('tbl_decay', [...DATA]
    .filter(r=>r.recent_decay!=null && r.icir20!=null && Math.abs(r.icir20)>=0.05 && (r.recent_days||0)>=5)
    .sort((a,b)=>a.recent_decay-b.recent_decay).slice(0,20),
  'recent_decay', v=>v.toFixed(2)+'×');

/* ---- 明细表：筛选 + 排序 ---- */
let sortKey='y1_icir20', sortAsc=false;
const cats=[...new Set(DATA.map(r=>r.category))].sort();
cats.forEach(c=>{const o=document.createElement('option');o.value=c;o.textContent=c;
  document.getElementById('f_cat').appendChild(o);});
const tbody=document.getElementById('tbody');

function render(){
  const q=document.getElementById('q').value.trim().toLowerCase(),
        cat=document.getElementById('f_cat').value,
        lt=document.getElementById('f_light').value,
        fdr=document.getElementById('f_fdr').checked,
        ctr=document.getElementById('f_center').checked;
  let rows=DATA.filter(r=>{
    if(cat && r.category!==cat) return false;
    if(lt){ if(lt==='none'){ if(r.light) return false; } else if(r.light!==lt) return false; }
    if(fdr && !(r.q!=null && r.q<0.25)) return false;
    if(ctr && !r.has_center) return false;
    if(q){
      const hay=(r.factor+' '+r.rationale+' '+r.desc+' '+r.category).toLowerCase();
      if(!hay.includes(q)) return false;
    }
    return true;});
  rows.sort((a,b)=>{
    let va=a[sortKey], vb=b[sortKey];
    if(va==null&&vb==null)return 0; if(va==null)return 1; if(vb==null)return -1;
    if(typeof va==='string') return sortAsc?va.localeCompare(vb):vb.localeCompare(va);
    return sortAsc?va-vb:vb-va;});
  document.getElementById('shown').textContent='显示 '+rows.length+' / '+DATA.length+' 因子';
  tbody.innerHTML=rows.map(r=>{
    const name = r.has_center
      ? '<a href="factor_center_'+encodeURIComponent(r.factor)+'.html" title="'+esc(r.desc)+'">'+esc(r.factor)+'</a>'
      : '<span style="color:#888" title="'+esc(r.desc)+'">'+esc(r.factor)+'</span>';
    const dec=r.recent_decay;
    const decCls = dec==null?'':(dec<0.5?'neg':(dec<0.8?'warn':''));
    const decY=r.r1y_decay;
    const decYCls = decY==null?'':(decY<0.5?'neg':(decY<0.8?'warn':''));
    return '<tr>'+
      '<td class="l">'+name+'</td>'+
      '<td style="color:var(--sub)">'+esc(r.category)+'</td>'+
      '<td>'+(r.direction==null?'—':(r.direction>0?'+':'')+r.direction)+'</td>'+
      '<td class="'+cls(r.y1_ric1)+'">'+fmt(r.y1_ric1)+'</td>'+
      '<td class="'+cls(r.y1_ric5)+'">'+fmt(r.y1_ric5)+'</td>'+
      '<td class="'+cls(r.y1_ric20)+'">'+fmt(r.y1_ric20)+'</td>'+
      '<td class="'+cls(r.y1_icir20)+'">'+fmt(r.y1_icir20,3)+'</td>'+
      '<td>'+(r.y1_win5==null?'—':(r.y1_win5*100).toFixed(0)+'%')+'</td>'+
      '<td class="'+cls(r.top_ann)+'" title="'+(r.ls_ann==null?'':'多空年化 '+((r.ls_ann>0?'+':'')+(r.ls_ann*100).toFixed(1)+'%'))+'">'+
        (r.top_ann==null?'—':(r.top_ann>0?'+':'')+(r.top_ann*100).toFixed(1)+'%')+'</td>'+
      '<td class="'+cls(r.t_adj)+'">'+fmt(r.t_adj,2)+'</td>'+
      '<td>'+(r.q==null?'—':r.q.toFixed(3))+'</td>'+
      '<td style="color:'+(r.pit==='PASS'?'var(--green)':(r.pit==='FAIL'?'var(--red)':'var(--gold)'))+'">'+esc(r.pit||'—')+'</td>'+
      '<td>'+(r.light||'—')+'</td>'+
      '<td class="'+decYCls+'">'+(decY==null?'—':decY.toFixed(2)+'×')+'</td>'+
      '<td class="'+cls(r.recent_icir20)+'">'+fmt(r.recent_icir20,3)+'</td>'+
      '<td class="'+decCls+'">'+(dec==null?'—':dec.toFixed(2)+'×')+'</td>'+
      codeCell(r)+
      '<td class="l rationale" style="color:var(--sub)" title="'+esc(r.rationale)+'">'+esc(r.rationale)+'</td>'+
      '</tr>';}).join('');
}
const KIND_ICON={'class':'🧩 类源码','alpha':'∑ 公式','func':'ƒ 函数','script':'📄 脚本','model':'🤖 模型'};
function codeCell(r){
  const c=r.code;
  if(!c) return '<td>—</td>';
  const t=KIND_ICON[c.kind]||'查看';
  return '<td><button class="codebtn" onclick="openCode(\''+esc(r.factor)+'\')" '+
    'title="'+esc(c.note||'')+'">'+t+'</button></td>';
}
document.querySelectorAll('#tbl_main th:not(.nosort)').forEach(th=>{
  const key=th.textContent.trim();
  const keyMap={'ric1·1Y':'y1_ric1','ric5·1Y':'y1_ric5','ric20·1Y':'y1_ric20',
    'icir20·1Y':'y1_icir20','win5·1Y':'y1_win5','头部年化·1Y':'top_ann',
    't_adj·全史':'t_adj','q·全史':'q',
    'r1y_decay':'r1y_decay',
    'r20_icir':'recent_icir20','r20_decay':'recent_decay'};
  th.onclick=()=>{
    const k=keyMap[key]; if(!k) return;
    if(sortKey===k) sortAsc=!sortAsc; else {sortKey=k; sortAsc=false;}
    render();};
});
['q','f_cat','f_light','f_fdr','f_center'].forEach(id=>{
  document.getElementById(id).addEventListener(id==='q'?'input':'change',render);});
render();

/* ---- 计算逻辑弹窗 ---- */
const BY_NAME = Object.fromEntries(DATA.map(r=>[r.factor,r]));
let curCode = null;
function openCode(name){
  const r = BY_NAME[name]; if(!r||!r.code) return;
  const c = r.code;
  curCode = c.src||null;
  document.getElementById('mTitle').textContent =
    name + ' · ' + ({'class':'类源码','alpha':'Alpha191 公式源码','func':'函数源码','script':'脚本实现','model':'模型训练'}[c.kind]||'');
  const bits = [];
  if(c.conf) bits.push('转译置信度: '+c.conf);
  if(c.anchor) bits.push('位置: '+c.anchor);
  if(c.note) bits.push(c.note);
  document.getElementById('mSrc').textContent = bits.join('｜');
  const body = document.getElementById('mBody');
  if(c.src){ body.textContent = c.src; }
  else { body.textContent = '该因子的公式内嵌于批量脚本 main()（未单点函数化），'
    + '无独立源码段可贴。请按上述 文件:行号 锚点查看实现上下文。'; }
  document.getElementById('modalBg').classList.add('show');
}
function closeCode(){ document.getElementById('modalBg').classList.remove('show'); }
function copyCode(){
  if(!curCode) return;
  const done = ok => { const b=document.activeElement; if(b&&b.tagName==='BUTTON'){b.textContent=ok?'已复制':'复制失败';setTimeout(()=>{b.textContent='复制源码'},1200);} };
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(curCode).then(()=>done(true),()=>fallbackCopy());
  } else fallbackCopy();
  function fallbackCopy(){
    const ta=document.createElement('textarea');ta.value=curCode;
    document.body.appendChild(ta);ta.select();
    let ok=false; try{ok=document.execCommand('copy');}catch(e){}
    document.body.removeChild(ta);done(ok);
  }
}
document.addEventListener('keydown',e=>{ if(e.key==='Escape') closeCode(); });
</script>
</body>
</html>"""
    R = {
        "__N__": str(len(rows)),
        "__RECENT__": str(X["recent"]),
        "__YEAR1__": str(X["year1"]),
        "__POOL__": X["pool"],
        "__POOLS__": X["pools"],
        "__L0_DATE__": X["l0_date"],
        "__GENERATED__": f"{datetime.now():%Y-%m-%d %H:%M}",
        "__ELAPSED__": str(X["elapsed"]),
        "__GREEN__": str(X["green"]), "__YELLOW__": str(X["yellow"]),
        "__RED__": str(X["red"]),
        "__Q05__": str(X["q05"]), "__Q25__": str(X["q25"]),
        "__HLZ__": str(X["hlz"]), "__REG__": str(X["registered"]),
        "__CODE_SRC_N__": str(X["code_src_n"]),
        "__CODE_ANCHOR_N__": str(X["code_any_n"] - X["code_src_n"]),
        "__DIR_FALLBACK__": str(X["dir_fallback"]),
        "__LIGHT_BAR__": "",  # 分布条由前端按数据渲染
        "__DATA__": payload,
    }
    html = TEMPLATE
    for k, v in R.items():
        html = html.replace(k, str(v))
    return html


def main() -> None:
    p = argparse.ArgumentParser(description="全因子总览看板构建器")
    p.add_argument("--pool", default="ashare_ex",
                   help="L0 近端动态主池（默认 ashare_ex）")
    p.add_argument("--recent", type=int, default=60,
                   help="近端动态窗口（交易日，默认 60）")
    p.add_argument("--year1", type=int, default=244,
                   help="近 1 年窗口（交易日，默认 244）")
    a = p.parse_args()
    t0 = time.time()

    rows = collect_rows()
    attach_recent(rows, a.pool, a.recent, "recent")
    attach_recent(rows, a.pool, a.year1, "r1y")
    rec1y = load_recent_full(a.pool, a.year1)
    n_dir_fallback = 0
    for r in rows:
        d = rec1y.get(r["factor"], {})
        for k in ("ric1", "ric5", "ric20", "icir20", "win5"):
            r[f"y1_{k}"] = d.get(k)
        r["y1_days"] = d.get("days")
        # 头部收益率：direction>0 → q5（高值组）；<0 → q1（低值组）。
        # direction 缺失时按近 1 年 ICIR20 符号兜底（footer 已声明）。
        direction = r.get("direction")
        if direction is None and d.get("icir20") is not None:
            direction = 1 if d["icir20"] >= 0 else -1
            n_dir_fallback += 1
        if direction is not None and direction != 0:
            r["top_ann"] = d.get("q5_ann") if direction > 0 else d.get("q1_ann")
        else:
            r["top_ann"] = None
        r["ls_ann"] = d.get("ls_ann")
    codes = collect_code([r["factor"] for r in rows])
    for r in rows:
        r["code"] = codes.get(r["factor"])
    pools = f"{a.pool}（+zz1000 可改 --pool）"

    n = len(rows)
    green = sum(1 for r in rows if r["light"] == "🟢")
    yellow = sum(1 for r in rows if r["light"] == "🟡")
    red = sum(1 for r in rows if r["light"] == "🔴")
    q05 = sum(1 for r in rows if r["q"] is not None and r["q"] < 0.05)
    q25 = sum(1 for r in rows if r["q"] is not None and r["q"] < 0.25)
    hlz = sum(1 for r in rows if r["harvey"])
    registered = sum(1 for r in rows if r["rationale"] != "未登记")
    l0_date = max((r["recent_date"] for r in rows if r["recent_date"]),
                  default="—")
    code_src_n = sum(1 for r in rows
                     if r["code"] and r["code"].get("src"))
    code_any_n = sum(1 for r in rows if r["code"])

    X = {"recent": a.recent, "year1": a.year1, "pool": a.pool, "pools": pools,
         "l0_date": l0_date, "green": green, "yellow": yellow, "red": red,
         "q05": q05, "q25": q25, "hlz": hlz, "registered": registered,
         "code_src_n": code_src_n, "code_any_n": code_any_n,
         "dir_fallback": n_dir_fallback,
         "elapsed": round(time.time() - t0, 1)}

    DOCS.mkdir(exist_ok=True)
    out = DOCS / "factor_universe.html"
    out.write_text(build_html(rows, X), encoding="utf-8")

    idx = (json.loads(INDEX.read_text(encoding="utf-8"))
           if INDEX.exists() else [])
    ts = datetime.now()
    idx.append({
        "id": f"dashboard:factor_universe:{ts:%Y%m%d%H%M}",
        "type": "dashboard", "name": "factor_universe",
        "date": f"{ts:%Y-%m-%d}", "generated_at": ts.isoformat(timespec="seconds"),
        "window": f"L0 近 {a.recent} 日 + 近 1 年 {a.year1} 日（{a.pool}）", "tags": ["universe", "overview"],
        "light": "—", "flags": [],
        "file": str(out.relative_to(ROOT)).replace("\\", "/"),
        "template": "universe-v1",
        "summary": (f"全库 {n} 因子总览：🟢{green} 🟡{yellow} 🔴{red}｜"
                    f"FDR q<0.25 幸存 {q25}｜机制已登记 {registered}"),
    })
    INDEX.write_text(json.dumps(idx, ensure_ascii=False, indent=1),
                     encoding="utf-8")

    print(f"[done] 全库总览看板：{n} 因子")
    print(f"  红绿灯 🟢{green} 🟡{yellow} 🔴{red} 未评估{n - green - yellow - red}")
    print(f"  FDR q<0.05: {q05}  q<0.25: {q25}  HLZ t>3: {hlz}  机制已登记: {registered}")
    print(f"  计算逻辑: 源码内嵌 {code_src_n} / 锚点登记 {code_any_n - code_src_n}"
          f"（合计 {code_any_n}/{n}）")
    print(f"  明细表 1Y 口径: direction 兜底 {n_dir_fallback} 个"
          f"｜头部年化有值 {sum(1 for r in rows if r['top_ann'] is not None)}")
    print(f"[dashboard] {out}  ({out.stat().st_size:,} bytes)")
    print(f"[index] {len(idx)} 条")


if __name__ == "__main__":
    main()
