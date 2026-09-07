"""周度调仓星期效应研究（2026-09-07，用户立项）
=================================================
问题：把主力模型从 20 日调仓改为周度调仓时，周一~周五哪个调仓日最佳？

方法：
- 自定义周度回测 run_weekly_backtest：每周选一个目标星期几的交易日调仓
  （当日休市则顺延到该周首个交易日），其余口径与 run_optimized_backtest
  完全一致（top100 / inverse_vol / 全成本 / 中证1000 基准）。
- 模型：PROD（现役基线）、EQ3_HFA（主切换候选）、EQ3_HFA_ICW（稳健变体）。
- 窗口：全期 2022-01-04~2026-09-04 与短窗 2025-02-01~2026-09-04。
- 稳健性：ICW vs 等权 × 星期；分年收益；假期顺延分布。

产物: reports/prodmodel/weekday.json
用法: python scripts/prodmodel_weekday.py
"""
from __future__ import annotations

import json
import sys
import warnings
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from quantlab.config import get_logger
from quantlab.data.store import Store
from quantlab.model import build_composite
from quantlab.model.backtest import (COST_PER_TURNOVER, _benchmark_returns,
                                     performance)
from quantlab.optimize.weights import optimize_weights

log = get_logger("weekday")
OUT = Path("reports/prodmodel")
OUT.mkdir(parents=True, exist_ok=True)

FULL = ("2022-07-01", "2026-09-04")   # 与主报告口径一致（前 6 个月留因子回看）
FOCUS = ("2025-02-01", "2026-09-04")
WEEKDAY_CN = {0: "周一", 1: "周二", 2: "周三", 3: "周四", 4: "周五"}

MODELS = {
    "PROD": ["size", "amihud_20"],
    "EQ3_HFA": ["size", "amihud_20", "sue_i", "hf_amihud_20"],
    "EQ3_HFA_ICW": ["size", "amihud_20", "sue_i", "hf_amihud_20"],
}


def _weekly_rb_dates(dates: list, weekday: int) -> list:
    """每个 ISO 周选一个目标星期几的交易日；缺失（假期）顺延为该周首个交易日。"""
    s = pd.Series(pd.to_datetime(dates))
    iso = s.dt.isocalendar()
    grp = pd.DataFrame({"d": s.values,
                        "year": iso["year"].values,
                        "week": iso["week"].values,
                        "wd": s.dt.weekday.values})
    out = []
    for _, g in grp.groupby(["year", "week"], sort=True):
        hit = g[g["wd"] == weekday]
        out.append(pd.Timestamp(hit["d"].iloc[0]) if len(hit)
                   else pd.Timestamp(g["d"].iloc[0]))
    return out


def run_weekly_backtest(score: pd.DataFrame, weekday: int,
                        n_stocks: int = 100, method: str = "inverse_vol",
                        lookback: int = 60, start=None, end=None,
                        benchmark: str = "000852.SH") -> dict:
    """周度调仓回测（与 run_optimized_backtest 同口径，rb_dates 按星期注入）"""
    store = Store()
    score["date"] = pd.to_datetime(score["date"])
    if start is not None:
        score = score[score["date"] >= pd.to_datetime(start)]
    if end is not None:
        score = score[score["date"] <= pd.to_datetime(end)]

    px = store.q("SELECT date, code, close*adj_factor AS c FROM kline_daily")
    px["date"] = pd.to_datetime(px["date"])
    pmat = px.pivot(index="date", columns="code", values="c").sort_index()
    rmat = pmat.pct_change()
    bench_ret = _benchmark_returns(pmat, store, benchmark)

    dates = sorted(score["date"].unique())
    rb_dates = _weekly_rb_dates(dates, weekday)
    if len(rb_dates) < 4:
        raise RuntimeError("周度调仓期数不足")

    prev_w, prev_codes = None, []
    turnover_sum, n_periods = 0.0, 0
    rows, rb_marks = [], []
    for i in range(len(rb_dates) - 1):
        d0, d1 = rb_dates[i], rb_dates[i + 1]
        if d0 not in pmat.index or d1 not in pmat.index:
            continue
        s = score[score["date"] == d0].sort_values("score", ascending=False)
        top = s.head(n_stocks)["code"].tolist()
        hist = rmat.loc[:d0, top].iloc[-(lookback + 1):-1]
        if len(hist) < 20:
            continue
        nan_ratio = hist.isna().mean()
        valid = [c for c in top if nan_ratio.get(c, 1.0) < 0.3]
        if len(valid) < 5:
            valid = top
        hist = hist[valid].fillna(0.0)

        w = np.asarray(optimize_weights(method, hist.values), dtype=float)

        seg = pmat.loc[d0:d1, valid].ffill()
        p0row = seg.iloc[0]
        usable_idx = [j for j, c in enumerate(valid)
                      if pd.notna(p0row.get(c)) and p0row[c] > 0]
        if not usable_idx:
            continue
        usable = [valid[j] for j in usable_idx]
        wu = w[usable_idx]
        wu = wu / wu.sum()

        vals = (1 + seg[usable].pct_change().fillna(0)).cumprod()
        port = (vals * wu).sum(axis=1)
        day_ret = port.pct_change().iloc[1:]

        if prev_w is not None:
            w_old = np.zeros(len(usable))
            for j, c in enumerate(usable):
                if c in prev_codes:
                    w_old[j] = prev_w[prev_codes.index(c)]
            turnover = float(np.abs(wu - w_old).sum() / 2)
        else:
            turnover = 1.0
        turnover_sum += turnover
        n_periods += 1
        rb_marks.append({"date": str(pd.Timestamp(d0).date()),
                         "wd": WEEKDAY_CN[int(pd.Timestamp(d0).weekday())],
                         "turnover": turnover})

        day_ret.iloc[-1] = day_ret.iloc[-1] - turnover * COST_PER_TURNOVER
        for t, r in day_ret.items():
            rows.append({"date": t, "ret": float(r),
                         "bench": float(bench_ret.get(t, np.nan))})
        prev_w, prev_codes = wu, usable

    if not rows:
        raise RuntimeError("回测无有效期间")
    curve = pd.DataFrame(rows).dropna(subset=["ret"])
    curve["nav"] = (1 + curve["ret"]).cumprod()
    curve["nav_bm"] = (1 + curve["bench"].fillna(0)).cumprod()
    from quantlab.model.backtest import excess_metrics
    return {"curve": curve,
            "metrics": performance(curve["nav"], period_days=1),
            "metrics_bm": performance(curve["nav_bm"], period_days=1),
            "excess": excess_metrics(curve, period_days=1),
            "turnover_avg": round(turnover_sum / max(n_periods, 1), 4),
            "n_periods": n_periods, "rb_marks": rb_marks}


