# -*- coding: utf-8 -*-
"""绝望人血馒头事件驱动策略 — 每日盘后运行（工作日 21:00）

策略口径（池I定版，2026-09-15 投产）：
  入池信号（数据日 D 收盘判定）：
    A. 2 日累计跌幅 >= 15%（复权，无上限）
    B. 或 3 日累计跌幅在 (20%, 30%] 区间
    C. 且 当日主力净流入占比 = 近 20 日最高（含当日）且 > 0
    D. 且 上市 >= 90 自然日（≈60 交易日，近似回测 bar_no 口径）
    E. 同股 3 自然日冷却（上次信号后 3 日内不重复）
  执行（模拟盘口径）：
    买入：信号日次日开盘价
    卖出：持有期内每日收盘检查主力净流入占比 <= -5% → 当日收盘卖出；
          或持有满 20 交易日兜底卖出
  组合：全仓等权日历组合；成本 买 22.5bp / 卖 27.5bp

生产声明（终审结论，2026-09-14/15 系列报告）：
  本策略超额来源 = 小市值（中位 16 亿）×（ST）×流动性危机反弹嵌套，
  剔 ST 后 t=0.9、再剔 30 亿以下后 t=0.6——不构成显著独立 alpha。
  本任务定位 = 信号跟踪与实盘模拟记录，非实盘交易指令。
  ST 状态与 mnp>50% 口径伪影在信号清单中作警示标注（不剔除，保持池I基准口径）。

用法：
  python scripts/despair_blood_bread_daily.py            # 全流程（计算+报告+企微发送）
  python scripts/despair_blood_bread_daily.py --skip-send # 只算不发（测试）
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATE_FILE = ROOT / "portfolio_state" / "despair_blood_bread.json"
REPORT_DIR = ROOT / "reports" / "despair"
MF_DIR = ROOT / "data" / "lake" / "clean" / "moneyflow"
NC_FILE = ROOT / "data" / "lake" / "clean" / "namechange" / "part-all.parquet"
DB_FILE = ROOT / "data" / "quant.duckdb"

EXIT_MF_TH = -0.05     # 主力净流占比 <= -5% 收盘卖出
MAX_HOLD = 20          # 20 交易日兜底
COST_BUY = 0.00225
COST_SELL = 0.00275
CHAT_ID = "woDLXKLgAAzvYbNh6VtDOj8oJ5Ci_fRA"

LOG = []


def log(msg: str) -> None:
    print(msg, flush=True)
    LOG.append(msg)


# ─────────────────────────── 数据准备 ───────────────────────────

def load_data() -> tuple[pd.DataFrame, date, str]:
    """返回 (panel, 数据日, 新鲜度说明)。panel 含单票按日序列所需全部列。"""
    con = duckdb.connect(str(DB_FILE), read_only=True)
    try:
        kmax = con.execute("select max(date) from kline_daily").fetchdf().iloc[0, 0]
        k = con.execute(f"""
            select date, code, open, high, low, close, vol, amount, adj_factor
            from kline_daily
            where date >= (select max(date) - 400 from kline_daily)
            order by code, date
        """).fetchdf()
        sc = con.execute(f"""
            select date, code, float_shares from share_capital_daily
            where date >= (select max(date) - 400 from share_capital_daily)
        """).fetchdf()
        nm = con.execute("select code, name, list_date from instruments").fetchdf()
    finally:
        con.close()

    mf_con = duckdb.connect()
    try:
        mf_files = sorted(MF_DIR.glob("part-*.parquet"))
        fs = ", ".join(f"'{f.as_posix()}'" for f in mf_files)
        mf = mf_con.execute(
            f"select date, code, main_net from read_parquet([{fs}], hive_partitioning=false)"
        ).fetchdf()
    finally:
        mf_con.close()

    k["date"] = pd.to_datetime(k["date"])
    sc["date"] = pd.to_datetime(sc["date"])
    mf["date"] = pd.to_datetime(mf["date"])
    mfmax = mf["date"].max()

    freshness = "OK"
    d_eff = min(kmax, mfmax)
    if kmax != mfmax:
        freshness = f"日线({kmax.date()})与资金流({mfmax.date()})不一致，按交集 {d_eff.date()} 计算"
        k = k[k["date"] <= d_eff]

    panel = k.merge(sc[["date", "code", "float_shares"]], on=["date", "code"], how="left")
    panel = panel.merge(mf, on=["date", "code"], how="left")
    panel = panel.merge(nm, on="code", how="left")
    panel["main_net_pct"] = panel["main_net"] * 1e4 / panel["amount"]
    panel["date"] = pd.to_datetime(panel["date"]).astype("datetime64[ns]")
    panel = panel.sort_values(["code", "date"]).reset_index(drop=True)
    return panel, d_eff.date(), freshness


def prepare_features(panel: pd.DataFrame) -> pd.DataFrame:
    """一次性预计算信号所需特征（供当日信号与近月信号重建复用）。"""
    g = panel.groupby("code", sort=False)
    panel["cum_2"] = (panel["close"] * panel["adj_factor"]) / (
        g["close"].shift(2) * g["adj_factor"].shift(2)) - 1
    panel["cum_3"] = (panel["close"] * panel["adj_factor"]) / (
        g["close"].shift(3) * g["adj_factor"].shift(3)) - 1
    panel["mf_20d_max"] = g["main_net_pct"].transform(
        lambda s: s.rolling(20, min_periods=10).max())
    panel["is_20d_high_mf"] = panel["main_net_pct"] >= panel["mf_20d_max"]
    # 上市>=60交易日近似：panel 覆盖约270交易日，行数>=60 即视为非次新
    # （instruments.list_date 全表为空不可用；停牌股行数少被保守排除，方向安全）
    panel["bar_cnt"] = g["close"].cumcount() + 1
    return panel


def signals_for_day(prepared: pd.DataFrame, d) -> pd.DataFrame:
    """在预计算面板上取某日截面信号（池I口径，不含冷却）。"""
    day = prepared[prepared["date"] == pd.Timestamp(d)].copy()
    if day.empty:
        return day
    m2 = day["cum_2"] <= -0.15
    m3 = (day["cum_3"] <= -0.20) & (day["cum_3"] > -0.30)
    base = ((m2 | m3) & (day["bar_cnt"] >= 60)
            & day["main_net_pct"].notna() & (day["main_net_pct"] > 0)
            & (day["is_20d_high_mf"] == True))  # noqa: E712
    sig = day[base].copy()
    if sig.empty:
        return sig
    m2s, m3s = m2[base], m3[base]
    sig["窗口"] = np.where(m2s & m3s, "2日+3日", np.where(m2s, "2日", "3日"))
    sig["2日跌幅%"] = (-sig["cum_2"] * 100).round(1)
    sig["3日跌幅%"] = (-sig["cum_3"] * 100).round(1)
    sig["主力净买%"] = (sig["main_net_pct"] * 100).round(1)
    return sig


def build_signals(panel: pd.DataFrame, d_eff: date) -> pd.DataFrame:
    """兼容入口：预计算 + 当日信号。"""
    return signals_for_day(prepare_features(panel), d_eff)


def mark_st_artifact(sig: pd.DataFrame) -> pd.DataFrame:
    """PIT ST 判定 + mnp>50% 伪影标记。"""
    if sig.empty:
        sig["ST"] = ""
        sig["伪影警示"] = ""
        return sig
    mf_con = duckdb.connect()
    try:
        nc = mf_con.execute(f"""
            select distinct code, start_date, end_date
            from read_parquet('{NC_FILE.as_posix()}', hive_partitioning=false)
            where name like '%ST%'
        """).fetchdf()
    finally:
        mf_con.close()
    nc["start_date"] = pd.to_datetime(nc["start_date"]).astype("datetime64[ns]")
    nc["end_date"] = (pd.to_datetime(nc["end_date"]).astype("datetime64[ns]")
                      .fillna(pd.Timestamp("2026-12-31")))
    st_map = defaultdict(list)
    for c, s, e in nc[["code", "start_date", "end_date"]].values.tolist():
        st_map[c].append((s, e))
    flags = []
    for _, r in sig.iterrows():
        f = ""
        for s, e in st_map.get(r["code"], []):
            if s <= r["date"] <= e:
                f = "ST"
                break
        flags.append(f)
    sig["ST"] = flags
    sig["伪影警示"] = np.where(sig["main_net_pct"] > 0.5, "mnp>50%", "")
    return sig


def track_recent_signals(prepared: pd.DataFrame, d_eff: date,
                         lookback_days: int = 30) -> pd.DataFrame:
    """最近 lookback_days 自然日内出现过的池I信号的前瞻表现持续跟踪。

    回溯口径：用同一信号引擎重建历史信号，再按策略规则模拟路径——
    信号次日开盘入场 → 持有期主力净流<=-5% 收盘出场 / 20 交易日兜底；
    数据未走完且未触发出场的标记"持有中"（实时视角，与回测期末强平不同）。
    同股 3 自然日冷却（与回测一致）。
    """
    d0 = pd.Timestamp(d_eff) - timedelta(days=lookback_days)
    all_dates = [d for d in sorted(prepared["date"].unique())
                 if d0 <= d < pd.Timestamp(d_eff)]
    frames = {c: s for c, s in prepared.groupby("code", sort=False)}
    rows = []
    last_sig: dict[str, pd.Timestamp] = {}
    for d in all_dates:
        sig = signals_for_day(prepared, d.date())
        if sig is None or sig.empty:
            continue
        sig = mark_st_artifact(sig)
        for _, r in sig.iterrows():
            code = str(r["code"])
            ls = last_sig.get(code)
            if ls is not None and (d - ls).days <= 3:
                continue
            last_sig[code] = d
            rec = {
                "信号日": d.strftime("%m-%d"), "代码": code,
                "名称": r.get("name", "") or "", "窗口": r["窗口"],
                "主力净买%": r["主力净买%"],
                "警示": (("ST " if r.get("ST") else "")
                         + (str(r.get("伪影警示") or ""))).strip(),
            }
            sub = frames.get(code)
            fwd = (sub[sub["date"] > d].reset_index(drop=True)
                   if sub is not None else None)
            if fwd is None or fwd.empty:
                rec.update({"状态": "待入场", "入场日": "—", "持有": 0,
                            "净收益%": None, "最大浮盈%": None, "最大浮亏%": None,
                            "出场": "—"})
                rows.append(rec)
                continue
            entry = fwd.iloc[0]
            entry_px = float(entry["open"]) * float(entry["adj_factor"])
            rec["入场日"] = entry["date"].strftime("%m-%d")
            max_up = max_dn = 0.0
            exit_j, reason = None, None
            n = len(fwd)
            for j in range(1, n):
                row_j = fwd.iloc[j]
                ret = (float(row_j["close"]) * float(row_j["adj_factor"])) / entry_px - 1
                max_up = max(max_up, ret)
                max_dn = min(max_dn, ret)
                if j >= MAX_HOLD:
                    exit_j, reason = j, "20日兜底"
                    break
                mnp = row_j["main_net_pct"]
                if not pd.isna(mnp) and mnp <= EXIT_MF_TH:
                    exit_j, reason = j, "主力流出"
                    break
            if exit_j is not None:
                row_e = fwd.iloc[exit_j]
                px = float(row_e["close"]) * float(row_e["adj_factor"])
                net = px / entry_px - 1 - COST_BUY - COST_SELL
                rec.update({"状态": "已出场", "持有": exit_j + 1,
                            "净收益%": round(net * 100, 1),
                            "最大浮盈%": round(max_up * 100, 1),
                            "最大浮亏%": round(max_dn * 100, 1),
                            "出场": reason})
            else:
                last = fwd.iloc[-1]
                px = float(last["close"]) * float(last["adj_factor"])
                net = px / entry_px - 1 - COST_BUY
                rec.update({"状态": "持有中", "持有": n,
                            "净收益%": round(net * 100, 1),
                            "最大浮盈%": round(max_up * 100, 1),
                            "最大浮亏%": round(max_dn * 100, 1),
                            "出场": "—"})
            rows.append(rec)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("信号日").reset_index(drop=True)


# ─────────────────────────── 状态机 ───────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"positions": [], "pending_buys": [], "trades": [],
            "equity": 1.0, "equity_history": [], "last_data_date": None, "start_date": None}


def save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def run_state_machine(st: dict, panel: pd.DataFrame, sig: pd.DataFrame,
                      d_eff: date) -> tuple[list, list, list]:
    """返回 (今日买入, 今日卖出, 持仓更新后明细)。panel 含最新两日。"""
    day_now = panel[panel["date"] == pd.Timestamp(d_eff)]
    px = {(r["code"]): r for _, r in day_now.iterrows()}
    # T-1（用于持有日收益）
    dates_sorted = sorted(panel["date"].unique())
    prev_date = dates_sorted[-2] if len(dates_sorted) >= 2 else None
    day_prev = panel[panel["date"] == prev_date] if prev_date is not None else day_now.iloc[0:0]
    ppx = {r["code"]: r for _, r in day_prev.iterrows()}

    bought, sold = [], []

    # 1) 昨日信号 → 今日开盘买入
    for p in st.get("pending_buys", []):
        c = p["code"]
        if c in px and not pd.isna(px[c]["open"]):
            r = px[c]
            st["positions"].append({
                "code": c, "name": p.get("name", ""),
                "signal_date": p["signal_date"],
                "buy_date": str(d_eff), "buy_price": float(r["open"]),
                "buy_adj": float(r["adj_factor"]),
                "main_net_pct_at_signal": p.get("main_net_pct"),
            })
            bought.append({"code": c, "name": p.get("name", ""), "buy_price": float(r["open"])})
    st["pending_buys"] = []

    # 2) 持仓检查：今日出场 / 更新浮盈
    still = []
    for pos in st["positions"]:
        c = pos["code"]
        if c not in px:
            still.append(pos)  # 今日停牌等，保留
            continue
        r = px[c]
        close_adj_now = float(r["close"]) * float(r["adj_factor"])
        entry = float(pos["buy_price"]) * float(pos["buy_adj"])
        pos_ret = close_adj_now / entry - 1
        mnp_today = float(r["main_net_pct"]) if not pd.isna(r["main_net_pct"]) else None
        hold_days = int((pd.Timestamp(d_eff) - pd.Timestamp(pos["buy_date"])).days)
        reason = None
        if mnp_today is not None and mnp_today <= EXIT_MF_TH:
            reason = "主力流出"
        elif hold_days >= MAX_HOLD * 1.6:  # 自然日近似兜底（20交易日≈28自然日）
            reason = "20日兜底"
        if reason:
            net = pos_ret - COST_BUY - COST_SELL
            st["trades"].append({
                "code": c, "name": pos.get("name", ""),
                "signal_date": pos["signal_date"], "buy_date": pos["buy_date"],
                "buy_price": pos["buy_price"], "sell_date": str(d_eff),
                "sell_price": float(r["close"]), "ret": round(net, 4), "reason": reason,
            })
            sold.append({"code": c, "name": pos.get("name", ""), "reason": reason,
                         "ret": round(net * 100, 1)})
        else:
            pos["float_ret"] = round(pos_ret - COST_BUY, 4)
            pos["last_date"] = str(d_eff)
            pos["last_close"] = float(r["close"])
            pos["mnp_today"] = None if mnp_today is None else round(mnp_today, 3)
            still.append(pos)
    st["positions"] = still

    # 3) 今日新信号 → pending（排除已持仓/今日已买）
    held = {p["code"] for p in st["positions"]} | {b["code"] for b in bought}
    last_sig = {}
    for t in st["trades"]:
        last_sig[t["code"]] = max(last_sig.get(t["code"], ""), t["sell_date"])
    for p in st["positions"]:
        last_sig[p["code"]] = max(last_sig.get(p["code"], ""), p["signal_date"])
    new_sigs = []
    for _, r in sig.iterrows():
        c = r["code"]
        if c in held:
            continue
        ls = last_sig.get(c)
        if ls and (pd.Timestamp(d_eff) - pd.Timestamp(ls)).days <= 3:
            continue
        st["pending_buys"].append({
            "code": c, "name": r.get("name", ""), "signal_date": str(d_eff),
            "main_net_pct": round(float(r["main_net_pct"]), 4),
        })
        new_sigs.append(r)
    return bought, sold, new_sigs


def update_equity(st: dict, panel: pd.DataFrame, d_eff: date) -> float | None:
    """简化净值：今日全部触票（持仓+今日买-今卖）等权日收益。"""
    dates_sorted = sorted(panel["date"].unique())
    if len(dates_sorted) < 2:
        return None
    prev_date = dates_sorted[-2]
    day_now = panel[panel["date"] == pd.Timestamp(d_eff)]
    day_prev = panel[panel["date"] == prev_date]
    rets = []
    touched = set()
    for t in st["trades"]:
        if t["sell_date"] == str(d_eff):
            touched.add(t["code"])
    for p in st["positions"]:
        touched.add(p["code"])
    for c in touched:
        rn = day_now[day_now["code"] == c]
        rp = day_prev[day_prev["code"] == c]
        if rn.empty:
            continue
        rn_, rp_ = rn.iloc[0], (rp.iloc[0] if not rp.empty else None)
        if rp_ is None or pd.isna(rp_["close"]):
            continue
        r = (rn_["close"] * rn_["adj_factor"]) / (rp_["close"] * rp_["adj_factor"]) - 1
        rets.append(float(r))
    if not rets:
        return None
    day_ret = float(np.mean(rets))
    st["equity"] = round(st["equity"] * (1 + day_ret), 6)
    st["equity_history"].append({"date": str(d_eff), "equity": st["equity"],
                                 "n": len(touched), "day_ret": round(day_ret, 5)})
    return day_ret


# ─────────────────────────── 报告 ───────────────────────────

def build_report(st: dict, sig: pd.DataFrame, bought: list, sold: list,
                 d_eff: date, freshness: str,
                 track: pd.DataFrame | None = None) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"despair_{d_eff.strftime('%Y%m%d')}.html"
    n_pos = len(st["positions"])
    float_avg = (np.mean([p.get("float_ret", 0) for p in st["positions"]]) * 100
                 if n_pos else 0.0)
    eq = st["equity"]

    def sig_rows(df):
        if df is None or (hasattr(df, "empty") and df.empty):
            return "<tr><td colspan='9' style='color:#8a8f98'>无</td></tr>"
        rows = []
        for _, r in df.iterrows():
            warn = ""
            if r.get("ST"):
                warn += f"<span class='w'>{r['ST']}</span> "
            if r.get("伪影警示"):
                warn += f"<span class='w'>{r['伪影警示']}</span>"
            rows.append(
                f"<tr><td>{r['code']}</td><td class='nm'>{r.get('name','')}</td>"
                f"<td>{r['窗口']}</td><td>{r['2日跌幅%']}</td><td>{r['3日跌幅%']}</td>"
                f"<td class='pos'>+{r['主力净买%']}%</td><td>{warn or '—'}</td></tr>")
        return "\n".join(rows)

    pos_rows = []
    for p in st["positions"]:
        fr = p.get("float_ret", 0) * 100
        cls = "pos" if fr > 0 else "neg"
        mnp = p.get("mnp_today")
        mnp_s = "—" if mnp is None else f"{mnp*100:+.1f}%"
        pos_rows.append(
            f"<tr><td>{p['code']}</td><td class='nm'>{p.get('name','')}</td>"
            f"<td>{p['buy_date']}</td><td>{p['buy_price']:.2f}</td>"
            f"<td>{p.get('last_close','-')}</td>"
            f"<td class='{cls}'>{fr:+.1f}%</td><td>{mnp_s}</td></tr>")
    pos_html = ("\n".join(pos_rows) if pos_rows
                else "<tr><td colspan='7' style='color:#8a8f98'>当前空仓</td></tr>")

    sold_html = ("\n".join(
        f"<tr><td>{s['code']}</td><td>{s['name']}</td><td>{s['reason']}</td>"
        f"<td class='{'pos' if s['ret']>0 else 'neg'}'>{s['ret']:+.1f}%</td></tr>"
        for s in sold) if sold else
        "<tr><td colspan='4' style='color:#8a8f98'>今日无出场</td></tr>")

    bought_html = ("\n".join(
        f"<tr><td>{b['code']}</td><td>{b['name']}</td><td>{b['buy_price']:.2f}</td></tr>"
        for b in bought) if bought else
        "<tr><td colspan='3' style='color:#8a8f98'>今日无买入</td></tr>")

    tr_rows = []
    for t in reversed(st["trades"][-15:]):
        cls = "pos" if t["ret"] > 0 else "neg"
        tr_rows.append(
            f"<tr><td>{t['code']}</td><td>{t['name']}</td><td>{t['buy_date']}</td>"
            f"<td>{t['sell_date']}</td><td>{t['reason']}</td>"
            f"<td class='{cls}'>{t['ret']*100:+.1f}%</td></tr>")
    tr_html = ("\n".join(tr_rows) if tr_rows
               else "<tr><td colspan='6' style='color:#8a8f98'>暂无已平仓交易</td></tr>")

    # 近一月信号持续跟踪
    if track is not None and not track.empty:
        trows = []
        for _, t in track.iterrows():
            ret = t["净收益%"]
            ret_s = "—" if ret is None or (isinstance(ret, float) and np.isnan(ret)) \
                else f"{ret:+.1f}%"
            cls = "pos" if (ret is not None and not pd.isna(ret) and ret > 0) else "neg"
            mu = t["最大浮盈%"]; md = t["最大浮亏%"]
            mu_s = "—" if mu is None or pd.isna(mu) else f"{mu:+.1f}%"
            md_s = "—" if md is None or pd.isna(md) else f"{md:+.1f}%"
            warn = (f"<span class='w'>{t['警示']}</span>" if t.get("警示") else "—")
            trows.append(
                f"<tr><td>{t['信号日']}</td><td>{t['代码']}</td>"
                f"<td class='nm'>{t['名称']}</td><td>{t['窗口']}</td>"
                f"<td>{t['主力净买%']:+.1f}%</td><td>{t['入场日']}</td>"
                f"<td>{t['状态']}</td><td>{t['持有']}</td>"
                f"<td class='{cls}'>{ret_s}</td>"
                f"<td class='pos'>{mu_s}</td><td class='neg'>{md_s}</td>"
                f"<td>{t['出场']}</td><td>{warn}</td></tr>")
        track_html = "\n".join(trows)
        ex = track[track["状态"] == "已出场"]
        hd = track[track["状态"] == "持有中"]
        track_sum = (f"近30日累计信号 {len(track)} 只：已出场 {len(ex)} 只"
                     f"（均值 {(ex['净收益%'].mean() if len(ex) else 0):+.1f}%，"
                     f"win {(ex['净收益%'].gt(0).mean() * 100 if len(ex) else 0):.0f}%）｜"
                     f"持有中 {len(hd)} 只"
                     f"（浮盈 {(hd['净收益%'].mean() if len(hd) else 0):+.1f}%）")
    else:
        track_html = "<tr><td colspan='13' style='color:#8a8f98'>近30日无信号</td></tr>"
        track_sum = "近30日无信号"

    fresh_cls = "okbox" if freshness == "OK" else "warnbox"
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>绝望人血馒头事件驱动策略 · {d_eff}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:#f5f6f8; color:#26282c; font-family:"Microsoft YaHei",sans-serif; line-height:1.7; }}
.wrap {{ max-width:1000px; margin:0 auto; padding:20px; }}
header {{ background:linear-gradient(135deg,#4a1220 0%,#7a1f2b 100%); color:#fff; border-radius:12px; padding:22px 28px; margin-bottom:16px; }}
header h1 {{ font-size:20px; }} header .meta {{ font-size:12.5px; opacity:.85; margin-top:6px; }}
.kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:10px; margin-bottom:16px; }}
.kpi {{ background:#fff; border-radius:8px; padding:12px; text-align:center; box-shadow:0 1px 3px rgba(0,0,0,.06); }}
.kpi b {{ font-size:19px; display:block; }} .kpi span {{ font-size:11px; color:#8a8f98; }}
section {{ background:#fff; border-radius:10px; padding:18px 22px; margin-bottom:14px; box-shadow:0 1px 3px rgba(0,0,0,.06); }}
section h2 {{ font-size:15px; border-left:4px solid #7a1f2b; padding-left:8px; margin-bottom:10px; }}
table {{ width:100%; border-collapse:collapse; font-size:12.5px; }}
th {{ background:#f0f2f6; padding:6px 8px; text-align:left; white-space:nowrap; }}
td {{ padding:5px 8px; border-bottom:1px solid #eceef2; white-space:nowrap; }}
.pos {{ color:#c23531; font-weight:600; }} .neg {{ color:#2f7d4f; font-weight:600; }}
.nm {{ font-weight:600; }} .w {{ background:#fdeaea; color:#c23531; font-size:11px; padding:1px 6px; border-radius:8px; }}
.okbox {{ background:#f0f7f2; border-left:4px solid #2f7d4f; padding:10px 14px; border-radius:6px; font-size:12.5px; margin-bottom:14px; }}
.warnbox {{ background:#fdf6e8; border-left:4px solid #d4a017; padding:10px 14px; border-radius:6px; font-size:12.5px; margin-bottom:14px; }}
footer {{ font-size:11.5px; color:#8a8f98; text-align:center; margin-top:20px; line-height:1.7; }}
</style></head><body><div class="wrap">

<header>
<h1>绝望人血馒头 事件驱动策略 · 每日跟踪</h1>
<div class="meta">池I口径：2日跌≥15% ∪ 3日跌20%~30% × 主力净流入占比20日新高 × 动态出场（主力流出≤−5% / 20日兜底）｜数据日 {d_eff}｜生成 {datetime.now():%Y-%m-%d %H:%M}</div>
</header>

<div class="{fresh_cls}">数据新鲜度：{freshness}</div>

<div class="kpis">
<div class="kpi"><b>{len(sig)}</b><span>今日新信号（次日开盘买）</span></div>
<div class="kpi"><b>{len(bought)}</b><span>今日买入</span></div>
<div class="kpi"><b>{len(sold)}</b><span>今日出场</span></div>
<div class="kpi"><b>{n_pos}</b><span>当前持仓</span></div>
<div class="kpi"><b class="{'pos' if float_avg>0 else 'neg'}">{float_avg:+.1f}%</b><span>持仓平均浮盈(成本后)</span></div>
<div class="kpi"><b class="{'pos' if eq>1 else 'neg'}">{eq:.3f}</b><span>跟踪净值（自 {st.get('start_date','—')}）</span></div>
</div>

<section><h2>一、明日买入清单（今日新信号）</h2>
<table><tr><th>代码</th><th>名称</th><th>窗口</th><th>2日跌幅%</th><th>3日跌幅%</th><th>主力净买%</th><th>警示</th></tr>
{sig_rows(sig)}</table>
<p style="font-size:11.5px;color:#8a8f98;margin-top:6px;">执行：明日开盘价买入。警示列 ST=该时点 ST 状态（PIT），mnp&gt;50%=分单口径伪影风险（成交额过小，分单"买额&gt;成交额"）。</p></section>

<section><h2>二、今日买入</h2>
<table><tr><th>代码</th><th>名称</th><th>买入价</th></tr>{bought_html}</table></section>

<section><h2>三、今日出场</h2>
<table><tr><th>代码</th><th>名称</th><th>出场原因</th><th>净收益</th></tr>{sold_html}</table></section>

<section><h2>四、当前持仓（{n_pos} 只）</h2>
<table><tr><th>代码</th><th>名称</th><th>买入日</th><th>买入价</th><th>最新收盘</th><th>浮盈(成本后)</th><th>今日主力净买</th></tr>
{pos_html}</table></section>

<section><h2>五、近一月信号持续跟踪（回溯口径）</h2>
<div class="okbox">{track_sum}</div>
<table><tr><th>信号日</th><th>代码</th><th>名称</th><th>窗口</th><th>主力净买%</th><th>入场日</th><th>状态</th><th>持有(日)</th><th>净收益%</th><th>最大浮盈%</th><th>最大浮亏%</th><th>出场</th><th>警示</th></tr>
{track_html}</table>
<p style="font-size:11.5px;color:#8a8f98;margin-top:6px;">回溯口径：用同一信号引擎重建近30日信号并按策略规则模拟路径（次日开盘入场→主力净流≤−5%收盘出场/20日兜底）；"持有中"=数据未走完且未触发出场（实时视角，与回测期末强平不同）。此表每日滚动重建，供观察信号实际兑现质量。</p></section>

<section><h2>六、最近已平仓交易</h2>
<table><tr><th>代码</th><th>名称</th><th>买入日</th><th>卖出日</th><th>出场</th><th>净收益</th></tr>{tr_html}</table></section>

<footer>
策略终审结论（2026-09-15 系列报告）：超额来源=小市值×(ST)×流动性危机反弹嵌套，剔ST后 t=0.9、再剔30亿以下 t=0.6——本任务为信号跟踪与模拟记录，<b>非实盘交易指令</b>。<br>
状态文件：portfolio_state/despair_blood_bread.json｜报告归档：reports/despair/｜口径与回测差异：上市天数用 list_date+90 自然日近似；净值简化为触票等权日收益。<br>
本报告基于公开数据和量化分析，仅供参考，不构成投资建议。市场有风险，投资需谨慎。
</footer>
</div></body></html>"""
    out.write_text(html, encoding="utf-8")
    return out


