#!/usr/bin/env python3
"""生成五层系统总览报告（自包含 HTML）
整合 L1 数据 → L2 因子 → L3 模型 → L4 优化 → L5 决策 的完整成果。
用法: python scripts/overview_report.py
输出: reports/overview_report.html

2026-09-09 重构 v2：十模型并列（详细四维介绍）+ 因子评估面板
  （近52周周均IC迷你图 + 近1年头部净值迷你图）+ 拥挤度监控表 +
  增强风险监控（覆盖闸门/账本健康）+ 扩充数据水位。
"""
import sys
import json
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from quantlab.config import REPORTS_DIR, LAKE_DIR
from quantlab.data.store import Store
from quantlab.factor import list_factors
from quantlab.model import build_composite
from quantlab.model.composite import FACTOR_DIRECTION
from quantlab.optimize import run_optimized_backtest
from quantlab.decision import load_state, generate_target

# 统一回测窗口：hf_amihud_20 分钟因子 2025-01 起才入湖，
# 全模型共用 2025-02 起的同窗口径保证可比性。
BT_START = "2025-02-01"


# ─────────────────────────── 模型登记表 ───────────────────────────
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
    # name: (定位标签, 简称, 回测口径备注, 四维详细介绍 dict)
    "PROD": ("现役", "size + amihud_20 双因子",
             "20 交易日调仓 · 逆波动加权 · top100",
             {"因子构成": "size（对数市值，做多小市值）+ amihud_20（20 日 Amihud "
                  "非流动性，做多低流动性）。",
              "建模方式": "两因子日内截面 pct-rank 等权合成 → ashare_ex 全池取 "
                  "top100 → 20 交易日调仓 → 逆波动加权 → 个股/行业风控约束。",
              "收益来源": "A 股小市值溢价 + 非流动性溢价，两大经典异象；两因子"
                  "相关性低，合成后分散增稳。",
              "特点": "现役基线，结构极简、换手温和（0.23/期）；2023 弱年仍有"
                  "正超额；主要风险为小市值拥挤（拥挤度监控持续跟踪）。"}),
    "PROD_SI": ("观察仓", "核心 + sue_i + overnight_mom_20 直加",
                "20 日调仓 · 逆波动 · top100",
                {"因子构成": "size + amihud_20 + sue_i（标准化盈余惊喜，财报"
                      "超预期幅度）+ overnight_mom_20（隔夜跳空动量）。",
                 "建模方式": "四因子 pct-rank 等权合成，链路与生产完全一致。",
                 "收益来源": "核心双溢价 + 盈余公告后漂移（PEAD）+ 隔夜情绪"
                      "动量，四路信息直接相加。",
                 "特点": "同窗直加组最强路线之一（63.3%/2.28）；overnight_mom "
                      "单腿全历史检验不过（2024 年负贡献），被剔出主切换候选，"
                      "此仓仅作直加 vs 条件融合的对照观察。"}),
    "V3_SI": ("观察仓", "条件融合 0.6×核心 + 0.4×SI 卫星块",
              "20 日调仓 · 逆波动 · top100",
              {"因子构成": "块 A=rank(size+amihud_20)；块 B=rank(sue_i+"
                    "overnight_mom_20)。",
               "建模方式": "两块各自合成后分别转日内 pct-rank，再按 0.6/0.4 "
                    "线性加权。",
               "收益来源": "核心双溢价为主，事件/情绪类信号以受限权重贡献"
                    "正交增量。",
               "特点": "比直加更保守的卫星暴露；同窗 58.2%/2.08，介于 PROD 与 "
                    "PROD_SI 之间——用于回答'卫星该给多大权重'。"}),
    "PROD_DUAL": ("观察仓", "两块式 0.9×核心 + 0.1×风格卫星块",
                  "20 日调仓 · 逆波动 · top100",
                  {"因子构成": "核心=size+amihud_20；卫星块=max_return_20、"
                        "amount_std_20、ev_high_vol_20、turnover_std_20"
                        "（2026-09-07 全池评估：与核心正交、ICIR 0.42-0.95）。",
                   "建模方式": "0.9×rank(核心) + 0.1×rank(卫星块)，卫星块四"
                        "因子等权。",
                   "收益来源": "核心双溢价为绝对主体，四个正交风格因子微调"
                        "截面排序。",
                   "特点": "全期回测略逊纯核心（年化 35.4% vs 36.9%），但 IC "
                        "逐年更稳（ICIR 0.47 vs 0.42）、2024 弱年更高——验证"
                        "'稳'与'强'的取舍。"}),
    "EQ3": ("切换候选", "核心 + sue_i 三因子等权",
            "20 日调仓 · 逆波动 · top100",
            {"因子构成": "size + amihud_20 + sue_i（标准化盈余惊喜）。",
             "建模方式": "三因子 pct-rank 等权合成，链路与生产一致。",
             "收益来源": "小市值 + 非流动性 + PEAD 三重溢价；sue_i 属基本面"
                  "事件信息源，与价格量类核心因子正交性最好。",
             "特点": "2026-09-07 第二轮修正后的主切换候选（overnight_mom 单腿"
                  "全历史稀释被剔除）；短窗夏普 2.33，sue_i_fast 扩展窗 "
                  "2024-07+ 夏普 2.55/OOS 2.60、四相位全优，两轮独立互证；"
                  "待 2026-12 双闸门裁决。"}),
    "PROD_HF": ("观察仓", "核心 + hf_amihud_20（分钟流动性精化）",
                "20 日调仓 · 逆波动 · top100",
                {"因子构成": "size + amihud_20 + hf_amihud_20（用分钟笔均价"
                      "与逐 bar 成交额重算的 Amihud，过滤高频微观结构噪声）。",
                 "建模方式": "三因子 pct-rank 等权，链路与生产一致。",
                 "收益来源": "分钟级数据对'非流动性溢价'的更精细度量——同一"
                      "经济含义的低频/高频双口径互证。",
                 "特点": "hf 系增量检验胜出者（同窗 65.5%/2.49，月配对胜率 "
                      "68% vs PROD）；依赖分钟湖（2025-01 起覆盖），历史样本短。"}),
    "PROD_HFA": ("切换候选", "核心 + sue_i + hf_amihud_20",
                 "20 日调仓 · 逆波动 · top100",
                 {"因子构成": "size + amihud_20 + sue_i + hf_amihud_20，四路"
                      "信息：价格量双溢价 + 基本面事件 + 分钟微观结构。",
                  "建模方式": "四因子 pct-rank 等权合成，链路与生产一致。",
                  "收益来源": "两类已独立验证的增量（PEAD + 分钟流动性精化）"
                       "同时叠加到核心之上。",
                  "特点": "同窗回测最强（71.7%/2.79/−20.7%），月配对胜率 78.9% "
                       "vs PROD，网格/滑点/容量全过；2026-09-07 第六轮定为切换"
                       "候选，与 EQ3 同场竞技待双闸门裁决。"}),
    "EQ3_HFA_ICW": ("稳健变体", "PROD_HFA 同因子 · ICIR 加权",
                    "20 日调仓 · 逆波动 · top100",
                    {"因子构成": "与 PROD_HFA 完全相同（size+amihud_20+sue_i+"
                          "hf_amihud_20）。",
                     "建模方式": "因子权重不用等权，改用滚动 IC 均值/标准差"
                          "（ICIR）动态定权，信号稳的因子权重大。",
                     "收益来源": "与 PROD_HFA 相同；差异仅在因子间权重分配。",
                     "特点": "稳健变体：同窗 67.9%/2.82/−17.0%，回撤最浅、2024 "
                          "股灾回放最抗跌；2026-12 裁决时与等权版二选一。"}),
    "PROD_HFA_W3": ("周调口径", "PROD_HFA · 周三周度调仓",
                    "每 5 交易日近似周调 · 逆波动 · top100",
                    {"因子构成": "与 PROD_HFA 完全相同。",
                     "建模方式": "因子与加权不变，仅把调仓频率从 20 交易日改"
                          "为每周三（回测以每 5 交易日近似）。",
                     "收益来源": "与 PROD_HFA 相同；差异来自调仓时点的星期"
                          "效应（7 组合平均周三最优、周五最差）。",
                     "特点": "换手降至 0.11/期（约为 20 日口径一半），容量与"
                          "成本友好；快照仅周三记录，纸面账本按周推进。"}),
    "DIV10": ("红利卫星仓", "红利官方规则池内 rc_prod Top10 等权",
              "月末截面月调 · 等权 · top10",
              {"因子构成": "池=中证红利官方编制方案本地复现（三年连续分红+"
                    "三年平均股息率 Top100+年调+缓冲区近似；市值/支付率筛选"
                    "因无历史股本跳过）；池内评分 rc_prod=size+amihud_20 精选。",
               "建模方式": "年度官方规则池缓存 → 月末截面池内 rc_prod 排序 "
                    "Top10 等权。",
               "收益来源": "高股息风格 beta + 池内小市值/流动性精选 alpha "
                    "（池基准 9.7%/0.66 → Top10 22.6%/0.93，三相位全正）。",
               "特点": "与核心模型暴露不同源（高股息 vs 小市值），相关性低；"
                    "裁决含义为卫星配置价值而非替换候选；历史池为官方规则"
                    "近似（2025 决策池与官方成分重合 49/100）。"}),
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


