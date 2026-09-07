#!/usr/bin/env python3
"""数据详情监控看板
=====================================
随时掌握 QuantLab 数据湖全貌：数据域水位/新鲜度/健康度、因子库状态、
K线覆盖深度、质量体检结果。每次运行重新生成，随时可跑：
    $PY scripts/data_dashboard.py
输出: reports/data_dashboard.html
"""
import sys
import json
import time
import warnings
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import pandas as pd

from quantlab.config import (FACTOR_DIR, REPORTS_DIR, DUCKDB_PATH,
                             KLINE_1MIN_DIR, LAKE_DIR)
from quantlab.data.store import query

# 各数据域：表名 → (中文名, 日期列, 更新频率, 数据源, 披露截止)
# cutoff：上游停止披露的日期（如北向净买入 2024-08-16 起交易所不再公布），
# 该域新鲜度以 min(最新交易日, cutoff) 为基准，避免误报"滞后"。
DOMAINS = {
    "kline_daily":      ("日K线+复权因子", "date", "日", "通达信", None),
    "kline_1min":       ("1分钟K线(Parquet湖)", "datetime", "日", "free-stockdb", None),
    "index_kline":      ("指数日K", "date", "日", "通达信", None),
    "trade_calendar":   ("交易日历", "trade_date", "年", "自维护", None),
    "instruments":      ("股票主表", "updated_at", "周", "新浪+通达信", None),
    "daily_snapshot":   ("当日估值快照", "date", "日", "腾讯", None),
    "finance_snapshot": ("财务快照", "report_date", "周触发", "通达信", None),
    "dragon_tiger":     ("龙虎榜", "date", "日", "东财", None),
    "margin_total":     ("两融余额", "date", "日", "东财", None),
    "lockup":           ("限售解禁", "date", "日", "东财", None),
    "block_trade":      ("大宗交易", "date", "日", "东财", None),
    "holder_num":       ("股东户数", "holder_date", "月触发", "东财", None),
    "hot_topic":        ("热点题材", "date", "日", "同花顺", None),
    "northbound_daily": ("北向资金", "date", "日", "东财", "2024-08-15"),
    "index_members":    ("指数成分", "in_date", "周触发", "东财", None),
    "fund_flow_daily":  ("个股资金流", "date", "日", "东财", None),
    "signal_portfolio": ("纸面组合台账", "date", "日", "内部", None),
    "crowding":         ("拥挤度监控(Parquet湖)", "date", "日(5日采样)", "内部", None),
}

# Parquet 直读域：不依赖 DuckDB 表/镜像（写锁占用期间仍可正确统计）
_PARQUET_DOMAINS = {"kline_1min", "crowding"}


def _db_locked() -> bool:
    """DuckDB 写锁是否被数据更新任务占用"""
    try:
        import duckdb as _dk
        con = _dk.connect(str(DUCKDB_PATH), read_only=True)
        con.close()
        return False
    except Exception:
        return True


def _parquet_stats(table: str):
    """Parquet 湖域直读统计：返回 (行数, 最新日期) 或抛异常"""
    if table == "kline_1min":
        import duckdb as _dk
        con = _dk.connect()
        try:
            pat = str(KLINE_1MIN_DIR / "**" / "*.parquet").replace("\\", "/")
            r = con.execute(
                f"SELECT COUNT(*), MAX(datetime) FROM read_parquet("
                f"'{pat}', hive_partitioning=1)").fetchone()
            return int(r[0]), r[1]
        finally:
            con.close()
    if table == "crowding":
        h = pd.read_parquet(LAKE_DIR / "crowding" / "history.parquet")
        return len(h), pd.to_datetime(h["date"]).max()
    raise ValueError(f"unknown parquet domain: {table}")


def _latest_trade_date():
    df = query("SELECT MAX(trade_date) d FROM trade_calendar")
    return pd.to_datetime(df.iloc[0, 0])