# ─────────────────────────── 企微发送 ───────────────────────────

def send_wecom(report_path: Path, st: dict, sig: pd.DataFrame, bought: list,
               sold: list, d_eff: date, freshness: str,
               track: pd.DataFrame | None = None) -> None:
    # 直接 node 执行 js 入口（绕过 cmd.exe 对含空格/引号 JSON 参数的转义 bug）
    node_exe = r"C:\Users\53497\.workbuddy\binaries\node\versions\24.14.0\node.exe"
    wecom_js = (r"C:\Users\53497\.workbuddy\binaries\node\cli-connector-packages"
                r"\node_modules\@wecom\cli\bin\wecom.js")
    if not Path(wecom_js).exists():
        log("[发送] 未找到 wecom.js 入口，跳过发送")
        return

    def cli(*args: str) -> str:
        r = subprocess.run([node_exe, wecom_js] + list(args),
                           capture_output=True, timeout=120,
                           encoding="utf-8", errors="replace")
        return (r.stdout or "") + (r.stderr or "")

    up = cli("media", "upload", "--json",
             json.dumps({"file_path": str(report_path)}))
    media_id = None
    try:
        media_id = json.loads(up).get("media_id")
    except Exception:
        pass
    if not media_id:
        log(f"[发送] HTML 上传失败：{up[:200]}")
    else:
        r1 = cli("message", "aibot", "send", "--json", json.dumps({
            "chat_id": CHAT_ID, "msg_type": "file",
            "file": {"media_id": media_id}}))
        log(f"[发送] HTML 文件: {'OK' if 'success' in r1 else r1[:150]}")

    n_pos = len(st["positions"])
    float_avg = (np.mean([p.get("float_ret", 0) for p in st["positions"]]) * 100
                 if n_pos else 0.0)
    sig_list = ""
    for _, r in (sig.head(8) if sig is not None else []).iterrows():
        warn = ("[ST]" if r.get("ST") else "") + ("[伪影]" if r.get("伪影警示") else "")
        sig_list += f"\n> {r['code']} {r.get('name','')}（{r['窗口']}跌 {r['主力净买%']:+.1f}%主力）{warn}"
    if len(sig) > 8 if sig is not None else False:
        sig_list += f"\n> ……共 {len(sig)} 只"
    track_line = "近30日信号跟踪：无信号"
    if track is not None and not track.empty:
        ex = track[track["状态"] == "已出场"]
        hd = track[track["状态"] == "持有中"]
        track_line = (f"近30日信号跟踪：累计{len(track)}只｜"
                      f"已出场{len(ex)}只（均值{(ex['净收益%'].mean() if len(ex) else 0):+.1f}%，"
                      f"win{(ex['净收益%'].gt(0).mean()*100 if len(ex) else 0):.0f}%）｜"
                      f"持有中{len(hd)}只（浮盈{(hd['净收益%'].mean() if len(hd) else 0):+.1f}%）")
    md = (f"**绝望人血馒头事件驱动策略 · {d_eff}**\n"
          f"数据新鲜度：{freshness}\n"
          f"**今日新信号 {len(sig) if sig is not None else 0} 只**（次日开盘买入）"
          + (sig_list if sig_list else "\n> 无") + "\n"
          f"今日买入 {len(bought)} / 出场 {len(sold)} / 持仓 {n_pos} 只\n"
          f"持仓平均浮盈 {float_avg:+.1f}%｜跟踪净值 {st['equity']:.3f}\n"
          f"{track_line}\n"
          f"完整报告见文件消息。定位=信号跟踪模拟，非实盘指令。")
    r2 = cli("message", "aibot", "send", "--json", json.dumps({
        "chat_id": CHAT_ID, "msg_type": "markdown",
        "markdown": {"content": md}}))
    log(f"[发送] 摘要: {'OK' if 'success' in r2 else r2[:150]}")