# ─────────────────────────── 因子评估面板 ───────────────────────────
def _spark(vals, color: str, w: int = 110, h: int = 28,
           zero: bool = False) -> str:
    """迷你走势 SVG（内联，无 JS 开销）"""
    v = [x for x in vals if pd.notna(x)]
    if len(v) < 8:
        return ""
    lo, hi = min(v), max(v)
    if zero:
        lo = min(lo, 0.0)
        hi = max(hi, 0.0)
    rng = (hi - lo) or 1e-9
    pts = " ".join(
        f"{w * i / (len(v) - 1):.1f},{h - 2 - (h - 4) * (x - lo) / rng:.1f}"
        for i, x in enumerate(v))
    zero_y = h - 2 - (h - 4) * (0 - lo) / rng
    zl = (f"<line x1='0' y1='{zero_y:.1f}' x2='{w}' y2='{zero_y:.1f}' "
          f"stroke='#3a4050' stroke-width='0.6' stroke-dasharray='2,2'/>"
          if zero and lo < 0 < hi else "")
    return (f"<svg width='{w}' height='{h}' class='spark'>"
            f"<rect width='{w}' height='{h}' fill='#12151c' rx='3'/>{zl}"
            f"<polyline points='{pts}' fill='none' stroke='{color}' "
            f"stroke-width='1.3'/></svg>")