def domain_table():
    """各数据域：行数 / 最新记录 / 水位新鲜度 / 状态灯"""
    rows = []
    latest_td = _latest_trade_date()
    locked = _db_locked()
    for t, (cn, dcol, freq, src, cutoff) in DOMAINS.items():
        mirror_missing = False
        if t in _PARQUET_DOMAINS:
            # Parquet 湖直读：不受 DuckDB 写锁影响
            try:
                n, mx = _parquet_stats(t)
            except Exception:
                n, mx = 0, None
        else:
            try:
                df = query(f"SELECT COUNT(*) n, MAX({dcol}) mx FROM {t}")
                n, mx = int(df.iloc[0, 0]), df.iloc[0, 1]
            except Exception as e:
                # 写锁占用时 query() 降级 Parquet 镜像，镜像未含的表报 Catalog Error
                n, mx = 0, None
                mirror_missing = locked and "does not exist" in str(e)
        # 披露截止域：基准日取 cutoff（如北向净买入 2024-08-15 后停披）
        cutoff_ts = pd.to_datetime(cutoff) if cutoff else None
        base_td = min(latest_td, cutoff_ts) if cutoff_ts else latest_td
        days_ago = None
        if mx is not None and str(mx) not in ("NaT", "None", "-"):
            try:
                days_ago = (base_td - pd.to_datetime(mx)).days
            except Exception:
                days_ago = None
        # 状态灯：日频域落后基准日>3天=红；1-3天=黄；0=绿；非日频/低频=绿
        if mirror_missing:
            status, cls = "镜像未含（更新中）", "gray"
        elif n == 0:
            status, cls = "空", "gray"
        elif days_ago is None:
            status, cls = "n/a", "gray"
        elif days_ago <= 0:
            status = "健康" + (f"（披露止{cutoff}）" if cutoff else "")
            cls = "ok"
        elif days_ago <= 3:
            status, cls = f"落后{days_ago}天", "warn"
        else:
            status, cls = f"滞后{days_ago}天", "bad"
        size = _table_size(t)
        rows.append({"table": t, "cn": cn, "src": src, "freq": freq,
                     "rows": n, "latest": str(mx)[:10] if mx else "-",
                     "days_ago": days_ago, "status": status, "cls": cls,
                     "size": size})
    return rows


def _table_size(t):
    try:
        from quantlab import config
        # 仅 duckdb 总大小无法按表拆，标注 -
        return "-"
    except Exception:
        return "-"


def factor_table():
    """因子库扫描：全部因子（含 alpha191）的行数/日期范围/磁盘大小"""
    rows = []
    for d in sorted(FACTOR_DIR.iterdir()):
        if not d.is_dir():
            continue
        files = list(d.glob("part-*.parquet"))
        if not files:
            continue
        try:
            import duckdb
            paths = ", ".join("'" + str(f).replace("\\", "/") + "'" for f in files)
            con = duckdb.connect()
            r = con.execute(
                f"SELECT COUNT(*) n, MIN(date) mn, MAX(date) mx "
                f"FROM read_parquet([{paths}])").fetchone()
            con.close()
            size = sum(f.stat().st_size for f in files) / 1e6
            rows.append({"factor": d.name, "rows": r[0],
                         "start": str(r[1])[:10], "end": str(r[2])[:10],
                         "mb": round(size, 1)})
        except Exception:
            continue
    return rows


def _load_audits() -> dict:
    """读取全部因子审查 JSON：verdict / PIT / IC 指标 / issues / 覆盖明细"""
    out = {}
    audit_dir = Path("data/lake/factor_audit")
    if audit_dir.exists():
        for p in audit_dir.glob("*.json"):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
                checks = rec.get("checks") or {}
                ic = checks.get("ic") or {}
                pit = checks.get("pit") or {}
                cov = checks.get("coverage") or {}
                dist = checks.get("distribution") or {}
                issues = rec.get("issues") or []
                out[p.stem] = {
                    "verdict": rec.get("verdict", "?"),
                    "pit": pit.get("status", "SKIP") if isinstance(pit, dict) else "SKIP",
                    "ic20": ic.get("ic20"), "icir20": ic.get("icir20"),
                    "ic5": ic.get("ic5"), "dir": ic.get("direction"),
                    "issues": issues,
                    "cov_med": cov.get("codes_per_day_median"),
                    "nonzero": dist.get("nonzero_ratio"),
                }
            except Exception:
                continue
    return out


