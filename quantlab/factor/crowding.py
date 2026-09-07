"""因子拥挤度监控（L2 因子层诊断工具）
=====================================
对指定因子按方向调整后截面 rank 构造多空两端（top/bottom 分位），
每 freq 个交易日采样，跟踪三个拥挤度指标时序（海通"因子拥挤"系列口径）：

1. 估值价差 val_spread = log(median BP_long / median BP_short)
   拥挤 → 多头被买贵 → BP_long 相对下降 → 价差收窄 → 贡献 = −z(价差)
2. 配对相关性 pair_corr = 多头组合（≤100 只）过去 corr_win 日收益两两相关均值
   拥挤 → 持仓趋同同涨同跌 → 相关性抬升 → 贡献 = +z(相关性)
3. 换手分位 turnover_pct = 多头组合换手率（turnover 因子中位数）滚动分位
   拥挤 → 异常放量 → 分位抬升 → 贡献 = 2·pct − 1

综合拥挤度 crowding = 三贡献均值（至少 2 个非空才计算）。
z/分位均为因果滚动（窗口 norm_win 个采样点，min_periods=norm_min）。

注意：v1 为监控/诊断口径，供条件融合门控与衰减预警使用；
不构成交易规则，不做参数寻优。BP 为 finance_snapshot as-of 回填，
与 bp 因子同口径（audit WARN 同源）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import get_logger
from ..data.store import Store

log = get_logger(__name__)


def _rolling_z(s: pd.Series, window: int, min_periods: int) -> pd.Series:
    """因果滚动 z 分数（仅用截至当期的过去 window 个采样点）"""
    m = s.rolling(window, min_periods=min_periods).mean()
    sd = s.rolling(window, min_periods=min_periods).std()
    return (s - m) / sd.replace(0.0, np.nan)


def _rolling_pct(s: pd.Series, window: int, min_periods: int) -> pd.Series:
    """因果滚动分位：当前值在过去 window 个采样点中的分位（含当前）"""
    return s.rolling(window, min_periods=min_periods).apply(
        lambda a: float(np.mean(a <= a[-1])), raw=True)


def factor_crowding(name: str, direction: int = 1,
                    universe: str | list | None = "ashare_ex",
                    quantile: float = 0.2, freq: int = 5,
                    corr_win: int = 60, norm_win: int = 100,
                    norm_min: int = 40, start=None, end=None,
                    store: Store | None = None,
                    retmat: pd.DataFrame | None = None) -> pd.DataFrame:
    """单因子拥挤度时序

    参数:
        direction: +1 做多高值 / −1 做多低值（与 FACTOR_DIRECTION 一致）
        quantile: 多空两端分位（0.2 = 各 20%）
        freq: 采样间隔（交易日）
        corr_win: 配对相关性的收益回看窗口（日）
        norm_win / norm_min: z 分数与分位的滚动窗口（采样点数）与最小样本
        retmat: 预构建的日收益宽表（date×code，可选，批量监控时复用）
    返回: date / val_spread / val_z / pair_corr / corr_z / turnover_mean /
          turnover_pct / crowding（按采样日）
    """
    store = store or Store()
    uni = None
    if universe is not None:
        if isinstance(universe, str):
            from ..data.universe import get_universe
            uni = set(get_universe(universe))
        else:
            uni = set(universe)

    if retmat is None:
        px = store.q("SELECT date, code, close*adj_factor AS c FROM kline_daily")
        px["date"] = pd.to_datetime(px["date"])
        pmat = px.pivot(index="date", columns="code", values="c").sort_index()
        rmat = pmat.pct_change()
    else:
        rmat = retmat

    sample_dates = rmat.index[::freq]
    if start is not None:
        sample_dates = sample_dates[sample_dates >= pd.to_datetime(start)]
    if end is not None:
        sample_dates = sample_dates[sample_dates <= pd.to_datetime(end)]

    def _by_date(name_: str) -> dict:
        fv = store.read_factor(name_)
        fv["date"] = pd.to_datetime(fv["date"])
        fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
        fv = fv[np.isfinite(fv["value"])]
        if uni is not None:
            fv = fv[fv["code"].isin(uni)]
        fv = fv[fv["date"].isin(sample_dates)]
        return {d: g.set_index("code")["value"] for d, g in fv.groupby("date")}

    fv_d = _by_date(name)
    bp_d = _by_date("bp")
    to_d = _by_date("turnover")
    dates = sorted(fv_d.keys())
    log.info(f"[crowding] {name}: {len(dates)} 个采样日")

    rows = []
    for d in dates:
        s = fv_d[d]
        n = len(s)
        if n < 200:
            continue
        r = s.rank(pct=True) * direction          # 方向调整后 rank，1=最看多
        k = max(int(n * quantile), 20)
        long_codes = r.nlargest(k).index.tolist()[:100]
        short_codes = r.nsmallest(k).index.tolist()[:100]

        # 1. 估值价差（bp 需 >0）
        bpl = bp_d.get(d, pd.Series(dtype=float)).reindex(long_codes).dropna()
        bpl = bpl[bpl > 0]
        bps = bp_d.get(d, pd.Series(dtype=float)).reindex(short_codes).dropna()
        bps = bps[bps > 0]
        val_spread = (np.log(bpl.median() / bps.median())
                      if len(bpl) >= 10 and len(bps) >= 10 and bps.median() > 0
                      else np.nan)

        # 2. 配对相关性（多头组过去 corr_win 日收益两两相关均值）
        loc = rmat.index.get_loc(d)
        pair_corr = np.nan
        if loc >= corr_win - 1:
            seg = rmat.iloc[loc - corr_win + 1: loc + 1].reindex(
                columns=long_codes)
            c = seg.corr().values
            iu = np.triu_indices(len(long_codes), k=1)
            vals = c[iu]
            vals = vals[np.isfinite(vals)]
            if len(vals) >= 100:
                pair_corr = float(vals.mean())

        # 3. 多头组换手率（中位数）
        tol = to_d.get(d, pd.Series(dtype=float)).reindex(long_codes).dropna()
        to_mean = float(tol.median()) if len(tol) >= 10 else np.nan

        rows.append({"date": d, "val_spread": val_spread,
                     "pair_corr": pair_corr, "turnover_mean": to_mean,
                     "n_long": len(long_codes)})

    df = pd.DataFrame(rows).set_index("date")
    if df.empty:
        return df

    df["val_z"] = _rolling_z(df["val_spread"], norm_win, norm_min)
    df["corr_z"] = _rolling_z(df["pair_corr"], norm_win, norm_min)
    df["turnover_pct"] = _rolling_pct(df["turnover_mean"], norm_win, norm_min)

    contrib = pd.DataFrame({
        "c_val": -df["val_z"],          # 价差收窄 → 拥挤
        "c_corr": df["corr_z"],         # 相关性抬升 → 拥挤
        "c_to": 2 * df["turnover_pct"] - 1,  # 放量 → 拥挤
    })
    df["crowding"] = contrib.mean(axis=1).where(
        contrib.notna().sum(axis=1) >= 2)
    return df.reset_index()


# ── 监控池编排（流水线步骤） ─────────────────────────────────────

DEFAULT_POOL: dict[str, list[str]] = {
    "core": ["size", "amihud_20"],
    "pricevol": ["momentum_20", "reversal_5", "volatility_20",
                 "price_position_250"],
    # 2026-09-07 sue 切换期：sue_i 并行监控（sue 保留对照直至切换完成）
    "new": ["sue", "sue_i", "overnight_mom_20", "chip_vwap_bias_250"],
}


def monitor_pool(pool: dict[str, list[str]] | None = None,
                 alert_pct: float = 0.8, freq: int = 5,
                 persist: bool = True,
                 store: Store | None = None) -> tuple[pd.DataFrame, list, dict]:
    """监控池全量跑批（每日流水线步骤）：计算 + 持久化 + 预警

    - 每个因子全量重算时序（rolling z 每日重算口径一致，~1 分钟内）
    - 持久化: data/lake/crowding/history.parquet（长表全量覆盖写）
              + data/lake/crowding/latest.json（最新值/历史分位/预警/门控）
    - 预警: 因子拥挤度历史分位 >= alert_pct → log.warning + 计入 alerts
    - 门控: 核心组拥挤度均值 → 因果滚动分位 → w = 1 − 0.4·pct（示意）

    返回 (df_long, alerts, gate)。
    """
    from pathlib import Path
    import json as _json

    from ..config import LAKE_DIR
    from ..model.composite import FACTOR_DIRECTION   # 惰性导入防环

    pool = pool or DEFAULT_POOL
    store = store or Store()
    px = store.q("SELECT date, code, close*adj_factor AS c FROM kline_daily")
    px["date"] = pd.to_datetime(px["date"])
    pmat = px.pivot(index="date", columns="code", values="c").sort_index()
    rmat = pmat.pct_change()

    frames = []
    for group, names in pool.items():
        for n in names:
            df = factor_crowding(n, direction=FACTOR_DIRECTION.get(n, 1),
                                 universe="ashare_ex", freq=freq,
                                 store=store, retmat=rmat)
            if df.empty:
                log.warning(f"[monitor] {n} 无数据，跳过")
                continue
            df["factor"] = n
            df["group"] = group
            frames.append(df)
    if not frames:
        raise RuntimeError("监控池全部无数据")
    df_long = pd.concat(frames, ignore_index=True)

    latest, alerts = {}, []
    for n, g in df_long.groupby("factor"):
        ch = g.dropna(subset=["crowding"])
        if ch.empty:
            continue
        row = ch.iloc[-1]
        hist_pct = float((ch["crowding"] <= row["crowding"]).mean())
        latest[n] = {
            "date": str(pd.Timestamp(row["date"]).date()),
            "group": str(row["group"]),
            "crowding": round(float(row["crowding"]), 3),
            "hist_pct": round(hist_pct, 3),
            "val_z": None if pd.isna(row["val_z"]) else round(float(row["val_z"]), 3),
            "corr_z": None if pd.isna(row["corr_z"]) else round(float(row["corr_z"]), 3),
            "turnover_pct": None if pd.isna(row["turnover_pct"])
                            else round(float(row["turnover_pct"]), 3),
        }
        if hist_pct >= alert_pct:
            alerts.append(n)
            log.warning(f"[拥挤预警] {n}: crowding={row['crowding']:+.2f}, "
                        f"历史分位 {hist_pct:.0%} >= {alert_pct:.0%}")

    # 门控（核心组拥挤度 → 因果分位 → w）
    gate: dict = {"w_current": None,
                  "note": "w=1-0.4*pct(核心拥挤因果滚动分位); 示意口径"}
    core_names = pool.get("core", [])
    core_series = []
    for n in core_names:
        g = df_long[df_long["factor"] == n].set_index("date")["crowding"].dropna()
        if len(g):
            core_series.append(g)
    if core_series and len(core_names):
        core = pd.concat(core_series, axis=1).mean(axis=1).dropna()
        if len(core) >= 40:
            pct = core.rolling(100, min_periods=40).apply(
                lambda a: float(np.mean(a <= a[-1])), raw=True).dropna()
            w = 1 - 0.4 * pct
            gate = {
                "dates": [str(pd.Timestamp(d).date()) for d in pct.index],
                "core_crowding": [round(float(v), 3) for v in core.reindex(pct.index)],
                "pct": [round(float(v), 3) for v in pct],
                "w": [round(float(v), 3) for v in w],
                "w_current": round(float(w.iloc[-1]), 3),
                "pct_current": round(float(pct.iloc[-1]), 3),
                "note": "w=1-0.4*pct(核心拥挤因果滚动分位); 示意口径，未回测",
            }

    if persist:
        out_dir = LAKE_DIR / "crowding"
        out_dir.mkdir(parents=True, exist_ok=True)
        df_long.to_parquet(out_dir / "history.parquet", index=False)
        with open(out_dir / "latest.json", "w", encoding="utf-8") as f:
            _json.dump({"latest": latest, "alerts": alerts,
                        "alert_pct": alert_pct, "gate": gate,
                        "pool": pool,
                        "generated_at": pd.Timestamp.now().isoformat()},
                       f, ensure_ascii=False, indent=1)
        log.info(f"[monitor] 持久化 → {out_dir}/history.parquet + latest.json")

    return df_long, alerts, gate
