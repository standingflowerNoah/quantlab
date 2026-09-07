#!/usr/bin/env python3
"""高频因子 walk-forward 滚动验证（项目硬约束：进信号层前必须通过）
用法: python scripts/highfreq_walkforward.py
对应研究报告 7.3 优先方向 ⑥

设计（月频调仓事件口径，严格无前视）：
  - 调仓日 = 每月首个交易日；验证收益 = 调仓日截面的 fwd20（T+20 持有）
  - 训练窗 = 调仓日前推 N 月，且窗末留 20 交易日 purge
    （训练窗内任意 d 的 fwd20 终点 ≤ 调仓日-1，不泄漏进验证段）
  - 因子方向与权重**由训练窗内估**（中性化后逐日 IC 的 sign / ICIR），
    不用文献先验、不用全样本信息——这是与 8.3 节准 OOS 的本质区别
  - 中性化只用当日截面（size/turnover/volatility_20 当日可知，无前视），
    因此全历史一次性预中性化是合法的

方案：A（训练 6 个月）× B（训练 12 个月）× 因子集 {hf7, hf6(剔除 amihud)}
      × 合成 {等权, ICIR 加权} + 全样本固定方向对照（有前视，上界参照）

输出：逐调仓日事件明细 CSV + 汇总表（OOS IC / ICIR / 多空年化 / 夏普）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import duckdb

ROOT = Path(__file__).resolve().parent.parent
LAKE = ROOT / "data" / "lake"
_con = duckdb.connect()          # 纯内存连接：直读 parquet，绕开写锁


def q(sql: str) -> pd.DataFrame:
    return _con.execute(sql).df()


def _p(sub: str) -> str:
    return str(LAKE / sub).replace("\\", "/")


def zscore_s(g: pd.Series) -> pd.Series:
    s = g.std()
    return (g - g.mean()) / s if s and np.isfinite(s) else g * np.nan


def load_factor(name):
    g = _p(f"factor/{name}") + "/part-*.parquet"
    fv = q(f"SELECT date, code, value FROM read_parquet('{g}')")
    fv["date"] = pd.to_datetime(fv["date"])
    fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
    return fv.dropna(subset=["value"])


def fwd_returns(horizon):
    kl = _p("clean/mirror/kline_daily.parquet")
    df = q(f"""
        SELECT date, code, c_lead / c - 1 AS fwd FROM (
            SELECT date, code, close * adj_factor AS c,
                   LEAD(close * adj_factor, {horizon}) OVER (
                       PARTITION BY code ORDER BY date) AS c_lead
            FROM read_parquet('{kl}'))
        WHERE c_lead IS NOT NULL AND c > 0
    """)
    df["date"] = pd.to_datetime(df["date"])
    return df[["date", "code", "fwd"]].dropna()


STYLES = ["size", "turnover", "volatility_20"]

# 因子集：hf7 = 8.3 节合成口径；hf6 = 8.5 修订（剔除 amihud）
HF7 = ["hf_rsk_20", "hf_dsem_20", "hf_corr_rv_20", "hf_amtrange_20",
       "hf_rvvol_20", "hf_amihud_20", "hf_smartq_10"]
HF6 = [n for n in HF7 if n != "hf_amihud_20"]
# 文献先验方向（仅用于对照与方向翻转统计，WF 不用）
PRIOR = {"hf_rsk_20": -1, "hf_dsem_20": +1, "hf_corr_rv_20": -1,
         "hf_amtrange_20": -1, "hf_rvvol_20": -1, "hf_amihud_20": +1,
         "hf_smartq_10": -1}


def neutralize(fv: pd.DataFrame, style_wide: pd.DataFrame,
               min_n=100) -> pd.DataFrame:
    """截面 OLS 残差（风格 rank 归一）；style_wide: date,code,<styles>"""
    df = fv.merge(style_wide, on=["date", "code"], how="left")
    for c in STYLES:
        df[c] = df.groupby("date")[c].transform(
            lambda x: x.rank(pct=True) - 0.5)
    df = df.dropna(subset=STYLES + ["value"])
    out = []
    for d, g in df.groupby("date"):
        if len(g) < min_n:
            continue
        X = np.column_stack([np.ones(len(g))] +
                            [g[c].values for c in STYLES])
        y = g["value"].values.astype(float)
        try:
            beta = np.linalg.lstsq(X, y, rcond=None)[0]
            out.append(g[["date", "code"]].assign(value=y - X @ beta))
        except np.linalg.LinAlgError:
            continue
    return (pd.concat(out, ignore_index=True)
              .sort_values(["date", "code"]).reset_index(drop=True))


def day_ic_series(fv: pd.DataFrame, fwd: pd.DataFrame,
                  min_n=30) -> pd.Series:
    """逐日截面 Spearman（index=date）"""
    m = fv.merge(fwd, on=["date", "code"], how="inner")
    out = {}
    for d, g in m.groupby("date"):
        if len(g) < min_n:
            continue
        out[d] = g["value"].rank().corr(g["fwd"].rank())
    return pd.Series(out, dtype=float)


def main():
    print("载入因子 / 风格 / 前视收益（纯 parquet）...")
    fwd = fwd_returns(20)
    all_days = sorted(fwd["date"].unique())
    # 调仓日：每月首个交易日（且存在 fwd20）
    s = pd.Series(all_days)
    rebal = (pd.DataFrame({"d": s})
             .groupby([s.dt.year, s.dt.month])["d"].min().tolist())
    day_idx = {d: i for i, d in enumerate(all_days)}
    print(f"交易日 {len(all_days)}（{all_days[0]:%Y-%m-%d}~"
          f"{all_days[-1]:%Y-%m-%d}），调仓日 {len(rebal)} 个")

    # 风格宽表（一次载入，中性化复用）
    st = load_factor(STYLES[0]).rename(columns={"value": STYLES[0]})
    for c in STYLES[1:]:
        st = st.merge(load_factor(c).rename(columns={"value": c}),
                      on=["date", "code"], how="outer")

    # 预中性化 + 逐日 IC（每因子一次）
    pure, day_ic = {}, {}
    for n in HF7:
        fv = neutralize(load_factor(n), st)
        pure[n] = fv
        day_ic[n] = day_ic_series(fv, fwd)
        print(f"  {n}: 中性化 {len(fv):,} 行，逐日 IC {len(day_ic[n])} 日")

    # 调仓日截面合成值（当日 zscore 后按 WF 方向加权）
    print("\nwalk-forward 滚动（方案 A: 训练6月 / B: 训练12月；purge 20 交易日）...")
    events = []
    for t in rebal:
        ti = day_idx[t]
        if ti < 21:                      # 无法 purge
            continue
        purge_end = all_days[ti - 21]     # 训练窗末（其 fwd20 ≤ t-1）
        for scheme, months in (("A", 6), ("B", 12)):
            # 训练窗起点：purge_end 往前 months 个月
            start = purge_end - pd.DateOffset(months=months)
            for fset_name, fset in (("hf7", HF7), ("hf6", HF6)):
                # 训练窗内估方向 / ICIR
                dirs, wts = {}, {}
                for n in fset:
                    ic = day_ic[n]
                    w = ic[(ic.index >= start) & (ic.index <= purge_end)]
                    if len(w) < 40 or not np.isfinite(w.mean()):
                        dirs[n], wts[n] = 0.0, 0.0
                        continue
                    dirs[n] = np.sign(w.mean())
                    wts[n] = (w.mean() / w.std()
                              if w.std() > 0 else 0.0)
                # 调仓日截面合成（列名=因子名，显式无歧义）
                base = None
                for n in fset:
                    if dirs.get(n, 0) == 0:
                        continue
                    v = pure[n][pure[n]["date"] == t][["date", "code", "value"]]
                    if v.empty:
                        continue
                    v = v.assign(z=v.groupby("date")["value"].transform(zscore_s)
                                 * dirs[n])[["date", "code", "z"]].rename(
                                     columns={"z": n})
                    base = v if base is None else base.merge(
                        v, on=["date", "code"], how="inner")
                if base is None:
                    continue
                cols = [n for n in fset if n in base.columns]
                if not cols:
                    continue
                for mode in ("eq", "icir"):
                    if mode == "eq":
                        base["comp"] = base[cols].mean(axis=1)
                    else:
                        # ICIR 加权：训练窗 ICIR 为权（截负置 0）
                        w = {n: max(wts.get(n, 0.0), 0.0) for n in cols}
                        wsum = sum(w.values())
                        base["comp"] = (
                            base[cols].mul(pd.Series(w)).sum(axis=1)
                            / (wsum or 1.0))
                    m = base[["date", "code", "comp"]].merge(
                        fwd[fwd["date"] == t], on=["date", "code"])
                    if len(m) < 200:
                        continue
                    ev_ic = m["comp"].rank().corr(m["fwd"].rank())
                    m = m.sort_values("comp")
                    k = max(int(len(m) * 0.2), 5)
                    ls = (m["fwd"].tail(k).mean()
                          - m["fwd"].head(k).mean())
                    events.append({
                        "rebal": t, "scheme": scheme, "fset": fset_name,
                        "mode": mode, "n": len(m),
                        "ic": round(float(ev_ic), 4),
                        "ls20": round(float(ls), 4),
                        "dir_flip": ",".join(
                            n for n in fset
                            if dirs.get(n, 0) != 0
                            and dirs[n] != PRIOR[n]),
                    })
    ev = pd.DataFrame(events)
    ev.to_csv(ROOT / "reports" / "highfreq_walkforward.csv", index=False)
    print(f"事件明细 → reports/highfreq_walkforward.csv（{len(ev)} 条）")

    # 全样本固定方向对照（有前视上界；8.3 口径在调仓日上的表现）
    fixed = []
    for t in rebal:
        base = None
        for n in HF7:
            v = pure[n][pure[n]["date"] == t][["date", "code", "value"]]
            if v.empty:
                continue
            v = v.assign(z=v.groupby("date")["value"].transform(zscore_s)
                         * PRIOR[n])[["date", "code", "z"]].rename(
                             columns={"z": n})
            base = v if base is None else base.merge(
                v, on=["date", "code"], how="inner")
        if base is None:            # 该调仓日无因子数据（如 2022-2024）
            continue
        cols = [n for n in HF7 if n in base.columns]
        if not cols:
            continue
        base["comp"] = base[cols].mean(axis=1)
        m = base[["date", "code", "comp"]].merge(fwd[fwd["date"] == t],
                                                 on=["date", "code"])
        if len(m) < 200:
            continue
        ic = m["comp"].rank().corr(m["fwd"].rank())
        m = m.sort_values("comp")
        k = max(int(len(m) * 0.2), 5)
        ls = m["fwd"].tail(k).mean() - m["fwd"].head(k).mean()
        fixed.append({"rebal": t, "ic": float(ic), "ls20": float(ls)})
    fx = pd.DataFrame(fixed)

    # 汇总
    print("\n=== walk-forward 汇总（OOS，事件=月度调仓截面 vs fwd20）===")
    print(f"{'方案':>4s} {'因子集':>5s} {'加权':>5s} {'事件数':>5s} "
          f"{'OOS IC':>8s} {'ICIR':>6s} {'win':>5s} {'多空年化':>8s} "
          f"{'夏普':>6s} {'方向翻转因子'}")
    for (scheme, fset, mode), g in ev.groupby(
            ["scheme", "fset", "mode"], sort=True):
        ic = g["ic"]
        ann = g["ls20"].mean() * 12
        sharpe = (g["ls20"].mean() / g["ls20"].std() * np.sqrt(12)
                  if g["ls20"].std() > 0 else np.nan)
        flips = set()
        for s in g["dir_flip"]:
            if s:
                flips.update(x for x in s.split(",") if x)
        print(f"{scheme:>4s} {fset:>5s} {mode:>5s} {len(g):>5d} "
              f"{ic.mean():>+8.4f} {ic.mean() / ic.std():>+6.2f} "
              f"{(ic > 0).mean():>5.2f} {ann:>+8.2%} {sharpe:>+6.2f} "
              f"{','.join(sorted(flips)) or '-'}")
    print("\n对照（全样本固定方向 hf7 等权，有前视上界）: "
          f"IC {fx['ic'].mean():+.4f} / ICIR "
          f"{fx['ic'].mean() / fx['ic'].std():+.2f} / "
          f"多空年化 {fx['ls20'].mean() * 12:+.2%} / 夏普 "
          f"{fx['ls20'].mean() / fx['ls20'].std() * np.sqrt(12):+.2f}")

    # 方向翻转明细（amihud 稳定性检验）
    print("\n=== 训练窗方向与文献先验不一致的事件（因子×次数）===")
    flip_cnt = {}
    for s in ev["dir_flip"]:
        for x in (s or "").split(","):
            if x:
                flip_cnt[x] = flip_cnt.get(x, 0) + 1
    for n, c in sorted(flip_cnt.items(), key=lambda kv: -kv[1]):
        print(f"  {n}: {c} 次")

    print("\n完成。")


if __name__ == "__main__":
    main()