def _grade(ic20, icir20):
    """评估结论（项目阈值：|IC20|>0.03 且 |ICIR|>0.5 进候选池）"""
    if ic20 is None or icir20 is None:
        return "未评", "#8b949e"
    a, b = abs(ic20), abs(icir20)
    if a > 0.03 and b > 0.5:
        return "✅ 达标", "#1a7f37"
    if a > 0.02 and b > 0.3:
        return "⚠ 观察", "#9a6700"
    return "✗ 弱", "#cf222e"


def _esc(s) -> str:
    return str(s).replace('"', "'").replace("<", "&lt;").replace(">", "&gt;")


def kline_depth():
    """K线覆盖深度：交易日覆盖 / 每股天数分布"""
    out = {}
    df = query("""
        SELECT COUNT(DISTINCT date) nd, MIN(date) mn, MAX(date) mx,
               COUNT(DISTINCT code) nc, COUNT(*) n
        FROM kline_daily
    """)
    r = df.iloc[0]
    out["days"] = int(r["nd"])
    out["start"], out["end"] = str(r["mn"])[:10], str(r["mx"])[:10]
    out["codes"] = int(r["nc"])
    out["rows"] = int(r["n"])
    # 近 250 交易日每股缺失天数分桶
    df2 = query("""
        WITH per AS (
          SELECT code, COUNT(*) c FROM
            (SELECT code, date FROM kline_daily
             WHERE date > (SELECT MAX(date) - INTERVAL 365 DAY FROM kline_daily))
          GROUP BY code)
        SELECT CASE WHEN c >= 240 THEN 'a.<5%缺失'
                    WHEN c >= 228 THEN 'b.<10%缺失'
                    WHEN c >= 200 THEN 'c.10~20%缺失'
                    ELSE 'd.>20%缺失' END bucket, COUNT(*) n
        FROM per GROUP BY bucket ORDER BY bucket
    """)
    out["buckets"] = df2.to_dict("records")
    return out


def quality_checks():
    """数据质量体检（轻量）"""
    try:
        from quantlab.data.quality import check_all
        iss = check_all()
        if iss.empty:
            return []
        return iss.to_dict("records")
    except Exception as e:
        return [{"domain": "-", "check_type": "体检异常", "status": "warn",
                 "detail": str(e)[:80]}]


def minute_depth():
    """分钟层覆盖与缺口（湖直读 + 日频对照，写锁兼容）

    缺口定义：近 30 天日频活跃、但近 30 天无分钟 bar 的代码。
    分组：北交所(920)分钟停更（时变缺口）/ 无任何分钟数据 / 沪深其他。
    """
    import duckdb as _dk
    out = {"rows": 0, "codes": 0, "start": "-", "end": "-",
           "gap_total": 0, "no_data_bse": 0, "stale_bse": 0,
           "stale_bse_end": "-", "gap_other": 0}
    try:
        con = _dk.connect()
        try:
            pat = str(KLINE_1MIN_DIR / "**" / "*.parquet").replace("\\", "/")
            r = con.execute(
                f"SELECT COUNT(*) n, MIN(datetime) mn, MAX(datetime) mx, "
                f"COUNT(DISTINCT code) nc FROM read_parquet('{pat}', "
                f"hive_partitioning=1)").fetchone()
            out["rows"], out["codes"] = int(r[0]), int(r[3])
            out["start"], out["end"] = str(r[1])[:10], str(r[2])[:16]
            per = con.execute(
                f"SELECT code, MAX(datetime) mx FROM read_parquet('{pat}', "
                f"hive_partitioning=1) GROUP BY code").df()
        finally:
            con.close()
        per["mx"] = pd.to_datetime(per["mx"])
        have = set(per["code"])
        # 近 30 天日频活跃（query 走镜像兜底，写锁期间仍可用）
        dly = query("SELECT DISTINCT code FROM kline_daily "
                    "WHERE date > CURRENT_DATE - INTERVAL 30 DAY")
        act = set(dly["code"].astype(str))
        gap = act - have                       # 无任何分钟数据
        out["no_data_bse"] = sum(1 for c in gap if c.startswith("920"))
        # 时变缺口：有数据但终点落后 >7 天（近 30 天日频活跃）
        cut = pd.Timestamp.now() - pd.Timedelta(days=7)
        stale = per[per["code"].isin(act) & (per["mx"] < cut)]
        bse = stale[stale["code"].str.startswith("920")]
        other = stale[~stale["code"].str.startswith("920")]
        out["stale_bse"] = len(bse)
        if len(bse):
            out["stale_bse_end"] = str(bse["mx"].max())[:10]
        out["gap_other"] = (len(gap) - out["no_data_bse"]) + len(other)
        out["gap_total"] = len(gap) + len(stale)
    except Exception:
        pass
    return out


