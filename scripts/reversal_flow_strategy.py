# -*- coding: utf-8 -*-
"""反转 × 资金流量因子策略研究（2026-09-14）
=================================================================================
研究问题：反转类因子 + 资金流量因子构造选股策略，叠加择时。

数据路径（全部无锁，绕开 DuckDB 写锁——批次2数据链运行中）：
- kline_daily / index_kline / instruments : data/lake/clean/mirror/*.parquet
- moneyflow                              : data/lake/clean/moneyflow/part-*.parquet
- 因子值                                 : data/lake/factor/<name>/part-*.parquet

流程：
  1. 计算 5 个资金流因子（quantlab/factor/library/moneyflow.py 注册版）
     → 直接落因子湖（复刻 append_factor 合并语义，不碰主库）；--skip-compute 可跳过
  2. IC 评估（IC5/IC20/ICIR/胜率/分年，ashare_ex 池）+ 截面相关性
  3. 因子选择规则（事前声明）：
     - 反转族代表 = |ICIR20| 最高者
     - 资金流族入组合门槛 |ICIR20| >= 0.15 且与已入选因子 |corr| < 0.7
       （不达标者只通过 过滤/择时 参与策略，不做等权合成——防稀释）
  4. 合成策略 4 相位回测（复刻 run_backtest 口径：d0 收盘建仓/期末扣费/
     佣金万2.5+印花税千1+滑点10bp×2；基准中证1000）
  5. 择时叠加（严格因果：t-1 收盘信号 → t 日仓位；仓位变动按全额成本计费）

诚实性声明：
- 因子方向由全样本 IC 符号拟合（与库内 FACTOR_DIRECTION 同法），属样本内选择
- 调仓网格运气用 4 相位平均平滑（与 run_multiphase_backtest 同法）
- 择时信号全部滞后一日生效，仓位变动成本全额计入（比 decision/timing.py
  的 apply_timing 更严格——后者同日乘仓位不计交易成本）
- 池过滤用 instruments 当前快照（与 model backtest 的 get_universe 同口径，
  含轻度幸存者偏差，库内既有约定）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from quantlab import config  # noqa: E402

MIRROR = config.CLEAN_DIR / "mirror"
MF_DIR = config.CLEAN_DIR / "moneyflow"
OUT_DIR = config.REPORTS_DIR / "reversal_flow"
OUT_DIR.mkdir(parents=True, exist_ok=True)

TRADING_DAYS = 252
COMMISSION, STAMP_TAX, SLIPPAGE = (config.COMMISSION, config.STAMP_TAX,
                                    config.SLIPPAGE_BASE)
COST_PER_TURNOVER = (COMMISSION + SLIPPAGE + COMMISSION + STAMP_TAX + SLIPPAGE)

POOL = "ashare_ex"
BENCH = "000852.SH"
MIN_STK_IC = 30          # IC 计算的最小截面股票数
FLOW_ICIR_GATE = 0.15    # 资金流因子入合成门槛

REVERSAL_FAMILY = ["reversal_5", "reversal_10", "anchor_reversal_20"]
FLOW_FAMILY = ["mf_main_pct_20", "mf_small_pct_20", "mf_net_pct_20",
               "mf_smart_dumb_20", "mf_main_chg_20"]


def log(msg: str):
    print(f"[{pd.Timestamp.now():%H:%M:%S}] {msg}", flush=True)


# ══════════════════════════════════════════════════════════════════
# 1. 无锁数据层
# ══════════════════════════════════════════════════════════════════
class MirrorStore:
    """Store 替身：in-memory DuckDB + mirror/clean 视图，供 SqlFactor.compute 用"""

    def __init__(self):
        self.con = duckdb.connect()
        views = {
            "kline_daily": MIRROR / "kline_daily.parquet",
            "index_kline": MIRROR / "index_kline.parquet",
            "instruments": MIRROR / "instruments.parquet",
            "daily_snapshot": MIRROR / "daily_snapshot.parquet",
        }
        for t, f in views.items():
            self.con.execute(
                f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{f.as_posix()}')")

    def q(self, sql: str, params=None) -> pd.DataFrame:
        cur = self.con.execute(sql, params or [])
        return cur.df() if cur.description is not None else pd.DataFrame()


def save_factor_lockfree(name: str, df: pd.DataFrame) -> int:
    """复刻 Store.append_factor 的按年分区 + keep-last 合并（不碰主库写锁）"""
    d = config.FACTOR_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    dates = pd.to_datetime(df["date"])
    written = 0
    for year in sorted(dates.dt.year.unique()):
        part = df[dates.dt.year == year]
        f = d / f"part-{year}.parquet"
        old = pd.read_parquet(f) if f.exists() else pd.DataFrame()
        if not old.empty:
            merged = pd.concat([old, part], ignore_index=True)
            merged = (merged.drop_duplicates(subset=["date", "code"],
                                             keep="last")
                      .sort_values(["date", "code"]))
        else:
            merged = part
        merged.to_parquet(f, index=False)
        written += len(part)
    return written


def compute_mf_factors() -> None:
    from quantlab.factor.registry import get_factor
    store = MirrorStore()
    for n in FLOW_FAMILY:
        fac = get_factor(n)
        df = fac.compute(store, start=None, end=None, universe=None)
        if df.empty:
            raise RuntimeError(f"因子 {n} 计算为空——检查 moneyflow 湖表")
        n_rows = save_factor_lockfree(n, df)
        log(f"因子 {n}: {len(df)} 行（{df['date'].min().date()} ~ "
            f"{df['date'].max().date()}，{df['code'].nunique()} 只）落湖 {n_rows} 行")


# ══════════════════════════════════════════════════════════════════
# 2. 数据矩阵
# ══════════════════════════════════════════════════════════════════
def load_price_matrix() -> pd.DataFrame:
    con = duckdb.connect()
    px = con.execute(f"""
        SELECT date, code, close * adj_factor AS c
        FROM read_parquet('{(MIRROR / 'kline_daily.parquet').as_posix()}')
        WHERE close > 0
    """).df()
    px["date"] = pd.to_datetime(px["date"])
    return px.pivot(index="date", columns="code", values="c").sort_index()


def load_pool_codes() -> set[str]:
    """ashare_ex 当前快照口径（与 model backtest 的 get_universe 同法）"""
    con = duckdb.connect()
    df = con.execute(f"""
        SELECT code, is_st, board, list_date
        FROM read_parquet('{(MIRROR / 'instruments.parquet').as_posix()}')
    """).df()
    df = df[~df["is_st"].astype(bool)]
    df = df[df["board"] != "BJ"]
    ld = pd.to_datetime(df["list_date"], errors="coerce")
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=150)
    df = df[~(ld > cutoff)]
    codes = set(df["code"].astype(str).str.zfill(6))
    log(f"ashare_ex 池（当前快照）: {len(codes)} 只")
    return codes


def load_factor_wide(name: str, pool: set[str]) -> pd.DataFrame:
    d = config.FACTOR_DIR / name
    df = pd.read_parquet(d)
    df["date"] = pd.to_datetime(df["date"])
    df["code"] = df["code"].astype(str).str.zfill(6)
    df = df[df["code"].isin(pool)]
    return df.pivot(index="date", columns="code", values="value").sort_index()


# ══════════════════════════════════════════════════════════════════
# 3. IC 评估
# ══════════════════════════════════════════════════════════════════
def _row_corr(xv: np.ndarray, yv: np.ndarray) -> np.ndarray:
    """行内 pearson（NaN 屏蔽），输入已秩化/对齐"""
    mask = np.isfinite(xv) & np.isfinite(yv)
    xm = np.where(mask, xv, np.nan)
    ym = np.where(mask, yv, np.nan)
    with np.errstate(invalid="ignore"):
        mx = np.nanmean(xm, axis=1)
        my = np.nanmean(ym, axis=1)
        dx = xm - mx[:, None]
        dy = ym - my[:, None]
        cov = np.nansum(dx * dy, axis=1)
        sx = np.sqrt(np.nansum(dx * dx, axis=1))
        sy = np.sqrt(np.nansum(dy * dy, axis=1))
        return cov / np.where(sx * sy > 0, sx * sy, np.nan)


def row_spearman(a: pd.DataFrame, b: pd.DataFrame) -> pd.Series:
    """逐日截面 spearman：秩化后行内 pearson"""
    cols = a.columns.intersection(b.columns)
    idx = a.index.intersection(b.index)
    if len(idx) < 10 or len(cols) < MIN_STK_IC:
        return pd.Series(dtype=float)
    x = a.loc[idx, cols].rank(axis=1)
    y = b.loc[idx, cols].rank(axis=1)
    ic = _row_corr(x.values, y.values)
    n = (np.isfinite(x.values) & np.isfinite(y.values)).sum(axis=1)
    s = pd.Series(ic, index=idx)
    s[n < MIN_STK_IC] = np.nan
    return s.dropna()


def ic_table(factors: dict[str, pd.DataFrame], fw5, fw20):
    rows, ic_series = [], {}
    for name, w in factors.items():
        ic5 = row_spearman(w, fw5)
        ic20 = row_spearman(w, fw20)
        ic_series[name] = ic20
        by_year = ic20.groupby(ic20.index.year).mean()
        rows.append({
            "factor": name,
            "ic5": ic5.mean(), "icir5": ic5.mean() / ic5.std(),
            "ic20": ic20.mean(),
            "icir20": ic20.mean() / ic20.std(),
            "win20": (ic20 > 0).mean(),
            "n_days": len(ic20),
            **{f"ic20_{y}": v for y, v in by_year.items()},
        })
    return pd.DataFrame(rows).set_index("factor"), ic_series


def cross_corr(factors: dict[str, pd.DataFrame]) -> pd.DataFrame:
    names = list(factors)
    ranked = {n: w.rank(axis=1) for n, w in factors.items()}
    n = len(names)
    m = pd.DataFrame(np.eye(n), index=names, columns=names)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = ranked[names[i]], ranked[names[j]]
            cols = a.columns.intersection(b.columns)
            idx = a.index.intersection(b.index)
            ic = _row_corr(a.loc[idx, cols].values, b.loc[idx, cols].values)
            m.loc[names[i], names[j]] = m.loc[names[j], names[i]] = np.nanmean(ic)
    return m


# ══════════════════════════════════════════════════════════════════
# 4. 回测引擎（复刻 run_backtest 记账 + 4 相位平均；numpy 加速）
# ══════════════════════════════════════════════════════════════════
class Backtester:
    def __init__(self, pmat: pd.DataFrame):
        self.dates = pmat.index
        self.cols = pmat.columns
        self.vals = pmat.values
        self.date_pos = {d: i for i, d in enumerate(self.dates)}
        self.col_pos = {c: i for i, c in enumerate(self.cols)}

    def run(self, score: pd.DataFrame, n_stocks: int, rebalance: int,
            phase: int = 0, phases: int = 4):
        """单相位 → (日收益序列, 平均换手率)"""
        sbd: dict = {}
        for d, g in score.groupby("date"):
            sbd[d] = g.sort_values("score", ascending=False)
        dates = sorted(sbd.keys())
        offset = max(rebalance // phases, 1)
        rb_dates = dates[phase * offset::rebalance]
        prev_top: list[str] = []
        rets, to_sum, n_per = {}, 0.0, 0
        for i in range(len(rb_dates) - 1):
            d0, d1 = rb_dates[i], rb_dates[i + 1]
            i0, i1 = self.date_pos.get(d0), self.date_pos.get(d1)
            if i0 is None or i1 is None:
                continue
            top = sbd[d0].head(n_stocks)["code"].tolist()
            if not top:
                continue
            turnover = (1 - len(set(top) & set(prev_top)) / n_stocks
                        if prev_top else 1.0)
            to_sum += turnover
            n_per += 1
            cidx = [self.col_pos[c] for c in top if c in self.col_pos]
            if not cidx:
                prev_top = top
                continue
            sub = self.vals[i0:i1 + 1][:, cidx]
            sub = pd.DataFrame(sub).ffill().values
            # repo 口径：建仓日无有效价的股票剔除（usable），不参与等权平均
            usable = np.isfinite(sub[0]) & (sub[0] > 0)
            if not usable.any():
                prev_top = top
                continue
            nav = np.ones(int(usable.sum()))
            navs = [nav]
            prev_row = sub[0][usable]
            for k in range(1, sub.shape[0]):
                cur = sub[k][usable]
                r = np.where(np.isfinite(cur) & np.isfinite(prev_row)
                             & (prev_row > 0), cur / prev_row - 1, 0.0)
                nav = nav * (1 + r)
                navs.append(nav)
                prev_row = cur
            port_nav = np.mean(navs, axis=1)     # 等权组合净值（d0=1）
            dr = port_nav[1:] / port_nav[:-1] - 1
            dr[-1] -= turnover * COST_PER_TURNOVER
            for k, t in enumerate(self.dates[i0 + 1:i1 + 1]):
                rets[t] = float(dr[k])
            prev_top = top
        s = pd.Series(rets).sort_index()
        return s, (to_sum / n_per if n_per else 0.0)

    def run_multiphase(self, score: pd.DataFrame, n_stocks=100,
                       rebalance=10, phases=4):
        rets, tos = [], []
        for k in range(phases):
            r, to = self.run(score, n_stocks, rebalance, k, phases)
            if len(r) > 0:
                rets.append(r)
                tos.append(to)
        if not rets:
            raise RuntimeError("回测无有效期间")
        common_start = min(r.index[0] for r in rets)
        combined = pd.concat([r[r.index >= common_start] for r in rets],
                             axis=1, sort=True).mean(axis=1, skipna=True).dropna()
        return combined, float(np.mean(tos))


def perf(nav: pd.Series, ret: pd.Series) -> dict:
    n = len(nav)
    years = n / TRADING_DAYS
    total = float(nav.iloc[-1] / nav.iloc[0] - 1)
    ann = float((nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1) if years > 0 else 0.0
    vol = float(ret.std() * np.sqrt(TRADING_DAYS)) if len(ret) > 1 else 0.0
    sharpe = float((ann - 0.02) / vol) if vol > 0 else np.nan
    mdd = float((nav / nav.cummax() - 1).min())
    return {"total": round(total, 4), "ann": round(ann, 4),
            "vol": round(vol, 4), "sharpe": round(sharpe, 3),
            "mdd": round(mdd, 4),
            "calmar": round(ann / abs(mdd), 3) if mdd < 0 else None}


def excess_perf(ret: pd.Series, bench_ret: pd.Series) -> dict:
    ex = (ret - bench_ret).dropna()
    years = len(ex) / TRADING_DAYS
    nav_ex = (1 + ex).cumprod()
    ann = float(nav_ex.iloc[-1] ** (1 / years) - 1) if years > 0 else 0.0
    vol = float(ex.std() * np.sqrt(TRADING_DAYS))
    dd = float((nav_ex / nav_ex.cummax() - 1).min())
    return {"excess_ann": round(ann, 4), "excess_vol": round(vol, 4),
            "ir": round(ann / vol, 3) if vol > 0 else np.nan,
            "excess_mdd": round(dd, 4),
            "win": round(float((ex > 0).mean()), 3)}


# ══════════════════════════════════════════════════════════════════
# 5. 择时（严格因果：t-1 收盘信号 → t 仓位；仓位变动全额计费）
# ══════════════════════════════════════════════════════════════════
def timing_ma(index_close: pd.Series, fast=20, slow=60, target=0.5) -> pd.Series:
    f = index_close.rolling(fast).mean()
    s = index_close.rolling(slow).mean()
    pos = pd.Series(1.0, index=index_close.index)
    pos[f < s] = target
    return pos.shift(1)


def timing_market_flow(mkt_flow: pd.Series, window=20, target=0.5) -> pd.Series:
    ma = mkt_flow.rolling(window).mean()
    pos = pd.Series(1.0, index=mkt_flow.index)
    pos[ma < 0] = target
    return pos.shift(1)


def timing_factor_ic(ic20: pd.Series, horizon=20, window=60,
                     target=0.5) -> pd.Series:
    known = ic20.shift(horizon)          # τ 日因子 IC 在 τ+20 交易日才完全可知
    sig = known.rolling(window).mean()
    pos = pd.Series(1.0, index=ic20.index)
    pos[sig < 0] = target
    return pos.shift(1)


def apply_timing(ret: pd.Series, pos: pd.Series) -> pd.Series:
    pos = pos.reindex(ret.index).ffill().fillna(1.0)
    prev = pos.shift(1).fillna(1.0)
    trade_cost = (pos - prev).abs() * COST_PER_TURNOVER
    return pos * ret - trade_cost


# ══════════════════════════════════════════════════════════════════
# 6. 主流程
# ══════════════════════════════════════════════════════════════════
def main(skip_compute: bool = False):
    results = {"meta": {
        "pool": POOL, "benchmark": BENCH,
        "cost_per_turnover": round(COST_PER_TURNOVER, 5),
        "generated_at": str(pd.Timestamp.now()),
        "data_note": ("kline/mirror 截至最近一次镜像导出（2026-09-11）；"
                      "moneyflow 湖表直读（2026-09-10）；因子方向=全样本 IC20 "
                      "符号（样本内拟合，与库内约定一致）"),
    }}

    if not skip_compute:
        log("STEP 1 计算资金流因子（无锁）")
        compute_mf_factors()
    else:
        log("STEP 1 跳过因子计算（--skip-compute）")

    log("STEP 2 装载价格矩阵与池")
    pmat = load_price_matrix()
    pool = load_pool_codes()
    fw5 = pmat.shift(-5) / pmat - 1
    fw20 = pmat.shift(-20) / pmat - 1

    log("STEP 3 装载因子宽表")
    all_names = REVERSAL_FAMILY + FLOW_FAMILY
    wide = {n: load_factor_wide(n, pool) for n in all_names}
    wide["size"] = load_factor_wide("size", pool)
    wide["amihud_20"] = load_factor_wide("amihud_20", pool)

    log("STEP 4 IC 评估")
    tab, ic_series = ic_table(
        {n: wide[n] for n in all_names}, fw5, fw20)
    tab = tab.round(4)
    log("\n" + tab.to_string())
    results["ic"] = json.loads(tab.to_json(orient="index"))

    log("STEP 5 截面相关性矩阵")
    corr = cross_corr({n: wide[n] for n in all_names}).round(3)
    log("\n" + corr.to_string())
    results["corr"] = json.loads(corr.to_json(orient="index"))

    # ── 因子选择（事前规则）──
    log("STEP 6 因子选择")
    directions = {n: (1 if tab.loc[n, "ic20"] > 0 else -1) for n in all_names}
    rev_best = tab.loc[REVERSAL_FAMILY, "icir20"].abs().idxmax()
    flow_sorted = tab.loc[FLOW_FAMILY, "icir20"].abs().sort_values(ascending=False)
    flow_sel, flow_rejected = [], {}
    for n in flow_sorted.index:
        if len(flow_sel) >= 2:
            break
        if abs(tab.loc[n, "icir20"]) < FLOW_ICIR_GATE:
            flow_rejected[n] = f"|ICIR20|={abs(tab.loc[n, 'icir20']):.3f} < {FLOW_ICIR_GATE}"
            continue
        if all(abs(corr.loc[n, m]) < 0.7 for m in flow_sel):
            flow_sel.append(n)
        else:
            flow_rejected[n] = "corr>=0.7 与已入选冗余"
    directions["size"], directions["amihud_20"] = -1, 1   # 库内实测方向
    results["selection"] = {
        "reversal_best": rev_best, "flow_selected": flow_sel,
        "flow_rejected": flow_rejected, "directions": directions,
        "flow_icir_gate": FLOW_ICIR_GATE,
    }
    log(f"反转族代表: {rev_best}（ICIR20={tab.loc[rev_best, 'icir20']:.3f}）")
    log(f"资金流族入选: {flow_sel}；拒绝: {flow_rejected}")

    # ── 合成 ──
    log("STEP 7 合成策略回测（4 相位）")

    def oriented_rank(n: str) -> pd.DataFrame:
        """方向校正后的截面秩 ∈ [0,1]，1 = 最看多"""
        r = wide[n].rank(axis=1, pct=True)
        return r if directions[n] > 0 else 1 - r

    def build_score(names, method="mean", weights=None) -> pd.DataFrame:
        frames = [oriented_rank(n) for n in names]
        if method == "mean":
            sc = sum(frames) / len(frames)
        elif method == "icir":
            w = np.array(weights, dtype=float)
            w = w / w.sum()
            sc = sum(f * wi for f, wi in zip(frames, w))
        elif method == "interact":
            sc = frames[0]
            for f in frames[1:]:
                sc = sc * f
        s = sc.stack().rename("score").reset_index()
        s.columns = ["date", "code", "score"]
        return s.dropna(subset=["score"])

    flow_f = flow_sel[0] if flow_sel else None

    def flow_filter(score: pd.DataFrame, thr: float) -> pd.DataFrame:
        """剔除主力净流入前 (1-thr) 分位的股票（拉高出货嫌疑，负 IC 证据）"""
        if flow_f is None:
            return score
        raw_rank = wide[flow_f].rank(axis=1, pct=True)
        bad = raw_rank.stack().reset_index()
        bad.columns = ["date", "code", "r"]
        bad = bad[bad["r"] > thr][["date", "code"]]
        m = score.merge(bad.assign(bad=1), on=["date", "code"], how="left")
        return m[m["bad"] != 1][["date", "code", "score"]]

    # 资金流过滤强度敏感性（rb10）
    flow_thr_best = 0.9

    combos = {
        "R_pure": ([rev_best], "mean", None),
        "R_rev10": (["reversal_10"], "mean", None),
        "F_pure": ([flow_f], "mean", None) if flow_f else None,
        "RF_equal": ([rev_best, flow_f], "mean", None) if flow_f else None,
        "RF_icir": ([rev_best, flow_f], "icir",
                    [abs(tab.loc[rev_best, "icir20"]),
                     abs(tab.loc[flow_f, "icir20"])]) if flow_f else None,
        "RF_interact": ([rev_best, flow_f], "interact", None) if flow_f else None,
        "REF_size_amihud": (["size", "amihud_20"], "mean", None),
    }
    combos = {k: v for k, v in combos.items() if v}

    bt = Backtester(pmat)
    bench_px = duckdb.connect().execute(f"""
        SELECT date, close FROM read_parquet(
        '{(MIRROR / 'index_kline.parquet').as_posix()}')
        WHERE code = '{BENCH}' ORDER BY date
    """).df()
    bench_px["date"] = pd.to_datetime(bench_px["date"])
    idx_close = bench_px.set_index("date")["close"]
    bench_ret = idx_close.pct_change()

    bt_results, nav_curves, to_records = {}, {}, {}
    for reb in (5, 10, 20):
        for cname, (names, method, weights) in combos.items():
            sc = build_score(names, method, weights)
            r, to = bt.run_multiphase(sc, 100, reb)
            br = bench_ret.reindex(r.index).fillna(0)
            nav = (1 + r).cumprod()
            bt_results[f"{cname}_rb{reb}"] = {
                "factors": list(names), "method": method, "rebalance": reb,
                "turnover_avg": round(to, 3),
                **perf(nav, r), **excess_perf(r, br)}
            if reb == 10:
                nav_curves[cname] = nav
                to_records[cname] = to
        # 资金流过滤版（R_flowfilter）rb10 敏感性
        if reb == 10:
            sc_base = build_score([rev_best], "mean")
            for thr in (0.8, 0.9, 0.95):
                sc = flow_filter(sc_base, thr)
                r, to = bt.run_multiphase(sc, 100, reb)
                br = bench_ret.reindex(r.index).fillna(0)
                nav = (1 + r).cumprod()
                bt_results[f"R_flowfilter{int(thr*100)}_rb{reb}"] = {
                    "factors": [rev_best], "method": f"flowfilter@{thr}",
                    "rebalance": reb, "turnover_avg": round(to, 3),
                    **perf(nav, r), **excess_perf(r, br)}
                if thr == flow_thr_best:
                    nav_curves[f"R_flowfilter{int(thr*100)}"] = nav
                    to_records[f"R_flowfilter{int(thr*100)}"] = to

    bt_df = pd.DataFrame(bt_results).T
    log("\n" + bt_df.to_string())
    results["backtest"] = bt_results

    # ── 择时 ──
    log("STEP 8 择时叠加（rb10）")
    con = duckdb.connect()
    mkt = con.execute(f"""
        SELECT m.date AS date,
               SUM(m.main_net) * 10000.0 / SUM(k.amount) AS mkt_flow
        FROM read_parquet('{MF_DIR.as_posix()}/part-*.parquet', union_by_name=true) m
        JOIN read_parquet('{(MIRROR / 'kline_daily.parquet').as_posix()}') k
          ON m.date = k.date AND m.code = k.code
        GROUP BY m.date ORDER BY m.date
    """).df()
    mkt["date"] = pd.to_datetime(mkt["date"])
    mkt_flow = mkt.set_index("date")["mkt_flow"]

    # 择时对象：R_flowfilter（主题完整版）与 R_pure（归因对照）
    timing_bases = {}
    sc_base = build_score([rev_best], "mean")
    timing_bases["R_pure"] = bt.run_multiphase(sc_base, 100, 10)[0]
    timing_bases["R_flowfilter"] = bt.run_multiphase(
        flow_filter(sc_base, flow_thr_best), 100, 10)[0]

    pos_ma = timing_ma(idx_close)
    pos_flow = timing_market_flow(mkt_flow)
    pos_ic = timing_factor_ic(ic_series[rev_best])
    timings = {
        "T0_none": pd.Series(1.0, index=timing_bases["R_pure"].index),
        "T1_ma_trend": pos_ma,
        "T2_mkt_flow": pos_flow,
        "T3_ma_x_flow": pos_ma * pos_flow,
        "T4_rev_ic": pos_ic,
    }
    timing_results, timing_curves = {}, {}
    for bname, base_ret in timing_bases.items():
        for tname, pos in timings.items():
            rt = apply_timing(base_ret, pos)
            nav = (1 + rt).cumprod()
            br = bench_ret.reindex(rt.index).fillna(0)
            timing_results[f"{bname}__{tname}"] = {
                **perf(nav, rt), **excess_perf(rt, br),
                "avg_pos": round(float(pos.reindex(rt.index).ffill()
                                       .fillna(1.0).mean()), 3)}
            if bname == "R_flowfilter":
                timing_curves[tname] = nav
    tm_df = pd.DataFrame(timing_results).T
    log("\n" + tm_df.to_string())
    results["timing"] = timing_results

    # 分年拆解
    year_split = {}
    for label, r in [
        ("R_pure_rb10", timing_bases["R_pure"]),
        ("R_flowfilter_rb10", timing_bases["R_flowfilter"]),
        ("R_flowfilter_rb10_T3", apply_timing(
            timing_bases["R_flowfilter"], pos_ma * pos_flow)),
        ("R_flowfilter_rb10_T1", apply_timing(
            timing_bases["R_flowfilter"], pos_ma)),
    ]:
        by = {}
        for y, g in r.groupby(r.index.year):
            by[int(y)] = round(float((1 + g).prod() - 1), 4)
        year_split[label] = by
    results["year_split"] = year_split
    log("分年: " + json.dumps(year_split, ensure_ascii=False))

    # ── 落盘 ──
    with open(OUT_DIR / "results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    log(f"结果 JSON → {OUT_DIR / 'results.json'}")

    # ── 图表 ──
    log("STEP 9 图表")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
    plt.rcParams["axes.unicode_minus"] = False

    yrs = [c for c in tab.columns if c.startswith("ic20_")]
    fig, ax = plt.subplots(figsize=(10, 5))
    for n in all_names:
        ax.plot([int(c[5:]) for c in yrs], tab.loc[n, yrs].values,
                marker="o", label=n)
    ax.axhline(0, color="gray", lw=0.8)
    ax.set_title("IC20 分年（ashare_ex 池，2022-01~2026-09）")
    ax.set_xlabel("年份"); ax.set_ylabel("IC20")
    ax.legend(fontsize=8, ncol=2)
    fig.tight_layout(); fig.savefig(OUT_DIR / "ic_by_year.png", dpi=130)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7.5, 6.5))
    im = ax.imshow(corr.values, cmap="RdBu_r", vmin=-1, vmax=1)
    ax.set_xticks(range(len(corr)), corr.columns, rotation=45, ha="right", fontsize=8)
    ax.set_yticks(range(len(corr)), corr.index, fontsize=8)
    for i in range(len(corr)):
        for j in range(len(corr)):
            ax.text(j, i, f"{corr.values[i, j]:.2f}",
                    ha="center", va="center", fontsize=7)
    ax.set_title("因子截面相关性（平均逐日 spearman）")
    fig.colorbar(im, shrink=0.8)
    fig.tight_layout(); fig.savefig(OUT_DIR / "corr_heatmap.png", dpi=130)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 6))
    bench_nav = (1 + bench_ret.reindex(nav_curves["R_pure"].index)
                 .fillna(0)).cumprod()
    ax.plot(bench_nav.index, bench_nav.values, "k--", lw=1, label="中证1000")
    show = ["R_pure", "R_rev10", "F_pure", "RF_equal", "RF_interact",
            "R_flowfilter90", "REF_size_amihud"]
    for cname in show:
        if cname in nav_curves:
            ax.plot(nav_curves[cname].index, nav_curves[cname].values,
                    lw=1.3, label=cname)
    ax.set_yscale("log")
    ax.set_title("组合净值（top100 等权，10日调仓，4相位平均，扣费后，对数轴）")
    ax.legend(fontsize=9)
    fig.tight_layout(); fig.savefig(OUT_DIR / "nav_combos.png", dpi=130)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 6))
    for tname, nav in timing_curves.items():
        ax.plot(nav.index, nav.values, lw=1.3, label=tname)
    ax.set_yscale("log")
    ax.set_title("择时叠加对比（R_flowfilter，rb10，仓位变动计成本）")
    ax.legend(fontsize=9)
    fig.tight_layout(); fig.savefig(OUT_DIR / "nav_timing.png", dpi=130)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 4))
    for label, nav in [("无择时", timing_curves["T0_none"]),
                       ("T1 趋势", timing_curves["T1_ma_trend"]),
                       ("T3 趋势×资金流", timing_curves["T3_ma_x_flow"])]:
        dd = nav / nav.cummax() - 1
        ax.plot(dd.index, dd.values, lw=1, label=label)
    ax.set_title("回撤对比（R_flowfilter，rb10）")
    ax.legend(fontsize=9)
    fig.tight_layout(); fig.savefig(OUT_DIR / "drawdown.png", dpi=130)
    plt.close(fig)

    # 图6：市场资金流择时信号可视化
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    axes[0].plot(idx_close.index, idx_close.values / idx_close.iloc[0], lw=1,
                 label="中证1000（归一）")
    ma20 = idx_close.rolling(20).mean()
    ma60 = idx_close.rolling(60).mean()
    axes[0].plot(ma20.index, ma20.values / idx_close.iloc[0], lw=0.8, label="MA20")
    axes[0].plot(ma60.index, ma60.values / idx_close.iloc[0], lw=0.8, label="MA60")
    axes[0].legend(fontsize=9); axes[0].set_title("趋势择时信号")
    axes[1].plot(mkt_flow.index, mkt_flow.values, lw=0.6, color="gray",
                 alpha=0.6, label="全市场主力净流入占比（日）")
    axes[1].plot(mkt_flow.index, mkt_flow.rolling(20).mean(), lw=1.2,
                 color="crimson", label="20日均值（<0 触发降仓）")
    axes[1].axhline(0, color="k", lw=0.8)
    axes[1].legend(fontsize=9); axes[1].set_title("市场资金流择时信号")
    fig.tight_layout(); fig.savefig(OUT_DIR / "timing_signals.png", dpi=130)
    plt.close(fig)

    log("图表输出完成 → " + str(OUT_DIR))
    return results


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-compute", action="store_true",
                    help="跳过因子计算（因子湖已有现值时）")
    args = ap.parse_args()
    main(skip_compute=args.skip_compute)
