# -*- coding: utf-8 -*-
"""QuantZone 分享因子复现 — 评估与报告生成
==========================================
口径:
  - 评估窗口: 2022-06-01 ~ 2026-08-14 (qz_mf_sens_60/kyle_60 需 60 日预热; fw20 需 21 日前瞻)
  - 股票池过滤(逐日): 非北交所 | 上市≥120 交易日 | 非 ST | 非停牌(vol>0) | close≥2 | v∩fw 双非空
  - 干净 IC: 过滤集内 spearman(v, fw20), fw20 = adj_close[t+21]/adj_close[t+1] - 1 (T+1 收盘进, 持有 20 日)
  - size 中性化: 过滤集内 rank(v) 对 rank(log_circ_mv) OLS 残差, 再与 rank(fw) 相关
  - 五分位: 过滤集内等分 5 组, 组内等权 fw20
  - 对照: 与 size / amihud_20(湖内原版) 的日度 IC 序列相关 → 增量判断
产出: reports/xhs_quantzone_repro/xhs_quantzone_repro.html + data/xhs_repro/eval_summary.json
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
MIRROR = ROOT / "data/lake/clean/mirror/kline_daily.parquet"
BASIC = ROOT / "data/lake/clean/daily_basic"
NCHG = ROOT / "data/lake/clean/namechange"
FACT = ROOT / "data/xhs_repro/factors"
OUT = ROOT / "data/xhs_repro"
RPT = ROOT / "reports/xhs_quantzone_repro"
AMIHUD = ROOT / "data/lake/factor/amihud_20"

W = 20
HOLD = 20  # fw20: T+1 收盘进, 持 20 日
EVAL_START = "2022-06-01"

FACTS: dict[str, dict] = {
    "qz_cvar_20":         {"src": "QuantZone全文(feat_cvar_dw日线近似)", "orig": "6年累计IC53, G0年化22%, 2021超额37.8%"},
    "qz_kpath_illiq_20":  {"src": "QuantZone全文(feat_kpath_illiq日线近似)", "orig": "6年累计IC76, G0年化30%, 换手<3% ('年化30%换手2%'篇)"},
    "qz_idio_illiq_20":   {"src": "QuantZone全文(feat_idio_illiq日线近似)", "orig": "6年累计IC45, G0年化24%"},
    "qz_kyle_lambda_60":  {"src": "QuantZone全文(feat_hf_illiq_amt日线近似)", "orig": "6年累计IC63, G0年化30%"},
    "qz_tsim_path_20":    {"src": "QuantZone全文(feat_tsim_path_intraday日线近似)", "orig": "6年累计IC25, 中性化后超额16.9%/夏普1.79"},
    "qz_semi_illiq_dn_20": {"src": "推测口径(SemiILLIQ下行, 复刻'下行冲击'篇)", "orig": "原文标题: 年化22%"},
    "qz_semi_illiq_up_20": {"src": "对照组(SemiILLIQ上行)", "orig": "—"},
    "qz_amp_downpos_20":  {"src": "推测口径(复刻'振幅下坡路'篇)", "orig": "原文标题: 占比高→值得买"},
    "qz_repair_eff_20":   {"src": "推测口径(复刻'修复效率'篇)", "orig": "原文标题: 年化22%换手3%"},
    "qz_mf_sens_60":      {"src": "推测口径(复刻'资金流敏感'篇)", "orig": "原文标题: 6年IC59, 年化28%"},
}


def spearman(a: pd.Series, b: pd.Series) -> float:
    m = a.notna() & b.notna()
    if m.sum() < 50:
        return np.nan
    return a[m].rank().corr(b[m].rank())


def main() -> None:
    t0 = time.perf_counter()
    RPT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()

    # ── 数据加载 ──────────────────────────────────────────────
    df = con.execute(
        f"SELECT date, code, close, vol, adj_factor FROM read_parquet('{MIRROR.as_posix()}') "
        "WHERE code NOT LIKE '8%' AND code NOT LIKE '4%' AND code NOT LIKE '92%' ORDER BY date, code"
    ).df()
    df["date"] = pd.to_datetime(df["date"])
    c = df.pivot(index="date", columns="code", values="close")
    vol = df.pivot(index="date", columns="code", values="vol")
    adj_c = c * df.pivot(index="date", columns="code", values="adj_factor")
    ret = adj_c.pct_change(fill_method=None)
    fw = adj_c.shift(-(HOLD + 1)).div(adj_c.shift(-1)) - 1  # T+1 收盘进, T+21 收盘出
    print(f"[{time.perf_counter()-t0:.0f}s] mirror 宽表就绪 {c.shape}", flush=True)

    # ST 掩码
    nch = con.execute(
        f"SELECT code, start_date, end_date FROM read_parquet('{NCHG.as_posix()}/[!~]*.parquet') WHERE name LIKE '%ST%'"
    ).df()
    st_mask = pd.DataFrame(False, index=c.index, columns=c.columns)
    for r in nch.itertuples():
        s = pd.Timestamp(r.start_date) if r.start_date is not None else None
        e = pd.Timestamp(r.end_date) if r.end_date is not None else pd.Timestamp("2099-12-31")
        if s is None:
            continue
        m = (c.index >= s) & (c.index <= e)
        cols = [cc for cc in c.columns if cc == r.code]
        if m.any() and cols:
            st_mask.loc[m, cols] = True
    print(f"[{time.perf_counter()-t0:.0f}s] ST 掩码就绪", flush=True)

    # 上市天数 & 市值 (pandas3 无 applymap, 改 numpy 广播)
    first_day = c.apply(lambda s: s.first_valid_index()).reindex(c.columns)
    days = (c.index.values[:, None] - first_day.values[None, :]) / np.timedelta64(1, "D")
    listed_days = pd.DataFrame(days, index=c.index, columns=c.columns)
    mv = con.execute(f"SELECT date, code, circ_mv FROM read_parquet('{BASIC.as_posix()}/[!~]*.parquet')").df()
    mv["date"] = pd.to_datetime(mv["date"])
    log_mv = np.log(mv.pivot(index="date", columns="code", values="circ_mv")).reindex(
        index=c.index, columns=c.columns
    )
    print(f"[{time.perf_counter()-t0:.0f}s] 过滤矩阵就绪", flush=True)

    # 基础过滤掩码(逐日布尔宽表)
    base_ok = (
        (vol > 0)
        & (c >= 2)
        & (~st_mask)
        & (listed_days >= 120)
        & fw.notna()
        & ret.notna()
    )

    # amihud_20 湖值(对照)
    amihud_long = con.execute(
        f"SELECT date, code, value FROM read_parquet('{AMIHUD.as_posix()}/[!~]*.parquet')"
    ).df()
    amihud_long["date"] = pd.to_datetime(amihud_long["date"])
    amihud = amihud_long.pivot(index="date", columns="code", values="value").reindex(
        index=c.index, columns=c.columns
    )

    eval_dates = c.index[(c.index >= pd.Timestamp(EVAL_START)) & (c.index <= c.index.max() - pd.Timedelta(days=32))]
    print(f"评估日 {len(eval_dates)} 个: {eval_dates[0].date()} ~ {eval_dates[-1].date()}", flush=True)

    # ── 评估函数 ──────────────────────────────────────────────
    def rank_resid_ic(v: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        """返回 (raw_ic 序列, size 中性化 IC 序列)."""
        ics, ics_neu = {}, {}
        for d in eval_dates:
            ok = base_ok.loc[d] & v.loc[d].notna() & log_mv.loc[d].notna()
            if ok.sum() < 100:
                ics[d] = np.nan
                ics_neu[d] = np.nan
                continue
            fv = v.loc[d][ok]
            fm = log_mv.loc[d][ok]
            ff = fw.loc[d][ok]
            ics[d] = spearman(fv, ff)
            # rank 残差
            rv = fv.rank()
            rm = fm.rank()
            b = np.polyfit(rm.values, rv.values, 1)
            resid = rv - (b[0] * rm + b[1])
            ics_neu[d] = resid.corr(ff.rank())
        return pd.Series(ics), pd.Series(ics_neu)

    def quintiles(v: pd.DataFrame, sign: int) -> dict:
        """五分位组合(等权 fw20). sign=+1 高值多头; sign=-1 低值多头."""
        g_ann, top_exc, turnover = {}, {}, {}
        top_sets: dict[object, set] = {}
        pool_mean = {}
        for d in eval_dates:
            ok = base_ok.loc[d] & v.loc[d].notna()
            fv = v.loc[d][ok].dropna()
            if len(fv) < 100:
                continue
            q = pd.qcut(fv.rank(method="first"), 5, labels=False)  # 0..4, 4=高值
            fg = fw.loc[d].reindex(fv.index)
            gm = fg.groupby(q).mean()
            g_ann[d] = gm
            pool_mean[d] = fg.mean()
            if sign >= 0:
                top, bot = 4, 0
            else:
                top, bot = 0, 4
            top_exc[d] = gm[top] - fg.mean()
            top_sets[d] = set(fv.index[q == top])
        # 年化
        G = pd.DataFrame(g_ann).T  # index=date, cols=0..4
        ann = G.mean() * 244
        pool_ann = pd.Series(pool_mean).mean() * 244
        if sign >= 0:
            spread = ann[4] - ann[0]
        else:
            spread = ann[0] - ann[4]
        # 换手(日频再平衡口径, 高估实际换手, 仅横向比较)
        tovs = []
        dates_sorted = sorted(top_sets.keys())
        for a, b in zip(dates_sorted[:-1], dates_sorted[1:]):
            if top_sets[a] and top_sets[b]:
                tovs.append(1 - len(top_sets[a] & top_sets[b]) / len(top_sets[a]))
        return {
            "group_ann": {int(k): float(vv) for k, vv in ann.items()},
            "pool_ann": float(pool_ann),
            "spread_ann": float(spread),
            "top_group": (4 if sign >= 0 else 0),
            "top_excess_ann": float(np.mean(list(top_exc.values())) * 244) if top_exc else np.nan,
            "daily_turnover": float(np.mean(tovs)) if tovs else np.nan,
            "top_sets": {str(k): v for k, v in top_sets.items()},
            "G": G,
        }

    # ── 逐因子评估 ────────────────────────────────────────────
    summary: dict[str, dict] = {}
    ic_series_all: dict[str, pd.Series] = {}
    for name, info in FACTS.items():
        fp = FACT / f"{name}.parquet"
        if not fp.exists():
            print(f"!! missing {name}")
            continue
        lg = pd.read_parquet(fp)
        lg["date"] = pd.to_datetime(lg["date"])
        v = lg.pivot(index="date", columns="code", values="value").reindex(
            index=c.index, columns=c.columns
        )
        ic_raw, ic_neu = rank_resid_ic(v)
        ic_raw = ic_raw.dropna()
        ic_neu = ic_neu.dropna()
        # 方向: 用全期 IC 符号定 sign(忠实呈现即可, 组合按 IC 方向取最优侧)
        sign = 1 if ic_raw.mean() >= 0 else -1
        q = quintiles(v, sign)
        ic_series_all[name] = ic_raw
        # 年度 IC
        yr = ic_raw.groupby(ic_raw.index.year).agg(["mean", "count"])
        # t 检验
        tstat_raw = ic_raw.mean() / ic_raw.std() * np.sqrt(len(ic_raw)) if ic_raw.std() > 0 else np.nan
        tstat_neu = ic_neu.mean() / ic_neu.std() * np.sqrt(len(ic_neu)) if len(ic_neu) > 2 and ic_neu.std() > 0 else np.nan
        summary[name] = {
            **info,
            "ic_mean": float(ic_raw.mean()),
            "ic_ir": float(ic_raw.mean() / ic_raw.std()) if ic_raw.std() > 0 else np.nan,
            "ic_t": float(tstat_raw),
            "ic_pos_rate": float((ic_raw > 0).mean()),
            "ic_neu_mean": float(ic_neu.mean()),
            "ic_neu_t": float(tstat_neu),
            "ic_by_year": {int(k): float(vv) for k, vv in yr["mean"].items()},
            "sign": sign,
            **{k: q[k] for k in ("group_ann", "pool_ann", "spread_ann", "top_group", "top_excess_ann", "daily_turnover")},
            "n_dates": int(len(ic_raw)),
        }
        print(
            f"[{time.perf_counter()-t0:.0f}s] {name}: IC={ic_raw.mean():+.4f} t={tstat_raw:+.1f} "
            f"neuIC={ic_neu.mean():+.4f} spread={q['spread_ann']:+.1%} top_exc={q['top_excess_ann']:+.1%}",
            flush=True,
        )

    # ── IC 相关(增量判断) ─────────────────────────────────────
    # size IC
    size_ic = {}
    for d in eval_dates:
        ok = base_ok.loc[d] & log_mv.loc[d].notna() & fw.loc[d].notna()
        if ok.sum() < 100:
            continue
        size_ic[d] = spearman(log_mv.loc[d][ok], fw.loc[d][ok])
    size_ic = pd.Series(size_ic).dropna()
    am_ic = {}
    for d in eval_dates:
        ok = base_ok.loc[d] & amihud.loc[d].notna() & fw.loc[d].notna()
        if ok.sum() < 100:
            continue
        am_ic[d] = spearman(amihud.loc[d][ok], fw.loc[d][ok])
    am_ic = pd.Series(am_ic).dropna()
    for name in summary:
        s = ic_series_all[name]
        summary[name]["ic_corr_size"] = float(s.corr(size_ic.reindex(s.index))) if len(s) else np.nan
        summary[name]["ic_corr_amihud"] = float(s.corr(am_ic.reindex(s.index))) if len(s) else np.nan
    summary["_baseline"] = {
        "size_ic_mean": float(size_ic.mean()),
        "amihud_ic_mean": float(am_ic.mean()),
        "amihud_ic_ir": float(am_ic.mean() / am_ic.std()),
        "n_dates": int(len(eval_dates)),
        "eval_start": str(eval_dates[0].date()),
        "eval_end": str(eval_dates[-1].date()),
    }

    (OUT / "eval_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"[{time.perf_counter()-t0:.0f}s] eval_summary.json 写出", flush=True)

    # 保存 IC 序列与五分位矩阵供画图
    pd.DataFrame(ic_series_all).to_parquet(OUT / "ic_series.parquet")
    for name, s in summary.items():
        if name.startswith("_") or "G" not in str(s):
            continue
    # G 矩阵在 quintiles 返回里被丢弃, 重存:
    # (为省内存不再重算, HTML 直接用 group_ann 画柱状)

    print("done")


if __name__ == "__main__":
    main()