def main():
    t0 = time.time()
    dom = domain_table()
    fac = factor_table()
    depth = kline_depth()
    qual = quality_checks()
    mdepth = minute_depth()

    dom_tr = "".join(
        f"<tr><td><code>{r['table']}</code></td><td>{r['cn']}</td>"
        f"<td>{r['src']}</td><td>{r['freq']}</td>"
        f"<td>{r['rows']:,}</td><td>{r['latest']}</td>"
        f"<td><span class='dot {r['cls']}'></span>{r['status']}</td></tr>"
        for r in dom)

    # 因子审查状态（data/lake/factor_audit/*.json，由构建即审查机制写入）
    audits = _load_audits()

    def _fmt(v, nd=4):
        return f"{v:+.{nd}f}" if isinstance(v, (int, float)) else "-"

    fac_rows = []
    grade_cnt = {"达标": 0, "观察": 0, "弱": 0, "未评": 0}
    verdict_cnt = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for r in fac:
        a = audits.get(r["factor"])
        if a:
            ic20, icir20, ic5 = a["ic20"], a["icir20"], a["ic5"]
            grade, gcolor = _grade(ic20, icir20)
            v_color = {"PASS": "#1a7f37", "WARN": "#9a6700",
                       "FAIL": "#cf222e"}.get(a["verdict"], "#8b949e")
            p_color = {"PASS": "#1a7f37", "WARN": "#9a6700",
                       "FAIL": "#cf222e"}.get(a["pit"], "#8b949e")
            issues = "; ".join(a["issues"]) if a["issues"] else "无"
            title = (f"方向 {a['dir']:+d} | 日均覆盖 {a['cov_med']} 股 | "
                     f"非零率 {a['nonzero']} | issues: {_esc(issues)}"
                     if a["dir"] is not None else
                     f"日均覆盖 {a['cov_med']} 股 | 非零率 {a['nonzero']} | "
                     f"issues: {_esc(issues)}")
            audit_html = (f"<span style='color:{v_color};font-weight:600'>{a['verdict']}</span>")
            pit_html = (f"<span style='color:{p_color}'>{a['pit']}</span>")
        else:
            ic20 = icir20 = ic5 = None
            grade, gcolor = "未评", "#8b949e"
            title = "无审查记录"
            audit_html = "<span style='color:#8b949e'>未审</span>"
            pit_html = "<span style='color:#8b949e'>-</span>"
        grade_cnt[grade.replace("✅ ", "").replace("⚠ ", "").replace("✗ ", "")] += 1
        if a:
            verdict_cnt[a["verdict"]] = verdict_cnt.get(a["verdict"], 0) + 1
        fac_rows.append(
            f"<tr title='{_esc(title)}'>"
            f"<td><code>{r['factor']}</code></td>"
            f"<td><span style='color:{gcolor};font-weight:600'>{grade}</span></td>"
            f"<td>{_fmt(ic20)}</td><td>{_fmt(icir20)}</td><td>{_fmt(ic5)}</td>"
            f"<td>{audit_html}</td><td>{pit_html}</td>"
            f"<td>{r['rows']:,}</td><td>{r['start']} ~ {r['end']}</td></tr>")

    fac_tr = "".join(fac_rows)
    fac_stat = (f"评估：✅ 达标 <b>{grade_cnt['达标']}</b> · ⚠ 观察 {grade_cnt['观察']}"
                f" · ✗ 弱 {grade_cnt['弱']} · 未评 {grade_cnt['未评']}"
                f"　|　审查：PASS {verdict_cnt.get('PASS', 0)}"
                f" / WARN {verdict_cnt.get('WARN', 0)}"
                f" / FAIL {verdict_cnt.get('FAIL', 0)}")

    q_tr = "".join(
        f"<tr><td>{r.get('domain','-')}</td><td>{r.get('check_type','-')}</td>"
        f"<td><span class='dot {'ok' if r.get('status')=='pass' else 'warn' if r.get('status')=='warn' else 'bad'}'></span>{r.get('status','-')}</td>"
        f"<td class='muted'>{r.get('detail','')}</td></tr>"
        for r in qual)

    buckets = depth["buckets"]
    total_b = sum(b["n"] for b in buckets) or 1
    bk_tr = "".join(
        f"<tr><td>{b['bucket'].split('.')[1]}</td><td>{b['n']:,}</td>"
        f"<td>{b['n']/total_b:.1%}</td></tr>"
        for b in buckets)

    n_ok = sum(1 for r in dom if r["cls"] == "ok")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lock_note = ""
    if _db_locked():
        lock_note = (" · <span style='color:#d97706'>⚠ 数据更新任务进行中"
                     "（DuckDB 写锁占用）：查询走 Parquet 镜像兜底，"
                     "kline_1min/crowding 为湖直读实时数，"
                     "signal_portfolio 等镜像未含表暂不可查</span>")
    payload = {
        "dom_names": [r["cn"] for r in dom],
        "dom_days": [r["days_ago"] if r["days_ago"] is not None else 0 for r in dom],
    }

    html = TMPL.format(
        now=now, gen_sec=round(time.time() - t0, 1),
        n_dom=len(dom), n_ok=n_ok, n_factor=len(fac),
        depth=depth, dom_tr=dom_tr, fac_tr=fac_tr, q_tr=q_tr, bk_tr=bk_tr,
        lock_note=lock_note, fac_stat=fac_stat, mdepth=mdepth,
        fac_mb=round(sum(r["mb"] for r in fac), 0),
        payload=str(payload).replace("'", '"'))

    out = REPORTS_DIR / "data_dashboard.html"
    out.write_text(html, encoding="utf-8")
    print(f"看板已生成: {out}（{round(time.time()-t0,1)}s）")


TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QuantLab 数据监控看板</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
:root{{--bg:#f7f8fa;--card:#fff;--text:#1f2430;--muted:#7a8194;--line:#e6e8ee;
--accent:#2563eb;--ok:#1d9e75;--warn:#d97706;--bad:#d8463e;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--text);font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.65;padding:36px 20px;}}
.wrap{{max-width:1100px;margin:0 auto;}}
h1{{font-size:24px;font-weight:700;}}
h2{{font-size:16.5px;font-weight:600;margin:32px 0 12px;padding-left:11px;border-left:3px solid var(--accent);}}
.sub{{color:var(--muted);font-size:13px;margin-top:5px;}}
.cards{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:20px 0;}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;}}
.card .l{{color:var(--muted);font-size:12px;}}
.card .v{{font-size:21px;font-weight:700;margin-top:3px;}}
table{{width:100%;border-collapse:collapse;margin:10px 0;font-size:12.5px;background:var(--card);}}
th,td{{padding:6px 10px;text-align:right;border-bottom:1px solid var(--line);}}
th{{color:var(--muted);font-weight:600;background:#fbfbfd;}}
td:first-child,th:first-child,td:nth-child(2),th:nth-child(2){{text-align:left;}}
code{{font-family:ui-monospace,Consolas,monospace;color:#1d4ed8;font-size:12px;}}
.muted{{color:var(--muted);}}
.chart{{width:100%;height:360px;margin:8px 0;background:var(--card);border:1px solid var(--line);border-radius:12px;}}
.dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;}}
.dot.ok{{background:var(--ok);}} .dot.warn{{background:var(--warn);}}
.dot.bad{{background:var(--bad);}} .dot.gray{{background:#c3c9d4;}}
.note{{color:var(--muted);font-size:12px;margin-top:10px;}}
</style>
</head>
<body>
<div class="wrap">
<h1>QuantLab 数据监控看板</h1>
<p class="sub">生成于 {now} · 计算耗时 {gen_sec}s · 随时重新生成：$PY scripts/data_dashboard.py{lock_note}</p>

<div class="cards">
  <div class="card"><div class="l">数据域</div><div class="v">{n_dom}</div></div>
  <div class="card"><div class="l">健康域数</div><div class="v" style="color:var(--ok)">{n_ok}</div></div>
  <div class="card"><div class="l">因子库</div><div class="v">{n_factor} 个</div></div>
  <div class="card"><div class="l">因子库大小</div><div class="v">{fac_mb} MB</div></div>
  <div class="card"><div class="l">K线覆盖</div><div class="v" style="font-size:15px">{depth[start]} ~<br>{depth[end]}</div></div>
</div>

<h2>一、数据域水位与健康度</h2>
<table>
<thead><tr><th>表</th><th>说明</th><th>源</th><th>频率</th><th>行数</th><th>最新记录</th><th>状态</th></tr></thead>
<tbody>{dom_tr}</tbody>
</table>
<p class="note">状态口径：相对最新交易日（trade_calendar 水位），日频域 0 天=健康 / 1-3 天=落后 / >3 天=滞后。</p>
<div id="chartFresh" class="chart"></div>

<h2>二、K 线覆盖深度</h2>
<div class="cards" style="grid-template-columns:repeat(4,1fr)">
  <div class="card"><div class="l">交易日数</div><div class="v">{depth[days]}</div></div>
  <div class="card"><div class="l">股票数</div><div class="v">{depth[codes]}</div></div>
  <div class="card"><div class="l">总行数</div><div class="v" style="font-size:17px">{depth[rows]:,}</div></div>
  <div class="card"><div class="l">区间</div><div class="v" style="font-size:14px">{depth[start]} ~ {depth[end]}</div></div>
</div>
<h3 style="font-size:14px;margin:14px 0 6px">近一年每股 K 线天数分布</h3>
<table style="max-width:480px">
<thead><tr><th>缺失程度</th><th>股票数</th><th>占比</th></tr></thead>
<tbody>{bk_tr}</tbody>
</table>

<h3 style="font-size:14px;margin:22px 0 6px">分钟层（kline_1min · Parquet 湖 · free-stockdb）</h3>
<div class="cards" style="grid-template-columns:repeat(4,1fr)">
  <div class="card"><div class="l">总行数</div><div class="v" style="font-size:17px">{mdepth[rows]:,}</div></div>
  <div class="card"><div class="l">代码数</div><div class="v">{mdepth[codes]}</div></div>
  <div class="card"><div class="l">覆盖区间</div><div class="v" style="font-size:14px">{mdepth[start]} ~ {mdepth[end]}</div></div>
  <div class="card"><div class="l">覆盖缺口（近30天日频活跃）</div><div class="v" style="font-size:17px;color:var(--warn)">{mdepth[gap_total]}</div></div>
</div>
<table style="max-width:820px">
<thead><tr><th>缺口分组</th><th>代码数</th><th style="text-align:left">说明</th></tr></thead>
<tbody>
<tr><td>北交所(920)：分钟停更</td><td>{mdepth[stale_bse]}</td><td class="muted" style="text-align:left">fsdb 镜像北交所分钟止于 {mdepth[stale_bse_end]}（时变缺口），日频正常交易</td></tr>
<tr><td>北交所(920)：无分钟数据</td><td>{mdepth[no_data_bse]}</td><td class="muted" style="text-align:left">fsdb 全程未收录</td></tr>
<tr><td>沪深：无数据 / 停更</td><td>{mdepth[gap_other]}</td><td class="muted" style="text-align:left">次新上市未收录为主</td></tr>
</tbody>
</table>
<p class="note">分钟层时间范围与日频对齐受源限制：fsdb 分钟仅 2025-01-02 起（日频 2022-01-04 起，2022-2024 无免费分钟源）。
缺口 = 近 30 天日频活跃但分钟无 bar 的代码，每日增量自愈复查（~30s），fsdb 恢复收录即自动流入。
增量防双写：写前 anti-join 回补存量键；完整性判定直扫湖（水位为 DATE 不含时点）。</p>

<h2>三、因子库状态（含 Alpha191 · 构建即审查 · IC 评估）</h2>
<p class="sub">{fac_stat}</p>
<table>
<thead><tr><th>因子</th><th>评估</th><th>IC20</th><th>ICIR20</th><th>IC5</th><th>审查</th><th>PIT</th><th>行数</th><th>日期范围</th></tr></thead>
<tbody>{fac_tr}</tbody>
</table>
<p class="note">评估口径（horizon=20 秩相关 IC，audit 时点计算值）：|IC20|&gt;0.03 且 |ICIR|&gt;0.5 = ✅ 达标（候选池阈值）；
|IC20|&gt;0.02 且 |ICIR|&gt;0.3 = ⚠ 观察；其余有 IC 者 = ✗ 弱；无 IC = 未评（audit 版本早于 IC 检查接入）。
PIT = 前视穿越自检（PASS / SKIP=无可截断数据源 / WARN=as-of 口径）；行悬停可见方向、日均覆盖、非零率与 issues。
WARN 两类口径边界（非缺陷，悬停可查明细）：① 股本类因子（size/turnover/ep/bp/chip_age 等）引用当前股本回填历史——finance_history 无股本科目；
② op_margin/quick_ratio 缺营业利润/存货科目，保留单点截面版。
2026-09-06 已将 roe/debt_ratio/ocf_ratio/current_ratio 迁移至 finance_history 多期版（as-of 与低覆盖 WARN 已消除）。
审查结论详情见 reports/factor_audit.html，详细记录：data/lake/factor_audit/&lt;因子&gt;.json</p>

<h2>四、数据质量体检</h2>
<table>
<thead><tr><th>域</th><th>检查项</th><th>状态</th><th>详情</th></tr></thead>
<tbody>{q_tr}</tbody>
</table>

<p class="note" style="margin-top:24px">本看板由 scripts/data_dashboard.py 生成，仅供研究监控使用。</p>
</div>

<script>
const P = {payload};
const axis={{axisLine:{{lineStyle:{{color:'#e6e8ee'}}}},axisLabel:{{color:'#7a8194'}},splitLine:{{lineStyle:{{color:'#eef0f4'}}}}}};
echarts.init(document.getElementById('chartFresh')).setOption({{
  grid:{{left:60,right:30,top:30,bottom:80}},
  tooltip:{{trigger:'axis'}},
  xAxis:{{type:'category',data:P.dom_names,...axis,axisLabel:{{rotate:40,fontSize:10,color:'#7a8194'}}}},
  yAxis:{{type:'value',name:'落后交易日数',...axis}},
  series:[{{type:'bar',data:P.dom_days,barMaxWidth:26,
    itemStyle:{{color:(p)=>p.value<=0?'#1d9e75':(p.value<=3?'#d97706':'#d8463e')}}}}]
}});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
