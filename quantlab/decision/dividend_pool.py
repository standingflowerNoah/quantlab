# -*- coding: utf-8 -*-
"""红利池内精选持仓（DIV10 观察仓，2026-09-07 2C 第二轮复验过闸门二）
================================================================
口径与 scripts/dividend_2c_official.py 回测严格一致：
  - 官方规则池（中证红利编制方案 2022 修订版近似）：
      过去三个完整日历年连续现金分红（纯现金事件）+ 三年平均股息率
      （决策日现价分母近似）+ 日均成交额前 80%；原样本缓冲区
      （过去一年股息率>0.5% 且成交额前 90% 保留）；年调。
      跳过：市值前 80% 筛选、股利支付率 (0,1)（无历史股本数据，声明）。
  - 生效：12 月第二个星期五后的首个交易日 → 新决策池启用。
  - 持仓：池内 rc_prod = 0.5×(1-size_rank) + 0.5×amihud_rank 的 Top10 等权，
    月末截面更新（流水线每日快照沿用最近月末截面持仓）。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import get_logger
from ..data.store import Store

log = get_logger(__name__)

TOP_N = 10
CACHE = Path("portfolio_state/dividend_pool_cache.json")


def _second_friday(Y: int) -> pd.Timestamp:
    d1 = pd.Timestamp(year=Y, month=12, day=1)
    first_fri = d1 + pd.Timedelta(days=(4 - d1.weekday()) % 7)
    return first_fri + pd.Timedelta(days=7)


def _cache_load() -> dict:
    if CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    return {}


def _cache_save(d: dict):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def official_pool_codes(store: Store, Y: int) -> set:
    """决策年 Y 的官方规则池（11 月末截面决策，缓存于 portfolio_state）"""
    cache = _cache_load()
    if str(Y) in cache:
        return set(cache[str(Y)])

    days = store.q(
        "SELECT DISTINCT date FROM kline_daily WHERE date >= ? AND date < ?",
        [pd.Timestamp(year=Y, month=11, day=1),
         pd.Timestamp(year=Y, month=12, day=1)])
    if days.empty:
        raise RuntimeError(f"{Y}-11 无交易日数据，无法决策")
    t = pd.to_datetime(days["date"]).max()
    snap = store.q(
        "SELECT code, close, amount FROM kline_daily WHERE date = ?", [t])

    # 过去一年日均成交额（截至 t 的 250 个交易日）
    kd = store.q(
        "SELECT code, date, amount FROM kline_daily WHERE date <= ? AND date > ?",
        [t, t - pd.Timedelta(days=400)])
    amt = kd.sort_values("date").groupby("code").tail(250).groupby("code")["amount"].mean()
    snap = snap.set_index("code")
    snap["amt_avg"] = amt
    snap["amt_r"] = snap["amt_avg"].rank(ascending=False, pct=True)

    # 三个完整日历年分红（纯现金事件，除息日年度归集）
    div = store.q(
        "SELECT code, strftime(date, '%Y') AS y, SUM(fenhong) AS d "
        "FROM dividend_events WHERE songzhuangu=0 AND peigu=0 AND fenhong>0 "
        "AND date >= ? AND date < ? GROUP BY code, y",
        [pd.Timestamp(year=Y - 3, month=1, day=1),
         pd.Timestamp(year=Y, month=1, day=1)])
    div["y"] = div["y"].astype(int)
    need = {Y - 1, Y - 2, Y - 3}
    dv = div[div["y"].isin(need)]
    div_years = dv.groupby("code")["y"].apply(set)
    div_sum = dv.groupby("code")["d"].mean()  # 三年平均每股分红

    # 过去一年股息率（缓冲区）
    d1 = store.q(
        "SELECT code, SUM(fenhong) AS d FROM dividend_events "
        "WHERE songzhuangu=0 AND peigu=0 AND fenhong>0 AND date > ? AND date <= ? "
        "GROUP BY code", [t - pd.Timedelta(days=365), t])
    snap["div_1y"] = d1.set_index("code")["d"]
    snap["dy1"] = snap["div_1y"].fillna(0.0) / snap["close"]

    snap["cont3"] = [need.issubset(div_years.get(c, set())) for c in snap.index]
    snap["div3"] = div_sum
    elig = snap[snap["cont3"] & (snap["amt_r"] <= 0.80)
                & (snap["close"] >= 2.0) & snap["div3"].notna()].copy()
    elig["score"] = elig["div3"] / elig["close"]
    elig = elig.sort_values("score", ascending=False)

    prev = _cache_load().get(str(Y - 1))
    prev = set(prev) if prev else None
    if prev:
        keep = [c for c in prev
                if c in snap.index and snap.loc[c, "dy1"] > 0.005
                and snap.loc[c, "amt_r"] <= 0.90]
        n_fill = 100 - len(keep)
        new_add = [c for c in elig.index if c not in keep][:max(n_fill, 0)]
        pool = set(keep + new_add)
    else:
        pool = set(elig.head(100).index)
    cache = _cache_load()
    cache[str(Y)] = sorted(pool)
    _cache_save(cache)
    log.info(f"红利官方池[{Y}] 决策日 {t.date()}: {len(pool)} 只")
    return pool


def build_div10_score(store: Store, universe: set | None = None) -> pd.DataFrame:
    """当前生效红利池内 rc_prod 评分（date/code/score，score∈[0,1]，1 最看多）

    仅月末截面输出（与回测口径一致）；池外股票不出现。
    """
    Y = None  # 按截面日期逐个判定
    ranked = []
    for name, d_ in [("size", -1), ("amihud_20", 1)]:
        f = store.read_factor(name)
        f["date"] = pd.to_datetime(f["date"])
        f["value"] = pd.to_numeric(f["value"], errors="coerce")
        f = f[np.isfinite(f["value"])]
        f["r"] = f.groupby("date")["value"].rank(pct=True) * d_
        ranked.append(f[["date", "code", "r"]])
    m = ranked[0].merge(ranked[1], on=["date", "code"], suffixes=("_s", "_a"))
    m = m.dropna()
    m["score"] = 0.5 * (1 - m["r_s"]) + 0.5 * m["r_a"]
    # 月末截面：全市场因子共同的每月最后交易日（rank 即月末当日截面，与回测一致）
    month_last = m.groupby(m["date"].dt.to_period("M"))["date"].max().dropna()
    m = m[m["date"].isin(set(month_last))]
    m = m.drop(columns=["r_s", "r_a", "value"], errors="ignore")

    pool_cache: dict[int, set] = {}
    keep_rows = []
    for d, g in m.groupby("date"):
        y = d.year if d.month == 12 else d.year - 1
        # 生效日判定按年度池（简化：12 月第二个星期五后启用当年池）
        if y not in pool_cache:
            try:
                pool_cache[y] = official_pool_codes(store, y)
            except Exception as e:
                log.warning(f"红利池 {y} 构建失败: {e}")
                pool_cache[y] = set()
        gg = g[g["code"].isin(pool_cache[y])]
        if len(gg):
            keep_rows.append(gg[["date", "code", "score"]])
    if not keep_rows:
        raise RuntimeError("红利池内无评分数据")
    out = pd.concat(keep_rows, ignore_index=True)
    if universe is not None:
        out = out[out["code"].isin(universe)]
    return out


def div10_holdings(store: Store, sig_date) -> pd.DataFrame:
    """DIV10 目标持仓（Top10 等权，月末截面，与回测口径一致）"""
    sig_date = pd.Timestamp(sig_date)
    score = build_div10_score(store)
    # 生效截面：sig_date 之前（含）最近的月末截面
    d = score[score["date"] <= sig_date]["date"].max()
    s = score[score["date"] == d].sort_values("score", ascending=False).head(TOP_N)
    try:
        names = store.q("SELECT code, name FROM instruments")
        nmap = dict(zip(names["code"], names["name"]))
    except Exception:
        nmap = {}
    h = pd.DataFrame({
        "code": s["code"].tolist(),
        "name": [nmap.get(c, c) for c in s["code"]],
        "weight": [1.0 / TOP_N] * len(s),
    })
    log.info(f"DIV10 持仓截面 {d.date()}: {len(h)} 只")
    return h