def _m(r: dict) -> dict:
    m = r["metrics"]
    return {"annual": round(m["annual_return"], 4),
            "sharpe": round(m["sharpe"], 3),
            "mdd": round(m["max_drawdown"], 4),
            "turnover": r["turnover_avg"],
            "periods": r.get("n_periods", -1)}


def main():
    log.info("构建模型评分 …")
    scores = {}
    for name, factors in MODELS.items():
        method = "ic_weighted" if name.endswith("_ICW") else "equal"
        scores[name] = build_composite(factors, universe="ashare_ex",
                                       method=method)
        log.info(f"  {name}: {len(scores[name])} 行")

    # ── 周度回测：每 (模型, 窗口, 星期) 只跑一次，复用曲线/分年 ──
    result = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
              "windows": {"full": FULL, "focus": FOCUS},
              "models": {}, "curves": {}, "yearly": {}}


    for name, sc in scores.items():
        result["models"][name] = {}
        for wname, (st, en) in (("full", FULL), ("focus", FOCUS)):
            wres = {}
            for wd in range(5):
                r = run_weekly_backtest(sc.copy(), wd, start=st, end=en)
                wres[WEEKDAY_CN[wd]] = _m(r)
                c = r["curve"]
                if wname == "focus":
                    result["curves"][f"{name}|{WEEKDAY_CN[wd]}"] = {
                        "dates": [str(pd.Timestamp(d).date()) for d in c["date"]],
                        "nav": [round(float(v), 4) for v in c["nav"]],
                        "nav_bm": [round(float(v), 4) for v in c["nav_bm"]]}
                else:
                    cc = c.copy()
                    cc["year"] = pd.to_datetime(cc["date"]).dt.year
                    result["yearly"][f"{name}|{WEEKDAY_CN[wd]}"] = {
                        int(y): round(float((1 + g["ret"]).prod() - 1), 4)
                        for y, g in cc.groupby("year")}
                log.info(f"{name}/{wname}/{WEEKDAY_CN[wd]}: {_m(r)}")
            result["models"][name][wname] = wres

    # ── 20 日调仓基线（同窗对照）──
    from quantlab.optimize.backtest import run_optimized_backtest
    for name, sc in scores.items():
        for wname, (st, en) in (("full", FULL), ("focus", FOCUS)):
            b = run_optimized_backtest(sc.copy(), n_stocks=100, rebalance=20,
                                       method="inverse_vol", start=st, end=en)
            result["models"][name][wname]["基线20日"] = _m(b)
            if wname == "focus":
                c = b["curve"]
                result["curves"][f"{name}|基线20日"] = {
                    "dates": [str(pd.Timestamp(d).date()) for d in c["date"]],
                    "nav": [round(float(v), 4) for v in c["nav"]],
                    "nav_bm": [round(float(v), 4) for v in c["nav_bm"]]}
            log.info(f"{name}/{wname}/基线20日: {_m(b)}")

    # ── 调仓日实际分布（假期顺延影响，以周一目标为例）──
    r0 = run_weekly_backtest(scores["PROD"].copy(), 0, start=FULL[0],
                             end=FULL[1])
    result["rb_wd_dist"] = pd.Series(
        [m["wd"] for m in r0["rb_marks"]]).value_counts().to_dict()
    result["n_rb_marks"] = len(r0["rb_marks"])

    def _clean(o):
        if isinstance(o, dict):
            return {str(k): _clean(v) for k, v in o.items()}
        if isinstance(o, list):
            return [_clean(v) for v in o]
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return float(o)
        return o

    (OUT / "weekday.json").write_text(
        json.dumps(_clean(result), ensure_ascii=False, indent=1))
    log.info(f"完成 → {OUT/'weekday.json'}")


if __name__ == "__main__":
    main()