def build_factor_panels(store: Store, flist: pd.DataFrame,
                        rmat: pd.DataFrame) -> list[dict]:
    """每因子：近 52 周周均 IC 迷你曲线 + 近 1 年头部净值迷你曲线 + 汇总指标

    IC 口径：日内 pct-rank Spearman IC20，日频 → W-FRI 周均（IC 需 20 日
    前瞻，末端天然滞后约 1 个月）。头部净值：方向调整 rank top100、
    每 5 交易日等权调仓、近 1 年。
    """
    t0 = time.time()
    fwd = store.q("""
        SELECT date, code, c_lead / c - 1 AS fwd FROM (
            SELECT date, code, close * adj_factor AS c,
                   LEAD(close * adj_factor, 20) OVER (
                       PARTITION BY code ORDER BY date) AS c_lead
            FROM kline_daily)
        WHERE c_lead IS NOT NULL AND c > 0""")
    fwd["date"] = pd.to_datetime(fwd["date"])

    end = rmat.index.max()
    win_start = end - pd.Timedelta(weeks=54)      # 52 周 IC + 1 年净值共用切片
    fwd = fwd[fwd["date"] >= win_start].dropna()
    fwd["fr"] = fwd.groupby("date")["fwd"].rank(pct=True)

    lake = LAKE_DIR.as_posix()
    panels = []
    for row in flist.to_dict("records"):
        name = row["name"]
        try:
            f = store.q(
                f"SELECT date, code, value FROM read_parquet("
                f"'{lake}/factor/{name}/part-*.parquet') WHERE date >= ?",
                [win_start])
            if f.empty:
                panels.append({**row, "ic_mean": None, "icir": None,
                               "ic_svg": None, "nav_svg": None})
                continue
            f["date"] = pd.to_datetime(f["date"])
            f["value"] = pd.to_numeric(f["value"], errors="coerce")
            f = f[np.isfinite(f["value"])].dropna()
            direction = FACTOR_DIRECTION.get(name, 1)
            f["r"] = f.groupby("date")["value"].rank(pct=True) * direction

            # ── 周均 IC（52 周）──
            j = f[["date", "code", "r"]].merge(
                fwd[["date", "code", "fwd", "fr"]],
                on=["date", "code"], how="inner")
            g = j.groupby("date")
            n = g.size().astype(float)
            mx, my = g["r"].mean(), g["fr"].mean()
            sxy = (j["r"] * j["fr"]).groupby(j["date"]).sum()
            sxx = (j["r"] ** 2).groupby(j["date"]).sum()
            syy = (j["fr"] ** 2).groupby(j["date"]).sum()
            cov = sxy / n - mx * my
            var = (sxx / n - mx ** 2) * (syy / n - my ** 2)
            ic = (cov / np.sqrt(var.clip(lower=1e-18)))[n >= 300]
            ic = ic[~ic.index.duplicated()].sort_index()
            wk = ic.resample("W-FRI").mean().dropna() if len(ic) else []
            ic_mean = float(ic.mean()) if len(ic) else None
            icir = (float(ic.mean() / ic.std())
                    if len(ic) > 5 and ic.std() > 0 else None)

            # ── 近 1 年头部净值（周调 top100 等权）──
            f1 = f[f["date"] >= end - pd.Timedelta(days=371)]
            dts = sorted(f1["date"].unique())
            nav = [1.0]
            nav_dates = []
            for i in range(0, len(dts) - 5, 5):
                d0, d1 = dts[i], dts[i + 5]
                s = f1[f1["date"] == d0]
                top = s.nlargest(100, "r")["code"].tolist()
                if not top:
                    continue
                seg = rmat.loc[d0:d1, [c for c in top
                                       if c in rmat.columns]].ffill()
                per = float((1 + seg.mean(axis=1).fillna(0)).prod() - 1)
                nav.append(nav[-1] * (1 + per))
                nav_dates.append(d1)
            nav_pts = nav[1:]
            panels.append({
                **row, "ic_mean": ic_mean, "icir": icir,
                "ic_svg": _spark(list(wk), "#378add", zero=True),
                "nav_svg": (_spark(nav_pts, "#e24b4a" if nav_pts[-1] >= 1
                                   else "#1d9e75") if nav_pts else None),
            })
        except Exception as e:                      # 单因子失败不阻塞报告
            import traceback
            print(f"因子面板 {name} 失败: {traceback.format_exc()[-300:]}",
                  flush=True)
            panels.append({**row, "ic_mean": None, "icir": None,
                           "ic_svg": None, "nav_svg": None,
                           "err": str(e)[:60]})
    n_err = sum(1 for p in panels if p.get("err"))
    print(f"因子面板 {len(panels)} 个（失败 {n_err}）({time.time()-t0:.0f}s)",
          flush=True)
    return panels


# ─────────────────────────── 主流程 ───────────────────────────
def l1_data(store):
    """扩充版数据水位：核心行情/财务/事件/情绪/账户全表"""
    tables = [
        ("kline_daily", "date", "日线行情"),
        ("kline_1min", "datetime", "分钟线（湖视图）"),
        ("minute_feat", "date", "分钟特征宽表"),
        ("index_kline", "date", "指数日线"),
        ("finance_snapshot", "report_date", "财务快照"),
        ("perf_forecast", "report_date", "业绩预告"),
        ("dividend_events", "date", "分红除权事件"),
        ("fund_flow_daily", "date", "个股资金流"),
        ("margin_total", "date", "两融余额"),
        ("northbound_daily", "date", "北向资金"),
        ("block_trade", "date", "大宗交易"),
        ("dragon_tiger", "date", "龙虎榜"),
        ("hot_topic", "date", "热点题材"),
        ("holder_num", "end_date", "股东户数"),
        ("daily_snapshot", "date", "每日快照"),
        ("instruments", "updated_at", "证券主档"),
        ("trade_calendar", "trade_date", "交易日历"),
    ]
    today = pd.Timestamp.now().normalize()
    rows = []
    for t, date_col, label in tables:
        try:
            df = store.q(f"SELECT COUNT(*) n, MAX({date_col}) mx FROM {t}")
            n, mx = int(df.iloc[0, 0]), df.iloc[0, 1]
            mx_s = str(mx)[:10] if pd.notna(mx) else "-"
            lag = ((today - pd.Timestamp(mx).normalize()).days
                   if pd.notna(mx) else None)
            rows.append({"table": t, "label": label, "rows": n,
                         "latest": mx_s, "lag": lag})
        except Exception:
            rows.append({"table": t, "label": label, "rows": 0,
                         "latest": "-", "lag": None})
    return rows


