# -*- coding: utf-8 -*-
"""QuantZone(quantzone攻城小队/Quant攻城小队) 分享因子复现 — 日频近似计算
=====================================================================
来源: 小红书 https://xhslink.cn/o/bsRBfObNP5 (quantzone攻城小队)
全文经其微信公众号镜像文章还原, 5 篇拿到完整定义, 4 篇用文献口径/推测口径复刻(明确标注).

数据: 全只读湖数据 — data/lake/clean/mirror/kline_daily.parquet (2022-01-04~2026-09-15)
      + data/lake/clean/daily_basic(circ_mv) + data/lake/clean/moneyflow(main_net)
      + data/lake/clean/namechange(ST 掩码)
不碰主库 quant.duckdb(流水线持写锁期间安全).

产出: data/xhs_repro/factors/{name}.parquet (date, code, value 长表, 全池含未过滤)
      data/xhs_repro/meta.json

因子清单(窗口/口径):
  qz_cvar_20          20 日负日收益均值 (feat_cvar_dw 日线近似; 原文=分钟左尾损失 20 日均, G0 高值多头, 6年累计IC53, G0年化22%)
  qz_kpath_illiq_20   日K最短路径/close/amount 20 日均 (feat_kpath_illiq 日线近似; 原文 6年IC76, G0年化30%换手<3% = "年化30%换手2%"篇)
  qz_idio_illiq_20    |ret−ret_mkt|/amount 20 日均 (feat_idio_illiq 日线近似; 原文 6年IC45, G0年化24%)
  qz_kyle_lambda_60   60 日 ret~signed_amount OLS 斜率 (feat_hf_illiq_amt 日线近似, 窗口 21→60 放大; 原文 6年IC63, G0年化30%)
  qz_tsim_path_20     20 日 corr(ret_i, ret_m) − |20 日累计路径差| (feat_tsim_path_intraday 日线近似; 原文中性化后超额16.9%夏普1.79)
  qz_semi_illiq_dn_20 Σ|ret|·1[ret<0]/Σamount 20 日均 (SemiILLIQ 下行半程, 复刻"下行冲击"篇直觉, 推测口径)
  qz_amp_downpos_20   mean((H−C)/(H−L)) 20 日均 (复刻"振幅下坡路"篇直觉, 推测口径)
  qz_repair_eff_20    (C−min20)/(max20−min20) (复刻"修复效率"篇直觉, 推测口径)
  qz_mf_sens_60       60 日 ret~main_net/amount OLS 斜率 (复刻"资金流敏感"篇直觉, 推测口径)

  ⚠ 日线近似 vs 原文分钟口径的差异在 eval 报告中声明, 不宣称等价复现.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
MIRROR = ROOT / "data/lake/clean/mirror/kline_daily.parquet"
BASIC = ROOT / "data/lake/clean/daily_basic"
MFLOW = ROOT / "data/lake/clean/moneyflow"
NCHG = ROOT / "data/lake/clean/namechange"
OUT = ROOT / "data/xhs_repro"
FACT = OUT / "factors"

W = 20   # 主窗口
WL = 60  # 长窗口(kyle / mf_sens)


def t(msg: str, t0: float) -> float:
    now = time.perf_counter()
    print(f"[{now - t0:7.1f}s] {msg}", flush=True)
    return now


def rolling_slope(y: pd.DataFrame, x: pd.DataFrame, w: int) -> pd.DataFrame:
    """向量化滚动 OLS 斜率 slope = (w*Sxy - Sx*Sy) / (w*Sxx - Sx^2), NaN 安全."""
    valid = x.notna() & y.notna()
    xv = x.where(valid)
    yv = y.where(valid)
    sx = xv.rolling(w, min_periods=int(w * 0.7)).sum()
    sy = yv.rolling(w, min_periods=int(w * 0.7)).sum()
    sxx = (xv * xv).rolling(w, min_periods=int(w * 0.7)).sum()
    sxy = (xv * yv).rolling(w, min_periods=int(w * 0.7)).sum()
    cnt = valid.rolling(w, min_periods=int(w * 0.7)).sum()
    denom = cnt * sxx - sx * sx
    slope = (cnt * sxy - sx * sy) / denom.where(denom.abs() > 1e-12)
    return slope.where(cnt >= int(w * 0.7))


def main() -> None:
    t0 = time.perf_counter()
    FACT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()

    # ── 1. 加载 mirror 日线并转宽表 ─────────────────────────────
    df = con.execute(
        f"SELECT date, code, open, high, low, close, vol, amount, adj_factor FROM read_parquet('{MIRROR.as_posix()}') "
        "WHERE code NOT LIKE '8%' AND code NOT LIKE '4%' AND code NOT LIKE '92%' ORDER BY date, code"
    ).df()
    df["date"] = pd.to_datetime(df["date"])
    t(f"mirror 加载 {len(df):,} 行, {df['code'].nunique()} 股", t0)

    def wide(col: str) -> pd.DataFrame:
        return df.pivot(index="date", columns="code", values=col)

    o, h, l, c = wide("open"), wide("high"), wide("low"), wide("close")
    vol, amt = wide("vol"), wide("amount")
    adj = wide("adj_factor")
    # 复权收益: close*adj_factor (mirror 带复权因子; 收益/尾部/相关类因子必须用复权口径,
    # 否则除权跳变会伪装成"暴跌"污染 ret/fw; 日内路径类因子用原始 OHLC, 同日比例不受影响)
    adj_c = c * adj
    ret = adj_c.pct_change(fill_method=None)

    # 市场等权收益(剔除当日停牌/NaN)
    ret_m = ret.mean(axis=1)

    # ST 掩码(逐日)
    st_mask = pd.DataFrame(False, index=c.index, columns=c.columns)
    if NCHG.exists():
        nch = con.execute(
            f"SELECT code, name, start_date, end_date FROM read_parquet('{NCHG.as_posix()}/[!~]*.parquet') "
            "WHERE name LIKE '%ST%'"
        ).df()
        nch["start_date"] = pd.to_datetime(nch["start_date"])
        nch["end_date"] = pd.to_datetime(nch["end_date"]).fillna(pd.Timestamp("2099-12-31"))
        dates = c.index
        st_codes_by_day = {}
        for _, r in nch.iterrows():
            m = (dates >= r["start_date"]) & (dates <= r["end_date"])
            if m.any():
                for d in dates[m]:
                    st_codes_by_day.setdefault(d, set()).add(r["code"])
        for d, codes in st_codes_by_day.items():
            cols = [cc for cc in codes if cc in c.columns]
            st_mask.loc[d, cols] = True
        t(f"ST 掩码: {sum(len(v) for v in st_codes_by_day.values()):,} 股·日", t0)

    # 市值(log circ_mv)
    mv = con.execute(
        f"SELECT date, code, circ_mv FROM read_parquet('{BASIC.as_posix()}/[!~]*.parquet')"
    ).df()
    mv["date"] = pd.to_datetime(mv["date"])
    log_mv = mv.pivot(index="date", columns="code", values="circ_mv").apply(np.log)
    log_mv = log_mv.reindex(index=c.index, columns=c.columns)

    # 资金流占比
    mfd = con.execute(
        f"SELECT b.date AS date, b.code AS code, b.main_net AS main_net, k.amount AS amount "
        f"FROM read_parquet('{MFLOW.as_posix()}/[!~]*.parquet') b "
        f"JOIN read_parquet('{MIRROR.as_posix()}') k ON b.date=k.date AND b.code=k.code"
    ).df()
    mfd["date"] = pd.to_datetime(mfd["date"])
    mf_ratio = mfd.pivot(index="date", columns="code", values="main_net").div(
        mfd.pivot(index="date", columns="code", values="amount"), axis=0
    )
    mf_ratio = mf_ratio.reindex(index=c.index, columns=c.columns)
    t("daily_basic / moneyflow / namechange 加载完成", t0)

    meta: dict[str, dict] = {}

    def save(name: str, wdf: pd.DataFrame, desc: str, direction_note: str) -> None:
        long = wdf.stack().rename("value").reset_index()
        long.columns = ["date", "code", "value"]
        long.to_parquet(FACT / f"{name}.parquet", index=False)
        meta[name] = {
            "desc": desc,
            "direction_note": direction_note,
            "nonzero_rows": int(long["value"].notna().sum()),
        }
        print(f"  saved {name}: {long['value'].notna().sum():,} cells", flush=True)

    # ── 2. 因子计算 ────────────────────────────────────────────
    # qz_cvar_20: 20 日负日收益均值(等权) — 下行尾部损失, 高值=左尾厚
    neg = ret.where(ret < 0)
    qz_cvar = neg.rolling(W, min_periods=W // 2).mean()
    save("qz_cvar_20", qz_cvar, "20日负日收益均值(CVaR日线近似)", "原文G0高值多头: 高左尾→高收益(尾部风险反转)")

    # qz_kpath_illiq_20: 日K最短路径 / close / amount, 20日均
    path1 = (h - o).abs() + (h - l).abs() + (c - l).abs()
    path2 = (o - l).abs() + (h - l).abs() + (c - h).abs()
    kpath = np.minimum(path1, path2) / c
    qz_kpath = (kpath / amt.replace(0, np.nan)).rolling(W, min_periods=W // 2).mean()
    save("qz_kpath_illiq_20", qz_kpath, "日K最短路径/close/amount 20日均(日线路径非流动性)", "原文G0高值多头: 高冲击→非流动性溢价")

    # qz_idio_illiq_20: |ret - ret_m| / amount 20日均
    idio_abs = (ret.sub(ret_m, axis=0)).abs() / amt.replace(0, np.nan)
    qz_idio = idio_abs.rolling(W, min_periods=W // 2).mean()
    save("qz_idio_illiq_20", qz_idio, "|特质收益|/amount 20日均(特质非流动性)", "原文G0高值多头")

    # qz_kyle_lambda_60: 60日 ret ~ signed_amount OLS 斜率
    signed_amt = np.sign(ret) * amt
    qz_kyle = rolling_slope(ret, signed_amt, WL)
    save("qz_kyle_lambda_60", qz_kyle, "60日 ret~sign(ret)*amount OLS斜率(Kyle λ 日线近似)", "原文G0高值多头: λ大=冲击强")

    # qz_tsim_path_20: 20日 corr(ret_i, ret_m) - |20日累计差| (日线近似: dist 用窗口累计差绝对值)
    corr_pm = ret.rolling(W, min_periods=W // 2).corr(ret_m, pairwise=True)
    e = ret.sub(ret_m, axis=0)
    dist_end = e.rolling(W, min_periods=W // 2).sum().abs()
    qz_tsim = corr_pm - dist_end
    save("qz_tsim_path_20", qz_tsim, "20日市场相关 - 20日累计路径差(路径相似度日线近似)", "原文G0高值多头: 与市场路径同步→连续信息驱动")

    # qz_semi_illiq_dn_20 / _up: 下行/上行半程非流动性
    dn_abs = ret.where(ret < 0).abs() / amt.replace(0, np.nan)
    up_abs = ret.where(ret > 0).abs() / amt.replace(0, np.nan)
    qz_sidn = dn_abs.rolling(W, min_periods=W // 2).mean()
    qz_siup = up_abs.rolling(W, min_periods=W // 2).mean()
    save("qz_semi_illiq_dn_20", qz_sidn, "下行日|ret|/amount 20日均(SemiILLIQ下行, 复刻'下行冲击')", "推测口径: 高下行非流动→溢价(待验)")
    save("qz_semi_illiq_up_20", qz_siup, "上行日|ret|/amount 20日均(SemiILLIQ上行, 对照)", "对照组")

    # qz_amp_downpos_20: mean((H-C)/(H-L)) 20日均 — 收盘在日内振幅下段的占比
    rng = (h - l).replace(0, np.nan)
    downpos = (h - c) / rng
    qz_ampdp = downpos.rolling(W, min_periods=W // 2).mean()
    save("qz_amp_downpos_20", qz_ampdp, "mean((H-C)/(H-L)) 20日均(振幅下坡路占比)", "标题暗示: 占比高→值得买(待验)")

    # qz_repair_eff_20: (C - min20) / (max20 - min20) — 20日区间修复位置
    lo20 = l.rolling(W, min_periods=W // 2).min()
    hi20 = h.rolling(W, min_periods=W // 2).max()
    qz_repair = (c - lo20) / (hi20 - lo20).replace(0, np.nan)
    save("qz_repair_eff_20", qz_repair, "(C-min20)/(max20-min20)(修复效率/区间位置)", "推测口径: 高=修复完全(动量) vs 低=超跌(反转) 待验")

    # qz_mf_sens_60: 60日 ret ~ main_net/amount OLS 斜率
    qz_mfs = rolling_slope(ret, mf_ratio, WL)
    save("qz_mf_sens_60", qz_mfs, "60日 ret~主力净额占比 OLS斜率(资金流敏感度)", "原文标题: 越敏感越赚钱(高值多头, 待验)")

    # 对照因子: log_mv 与 amihud_20(湖) 由 eval 脚本加载

    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    # 同时落 log_mv / ST掩码 供 eval 用
    log_mv.stack().rename("value").reset_index().to_parquet(OUT / "log_mv.parquet", index=False)
    t("全部因子计算完成", t0)


if __name__ == "__main__":
    sys.exit(main())