# ─────────────────────────── 主流程 ───────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-send", action="store_true")
    args = ap.parse_args()

    panel, d_eff, freshness = load_data()
    log(f"[数据] 数据日 {d_eff}，{freshness}，panel {len(panel)} 行")

    st = load_state()
    if st.get("last_data_date") == str(d_eff):
        log(f"[跳过] 数据日 {d_eff} 已处理过（休市或重复运行），不重复计信号")
        if not args.skip_send:
            log("[发送] 跳过（无新数据日）")
        return
    if st.get("start_date") is None:
        st["start_date"] = str(d_eff)

    panel = prepare_features(panel)
    sig = signals_for_day(panel, d_eff)
    sig = mark_st_artifact(sig)
    log(f"[信号] {d_eff} 新信号 {len(sig)} 只"
        + (f"：{'/'.join(sig['code'].astype(str))}" if not sig.empty else ""))

    track = track_recent_signals(panel, d_eff)
    if not track.empty:
        ex = track[track["状态"] == "已出场"]
        hd = track[track["状态"] == "持有中"]
        log(f"[跟踪] 近30日信号 {len(track)} 只：已出场 {len(ex)}"
            f"（均值 {(ex['净收益%'].mean() if len(ex) else 0):+.1f}%）"
            f"｜持有中 {len(hd)}"
            f"（{(hd['净收益%'].mean() if len(hd) else 0):+.1f}%）")
    else:
        log("[跟踪] 近30日无信号")

    bought, sold, _ = run_state_machine(st, panel, sig, d_eff)
    update_equity(st, panel, d_eff)
    st["last_data_date"] = str(d_eff)
    save_state(st)
    log(f"[状态] 买入{len(bought)} 出场{len(sold)} 持仓{len(st['positions'])} 净值{st['equity']:.4f}")

    report = build_report(st, sig, bought, sold, d_eff, freshness, track)
    log(f"[报告] {report}")

    if not args.skip_send:
        send_wecom(report, st, sig, bought, sold, d_eff, freshness, track)

    # 广播（项目内）
    try:
        summary = (f"绝望人血馒头策略 {d_eff}：新信号{len(sig)}只/买入{len(bought)}/"
                   f"出场{len(sold)}/持仓{len(st['positions'])}/净值{st['equity']:.3f}")
        subprocess.run(
            ["python", "scripts/broadcast.py", "add", "--category", "signal",
             "--action", "change", "--title", summary,
             "--detail", f"自动任务产出：{report}", "--source", "绝望人血馒头策略"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        log("[异常] " + traceback.format_exc()[-500:])
        raise