def load_crowding():
    """读流水线持久化的拥挤度 latest.json（流水线每日已跑批）"""
    p = Path(LAKE_DIR) / "crowding" / "latest.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def main():
    t0 = time.time()
    store = Store()

    # L1
    data_rows = l1_data(store)
    # L2
    flist = list_factors()

    # ── 模型层：十模型并列 ──
    scores = build_model_scores(store)
    # 共享价格宽表（全部回测/因子面板只读一次 kline_daily 行情）
    px = store.q("SELECT date, code, close*adj_factor AS c FROM kline_daily")
    px["date"] = pd.to_datetime(px["date"])
    pmat = px.pivot(index="date", columns="code", values="c").sort_index()
    rmat = pmat.pct_change()

    # 中证1000 收盘序列（纸面账本超额基准 + 图表基准线）
    bm = store.q(
        "SELECT date, close FROM index_kline "
        "WHERE code='000852.SH' ORDER BY date")
    bm["date"] = pd.to_datetime(bm["date"])
    bm_close = bm.set_index("date")["close"].astype(float)

    model_rows = []
    payload_bt = []
    payload_bm = None
    for name, score in scores.items():
        tag, short, note, info = MODEL_META[name]
        n = 10 if name == "DIV10" else 100
        rb = 5 if name == "PROD_HFA_W3" else (1 if name == "DIV10" else 20)
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
                "info": info,
                "ann": m["annual_return"], "sharpe": m["sharpe"],
                "mdd": m["max_drawdown"], "turnover": r["turnover_avg"],
                "ic": ic["ic"], "icir": ic["icir"], "err": None})
        except Exception as e:
            model_rows.append({
                "name": name, "tag": tag, "short": short, "note": note,
                "info": info,
                "ann": None, "sharpe": None, "mdd": None, "turnover": None,
                "ic": None, "icir": None, "err": str(e)[:120]})

    # L5 决策：目标持仓（现役）
    cur = load_state()
    tgt = generate_target(scores["PROD"])

    # 纸面前向账本：全模型（共享价格宽表）
    from quantlab.decision.tracker import _mark_to_market
    sig_all = store.q(
        "SELECT model, date, code, weight FROM signal_portfolio_multi "
        "ORDER BY model, date")
    paper_curves: dict[str, pd.DataFrame] = {}
    paper_starts: dict[str, str] = {}
    if not sig_all.empty:
        for mdl, g in sig_all.groupby("model"):
            c = _mark_to_market(g[["date", "code", "weight"]], px=px)
            if not c.empty:
                paper_curves[mdl] = c
                # 基准对齐起点 = 该模型首次快照日（持仓自当日起算）
                paper_starts[mdl] = str(pd.Timestamp(g["date"].min()).date())
    prod_curve = paper_curves.get("PROD")

    # 因子评估面板（52 周周均 IC + 1 年头部净值迷你图）
    factor_panels = build_factor_panels(store, flist, rmat)

    # 风险监控数据
    crowding = load_crowding()
    try:
        from quantlab.model.composite import factor_coverage
        cov = factor_coverage(
            ["size", "amihud_20", "sue_i", "overnight_mom_20",
             "hf_amihud_20"])
    except Exception:
        cov = pd.DataFrame()
    try:
        ledger = store.q(
            "SELECT model, COUNT(DISTINCT date) snaps, MAX(date) last_day "
            "FROM signal_portfolio_multi GROUP BY model ORDER BY model")
    except Exception:
        ledger = pd.DataFrame()

    # 选股器数据：各模型最新信号日快照（代码/名称/权重）
    screener = {}
    for m in model_rows:
        snap = _latest_snapshot(None if m["name"] == "PROD" else m["name"])
        if snap is not None:
            d, h = snap
            screener[m["name"]] = {
                "date": d,
                "stocks": [[r["code"], str(r.get("name", "")),
                            round(float(r["weight"]), 4)]
                           for r in h.to_dict("records")]}

    html = render(data_rows, flist, factor_panels, model_rows, payload_bt,
                  payload_bm, tgt, paper_curves, prod_curve, bm_close,
                  paper_starts, screener, crowding, cov, ledger)
    out = REPORTS_DIR / "overview_report.html"
    out.write_text(html, encoding="utf-8")
    print(f"报告已生成: {out} ({time.time()-t0:.0f}s)")


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


def render(data_rows, flist, factor_panels, model_rows, payload_bt,
           payload_bm, tgt, paper_curves, prod_curve, bm_close,
           paper_starts, screener, crowding, cov, ledger):
    # ── 数据水位（含滞后天数）──
    data_tr = ""
    for r in data_rows:
        if r["lag"] is None:
            lag_s = "<span class='muted'>—</span>"
        elif r["lag"] <= 1:
            lag_s = f"<span class='pos'>T{r['lag']}</span>"
        elif r["lag"] <= 7:
            lag_s = f"<span class='muted'>{r['lag']} 天</span>"
        else:
            lag_s = f"<span class='neg'>{r['lag']} 天</span>"
        data_tr += (f"<tr><td><code>{r['table']}</code></td>"
                    f"<td class='lt'>{r['label']}</td>"
                    f"<td>{r['rows']:,}</td><td>{r['latest']}</td>"
                    f"<td>{lag_s}</td></tr>")

    # ── 因子评估面板（78 行 × 迷你图）──
    def _ic(v):
        return f"{v:+.3f}" if v is not None else "—"

    def _icir(v):
        return "—" if v is None else f"{round(v, 2):.2f}"

    factor_rows = ""
    n_ic = 0
    for p in factor_panels:
        if p["ic_mean"] is not None:
            n_ic += 1
        ic_cls = "pos" if (p["ic_mean"] or 0) > 0 else "neg"
        err_note = (f" <span class='muted'>(计算失败: {p['err']})</span>"
                    if p.get("err") else "")
        factor_rows += (
            f"<tr><td><code>{p['name']}</code></td><td>{p['category']}</td>"
            f"<td class='{ic_cls}'>{_ic(p['ic_mean'])}</td>"
            f"<td>{_icir(p['icir'])}</td>"
            f"<td>{p['ic_svg'] or '—'}</td>"
            f"<td>{p['nav_svg'] or '—'}</td>"
            f"<td class='lt'>{p['description']}{err_note}</td></tr>")

    # ── 模型一览：详细四维介绍卡片 ──
    model_cards = ""
    for m in model_rows:
        info_html = "".join(
            f"<div class='mrow'><span class='mlabel'>{k}</span>"
            f"<span>{v}</span></div>" for k, v in m["info"].items())
        model_cards += (
            f"<div class='mcard'>"
            f"<div class='mhead'><b>{m['name']}</b>"
            f"<span class='tag {TAG_CLS.get(m['tag'], 'tag-watch')}'>"
            f"{m['tag']}</span>"
            f"<span class='muted'>{m['short']} · {m['note']}</span></div>"
            f"{info_html}</div>")

    # ── 拥挤度表（流水线每日跑批持久化结果）──
    crowd_html = "<p class='sub'>拥挤度数据不可用（流水线未生成 latest.json）。</p>"
    if crowding:
        latest = crowding.get("latest", {})
        alerts = set(crowding.get("alerts", []))
        gate = crowding.get("gate", {})
        rows = ""
        for name, v in sorted(latest.items(),
                              key=lambda kv: -kv[1].get("hist_pct", 0)):
            hot = v.get("hist_pct", 0) >= 0.8
            rows += (
                f"<tr><td><code>{name}</code></td>"
                f"<td>{v.get('group', '')}</td>"
                f"<td class='{'neg' if hot else ''}'>"
                f"{v.get('crowding', 0):+.2f}</td>"
                f"<td class='{'neg' if hot else ''}'>"
                f"{v.get('hist_pct', 0):.0%}</td>"
                f"<td>{v.get('val_z', 0):+.2f}</td>"
                f"<td>{v.get('corr_z', 0):+.2f}</td>"
                f"<td>{v.get('turnover_pct', 0):.0%}</td>"
                f"<td>{'<span class=\"tag tag-live\">预警</span>' if name in alerts else '<span class=muted>正常</span>'}</td></tr>")
        w_cur = gate.get("w_current")
        pct_cur = gate.get("pct_current")
        gate_s = (f"核心组拥挤度门控：w={w_cur:.2f}"
                  f"（因果分位 {pct_cur:.0%}，示意口径）。" if w_cur else "")
        crowd_html = (
            f"<table><thead><tr><th>因子</th><th>组</th><th>拥挤度 z</th>"
            f"<th>历史分位</th><th>估值 z</th><th>配对相关 z</th>"
            f"<th>换手分位</th><th>状态</th></tr></thead><tbody>{rows}"
            f"</tbody></table>"
            f"<p class='sub'>{gate_s}预警阈值：历史分位 ≥80%；"
            f"预警=关注暴露，非减仓信号。数据截至 "
            f"{next(iter(latest.values())).get('date', '—') if latest else '—'}"
            f"（周度采样）。</p>")

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

    # ── 纸面前向账本（含中证1000 超额）──
    paper_payload = []
    for name, c in paper_curves.items():
        pts = [[str(pd.Timestamp(d).date()), round(float(v), 4)]
               for d, v in zip(c["date"], c["nav"])]
        paper_payload.append([name, pts])

    # 基准：窗口 = [该模型首次快照日, 末次盯市日]，起点归一 1.0
    def _bm_nav(start, end):
        w = bm_close.loc[start:end]
        if w.empty:
            return pd.Series(dtype=float)
        return w / w.iloc[0]

    # 图表基准线：全部账本窗口 [最早快照日, 最晚盯市日]
    pbm_pts = []
    if paper_curves and paper_starts:
        g_start = min(paper_starts.values())
        g_end = max(pd.to_datetime(c["date"]).max() for c in paper_curves.values())
        bnav = _bm_nav(g_start, g_end)
        pbm_pts = [[str(pd.Timestamp(d).date()), round(float(v), 4)]
                   for d, v in bnav.items()]

    paper_meta = {}
    prod_cum = None
    for name, c in paper_curves.items():
        cum = float(c["nav"].iloc[-1] - 1)
        bnav = _bm_nav(paper_starts.get(name, c["date"].iloc[0]),
                       c["date"].max())
        bm_cum = (float(bnav.iloc[-1] - 1) if len(bnav) else 0.0)
        if name == "PROD":
            prod_cum = cum
        paper_meta[name] = {"days": len(c), "cum": cum, "bm_cum": bm_cum,
                            "start": str(pd.Timestamp(c['date'].iloc[0]).date())}
    paper_rows = ""
    for m in model_rows:
        name = m["name"]
        info = paper_meta.get(name)
        if info is None:
            paper_rows += (f"<tr><td><b>{name}</b></td>"
                           f"<td>积累中（快照不足 2 期）</td><td>—</td><td>—</td><td>—</td></tr>")
            continue
        rel = (info["cum"] - prod_cum
               if prod_cum is not None and name != "PROD" else None)
        rel_s = "—" if rel is None else f"{rel:+.2%}"
        exc = info["cum"] - info["bm_cum"]
        cum_cls = "pos" if info["cum"] >= 0 else "neg"
        exc_cls = "pos" if exc >= 0 else "neg"
        paper_rows += (
            f"<tr><td><b>{name}</b>"
            f"<span class='tag {TAG_CLS.get(m['tag'], 'tag-watch')}'>{m['tag']}</span></td>"
            f"<td>{info['days']} 日（{info['start']}~）</td>"
            f"<td class='{cum_cls}'>{info['cum']:+.2%}</td>"
            f"<td class='{exc_cls}'>{exc:+.2%}</td>"
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

    # ── 风险监控：覆盖闸门 + 账本健康 + 拥挤预警摘要 ──
    cov_html = "<p class='sub'>覆盖闸门数据不可用。</p>"
    if not cov.empty:
        rows = ""
        for r in cov.itertuples():
            ok = bool(getattr(r, "ok", True))
            behind = getattr(r, "behind_days", 0)
            rows += (
                f"<tr><td><code>{r.factor}</code></td>"
                f"<td>{str(getattr(r, 'f_max', '—'))[:10]}</td>"
                f"<td class='{'pos' if behind == 0 else 'neg'}'>"
                f"{behind} 天</td>"
                f"<td>{getattr(r, 'n_max', '—')}</td>"
                f"<td>{'<span class=pos>ok</span>' if ok else '<span class=neg>落后</span>'}"
                f"</td></tr>")
        cov_html = (f"<table><thead><tr><th>因子</th><th>最新日期</th>"
                    f"<th>落后</th><th>覆盖股票数</th><th>状态</th></tr></thead>"
                    f"<tbody>{rows}</tbody></table>"
                    "<p class='sub'>任一依赖因子落后 → 流水线防降级闸门自动跳过"
                    "候选模型记账（防快照口径降级）。</p>")

    ledger_rows = ""
    if not ledger.empty:
        for r in ledger.itertuples():
            ledger_rows += (f"<tr><td><b>{r.model}</b></td>"
                            f"<td>{r.snaps} 期</td>"
                            f"<td>{str(r.last_day)[:10]}</td></tr>")
        ledger_html = (f"<table><thead><tr><th>模型</th><th>快照期数</th>"
                       f"<th>最新快照日</th></tr></thead><tbody>{ledger_rows}"
                       f"</tbody></table>")
    else:
        ledger_html = "<p class='sub'>账本数据不可用。</p>"

    crowd_summary = "无预警"
    if crowding and crowding.get("alerts"):
        crowd_summary = "、".join(crowding["alerts"]) + \
            "（≥80% 分位，关注非减仓）"

    crowd_section = (
        '<h2>七、风险监控</h2>'
        f'<p><b>拥挤度预警</b>：{crowd_summary}；全量指标见第三节拥挤度表。</p>'
        '<h3>7.1 因子覆盖闸门（防降级）</h3>' + cov_html +
        '<h3>7.2 纸面账本健康</h3>' + ledger_html)

    # ── 头部卡片 ──
    sig_day = max(paper_meta.values(), key=lambda x: x["start"],
                  default=None)
    sig_day_s = sig_day["start"] if sig_day else "—"

    top10 = tgt.head(10)[[c for c in ("code", "name", "weight")
                          if c in tgt.columns]]
    top10_tr = "".join(
        f"<tr><td><code>{r['code']}</code></td><td>{r.get('name', '')}</td>"
        f"<td>{r['weight']:.2%}</td></tr>" for r in top10.to_dict("records"))

    payload = json.dumps({
        "bt": payload_bt, "bm": payload_bm, "paper": paper_payload,
        "pbm": pbm_pts, "scr": screener,
    }, ensure_ascii=False)

    return TMPL.format(
        n_factors=len(flist), n_models=len(model_rows), n_ic=n_ic,
        data_tr=data_tr, factor_rows=factor_rows,
        model_cards=model_cards, crowd_html=crowd_html,
        bt_rows=bt_rows, paper_rows=paper_rows, hold_section=hold_section,
        crowd_section=crowd_section, top10_tr=top10_tr,
        track_days=track_days, sig_day=sig_day_s, tgt_n=len(tgt),
        payload=payload).replace("%%SCR%%", SCR_BLOCK)


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
.wrap{{max-width:1280px;margin:0 auto;}}
h1{{font-size:26px;font-weight:600;}}
h2{{font-size:18px;font-weight:600;margin:40px 0 16px;padding-left:12px;border-left:3px solid var(--accent);}}
h3{{font-size:15px;font-weight:600;margin:24px 0 10px;color:#b9c2d0;}}
.sub{{color:var(--muted);font-size:14px;margin-top:6px;}}
.cards{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:24px 0;}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;}}
.card .l{{color:var(--muted);font-size:13px;}}
.card .v{{font-size:22px;font-weight:600;margin-top:4px;}}
table{{width:100%;border-collapse:collapse;margin:12px 0;font-size:13px;}}
th,td{{padding:7px 10px;text-align:right;border-bottom:1px solid var(--line);vertical-align:middle;}}
th{{color:var(--muted);font-weight:500;}}
td:first-child,th:first-child{{text-align:left;}}
td.lt{{text-align:left;color:var(--muted);font-size:12px;line-height:1.5;}}
code{{font-family:ui-monospace,Consolas,monospace;color:#7fb2e5;}}
.chart{{width:100%;height:460px;margin:8px 0;}}
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
.spark{{display:block;}}
.mcard{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 18px;margin:12px 0;}}
.mhead{{font-size:15px;margin-bottom:8px;}}
.mrow{{display:grid;grid-template-columns:88px 1fr;gap:10px;padding:3px 0;font-size:13px;border-top:1px dashed #222835;}}
.mlabel{{color:var(--muted);flex-shrink:0;}}
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
<table>
<thead><tr><th>表</th><th>说明</th><th>行数</th><th>最新日期</th><th>滞后</th></tr></thead>
<tbody>{data_tr}</tbody>
</table>
<p class="sub">滞后 = 最新日期距今天数（T0/T1=当日/昨日正常）；分钟线为湖视图统计。</p>

<span class="layer">L2 因子层</span>
<h2>二、内置因子评估（{n_factors} 个）</h2>
<p class="sub">IC 口径：日内 pct-rank Spearman IC20，取近 52 周周均（IC 需 20 日前瞻，
末端滞后约 1 个月）；头部净值：按因子方向取 rank top100、每 5 交易日等权调仓、
近 1 年区间；迷你图红线上行=正贡献。{n_ic} 个因子有足够 IC 样本。</p>
<table>
<thead><tr><th>因子</th><th>类别</th><th>IC20 均值</th><th>ICIR</th>
<th>周均 IC · 52周</th><th>头部净值 · 1年</th><th>说明</th></tr></thead>
<tbody>{factor_rows}</tbody>
</table>

<span class="layer">L3 模型层 · 十模型并列</span>
<h2>三、模型一览</h2>
<p class="sub">全部生产编制模型统一地位：现役=当前实盘口径；切换候选=双闸门裁决对象；
观察仓=积累前向样本；周调口径/卫星仓=差异化配置研究。</p>
{model_cards}

<h3>拥挤度监控（监控池全量）</h3>
{crowd_html}

<h2>四、同窗回测对比（{bt_start} 起 · 中证1000 基准）</h2>
<p class="sub">统一窗口受分钟因子覆盖约束（hf_amihud_20 于 2025-01 入湖）。
点击图例可单独聚焦任一模型；DIV10 为月调 top10 等权、高股息暴露，曲线仅作横向参考。</p>
<div id="chartBT" class="chart"></div>
<table>
<thead><tr><th>模型</th><th>年化</th><th>夏普</th><th>最大回撤</th>
<th>换手/期</th><th>IC20</th><th>ICIR20</th></tr></thead>
<tbody>{bt_rows}</tbody>
</table>

<span class="layer">L5 决策层</span>
<h2>五、纸面前向账本（全模型）</h2>
<p class="sub">信号快照记录于每日流水线，逐日盯市，与回测独立的前向验证账本。
账本自 2026-09-07 同日起算，满 60 交易日后（约 2026-12）双闸门裁决；
所有模型同图呈现（灰虚线=中证1000），点击图例聚焦。超额 = 累计收益 − 同窗口基准收益。</p>
<div id="chartPaper" class="chart" style="height:380px"></div>
<table>
<thead><tr><th>模型</th><th>纸面天数</th><th>累计收益</th><th>超额 vs 中证1000</th><th>相对现役 PROD</th></tr></thead>
<tbody>{paper_rows}</tbody>
</table>

{hold_section}

{crowd_section}

<h2>八、选股器</h2>
<p class="sub">自选观察工具：勾选模型并选择组合逻辑——<b>且</b> = 交集（同时出现在所有选中模型的最新持仓），
<b>或</b> = 并集（出现在任一选中模型）。结果按命中模型数与权重降序，权重为各模型最新信号日快照。</p>
<div id="scrBox"></div>
<div id="scrOut"></div>

<p class="note" style="margin-top:24px">本报告由 QuantLab 五层流水线自动生成（scripts/overview_report.py，v3 每日标准范本）。仅供研究，不构成投资建议。</p>
</div>

<script>
const P = {payload};
const COLORS = {{PROD:'#e24b4a','PROD_SI':'#e08c4a','V3_SI':'#d9c04a','PROD_DUAL':'#9ecf4a',
'EQ3':'#4ad499','PROD_HF':'#4ac9d4','PROD_HFA':'#4a8fe0','EQ3_HFA_ICW':'#7a6ce0',
'PROD_HFA_W3':'#c66ce0',DIV10:'#e0b45c'}};
const gray='#9aa3b2', line='#2a2f3a';
const axis={{axisLine:{{lineStyle:{{color:line}}}},axisLabel:{{color:gray}},splitLine:{{lineStyle:{{color:line}}}}}};

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
  if(P.pbm && P.pbm.length) series.push({{name: '中证1000', type: 'line',
    smooth: false, symbol: 'none',
    lineStyle: {{width: 1.5, color: gray, type: 'dashed'}},
    itemStyle: {{color: gray}}, emphasis: {{focus: 'series'}},
    data: P.pbm}});
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

%%SCR%%
</body>
</html>
""".replace("{bt_start}", BT_START)


# ─────────────────────────── 选股器（原始字符串，不参与 format）───────────────────────────
SCR_BLOCK = """
<style>
.scrBox{display:flex;flex-wrap:wrap;gap:10px;align-items:center;background:#171a21;
border:1px solid #2a2f3a;border-radius:10px;padding:12px 16px;margin:10px 0;}
.scrItem{display:inline-flex;align-items:center;gap:6px;font-size:13px;cursor:pointer;user-select:none;}
.scrItem input{accent-color:#378add;cursor:pointer;}
.scrSep{width:1px;height:18px;background:#2a2f3a;margin:0 4px;}
.btn{background:#1c3a5e;color:#7fb2e5;border:1px solid #2b5583;border-radius:6px;
padding:5px 16px;font-size:13px;cursor:pointer;}
.btn:hover{background:#244b79;}
.btn.ghost{background:transparent;color:#9aa3b2;border-color:#2a2f3a;}
</style>
<script>
(function(){
  const scr = P.scr || {};
  const models = Object.keys(scr);
  const box = document.getElementById('scrBox');
  const out = document.getElementById('scrOut');
  if(!models.length){ box.innerHTML = "<p class='sub'>暂无模型快照。</p>"; return; }
  box.className = 'scrBox';
  box.innerHTML = models.map(m => {
    const d = scr[m], has = d.stocks && d.stocks.length;
    return `<label class="scrItem"><input type="checkbox" value="${m}" data-auto="1" ${has?'':'disabled'} ${m==='PROD'?'checked':''}><b style="color:${COLORS[m]||'#9aa3b2'}">${m}</b><span class="muted" style="margin-left:0">${has? d.stocks.length+' 只 · '+d.date : '无快照'}</span></label>`;
  }).join('')
  + `<span class="scrSep"></span>
  <label class="scrItem"><input type="radio" name="scrLogic" value="and" checked><b>且</b><span class="muted" style="margin-left:0">交集</span></label>
  <label class="scrItem"><input type="radio" name="scrLogic" value="or"><b>或</b><span class="muted" style="margin-left:0">并集</span></label>
  <span class="scrSep"></span>
  <button class="btn" id="scrRun">筛选</button>
  <button class="btn ghost" id="scrAll">全选/清空</button>`;

  function run(){
    const checked = [...box.querySelectorAll('input[type=checkbox]:checked')].map(i=>i.value);
    const logic = box.querySelector('input[name=scrLogic]:checked').value;
    if(!checked.length){ out.innerHTML = "<p class='sub'>请至少勾选一个模型。</p>"; return; }
    const hit = {};
    for(const m of checked){
      for(const st of scr[m].stocks){
        const code = st[0], nm = st[1], w = st[2];
        if(!hit[code]) hit[code] = {name: nm, w: {}};
        hit[code].w[m] = w;
      }
    }
    let codes;
    if(logic === 'and'){
      codes = Object.keys(hit).filter(c => Object.keys(hit[c].w).length === checked.length);
    } else {
      codes = Object.keys(hit);
    }
    if(!codes.length){
      out.innerHTML = "<p class='sub'>无满足条件的股票" +
        (logic==='and' ? "（交集为空，可尝试「或」或减少模型）" : "。") + "</p>";
      return;
    }
    const totalW = c => Object.values(hit[c].w).reduce((a,b)=>a+b,0);
    codes.sort((a,b) => (Object.keys(hit[b].w).length - Object.keys(hit[a].w).length)
                        || (totalW(b) - totalW(a)));
    const rows = codes.map(c => {
      const h = hit[c];
      const det = checked.filter(m => h.w[m] !== undefined)
        .map(m => `<span style="color:${COLORS[m]||'#9aa3b2'}">${m} ${(h.w[m]*100).toFixed(2)}%</span>`)
        .join(' · ');
      return `<tr><td><code>${c}</code></td><td>${h.name||''}</td><td>${Object.keys(h.w).length}/${checked.length}</td><td style="text-align:left">${det}</td></tr>`;
    }).join('');
    out.innerHTML = `<p class="sub">命中 <b>${codes.length}</b> 只 · 逻辑= ${logic==='and'?'且（交集）':'或（并集）'} · 模型 = ${checked.join(', ')}</p>
    <table><thead><tr><th>代码</th><th>名称</th><th>命中</th><th style="text-align:left">各模型权重（最新信号日）</th></tr></thead><tbody>${rows}</tbody></table>`;
  }
  box.querySelector('#scrRun').addEventListener('click', run);
  box.querySelector('#scrAll').addEventListener('click', () => {
    const cbs = [...box.querySelectorAll('input[type=checkbox]:not(:disabled)')];
    const allOn = cbs.every(i => i.checked);
    cbs.forEach(i => i.checked = !allOn);
  });
  box.querySelectorAll('input[data-auto]').forEach(i => i.addEventListener('change', run));
  box.querySelectorAll('input[name=scrLogic]').forEach(i => i.addEventListener('change', run));
  run();
})();
</script>
"""


if __name__ == "__main__":
    main()
