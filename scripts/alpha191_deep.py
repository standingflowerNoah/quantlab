#!/usr/bin/env python3
"""Alpha191 深化检验：中性化纯净 IC + 冗余聚类 + walk-forward 验证
用法: python scripts/alpha191_deep.py

背景：Alpha191 已完成全样本评估（180 因子 IC + top10 融合组合年化
12.7%/IR 0.77，reports/alpha191_model.json）——但 top10 是从 180 个
因子里按**全样本** ICIR 挑选的（选择偏差），且未做 walk-forward。
本项目硬约束：上线前必须通过样本外/滚动验证。

本脚本对齐高频因子研究（research/分钟高频因子研究.md）的方法论：
  1) top20 代表因子的 raw vs 风格中性纯净 IC（T+5/T+20）
  2) 中性化后因子间相关（独立信号数估计）
  3) walk-forward：月度调仓事件，12 月训练窗 + purge 21 交易日，
     方向/权重由训练窗内估（不用全样本先验）——a191 样本 2022-06 起，
     约 38 个 OOS 月（统计功效远高于高频因子的 16 个月）
  4) 对照：全样本固定方向（前视上界，即 model.json 口径的 IC 等价物）

纯 Parquet 路径（绕开 quant.duckdb 写锁）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import duckdb

ROOT = Path(__file__).resolve().parent.parent
LAKE = ROOT / "data" / "lake"
_con = duckdb.connect()


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
# model.json a191_top10（全样本选出的生产候选——待 WF 检验）
TOP10 = ["alpha016", "alpha013", "alpha015", "alpha126", "alpha055",
         "alpha119", "alpha026", "alpha183", "alpha148", "alpha116"]
PRIOR = {n: +1 for n in TOP10}      # 全样本 IC 均为正
TRAIN_MONTHS = 12
PURGE_TD = 21
MIN_IC_DAYS = 40


def neutralize(fv, st, min_n=100):
    df = fv.merge(st, on=["date", "code"], how="left")
    for c in STYLES:
        df[c] = df.groupby("date")[c].transform(
            lambda x: x.rank(pct=True) - 0.5)
    df = df.dropna(subset=STYLES + ["value"])
    out = []
    for d, g in df.groupby("date"):
        if len(g) < min_n:
            continue
        X = np.column_stack([np.ones(len(g))] + [g[c].values for c in STYLES])
        y = g["value"].values.astype(float)
        try:
            beta = np.linalg.lstsq(X, y, rcond=None)[0]
            out.append(g[["date", "code"]].assign(value=y - X @ beta))
        except np.linalg.LinAlgError:
            continue
    return (pd.concat(out, ignore_index=True)
              .sort_values(["date", "code"]).reset_index(drop=True))


def day_ic(fv, fwd, min_n=30):
    m = fv.merge(fwd, on=["date", "code"], how="inner")
    out = {}
    for d, g in m.groupby("date"):
        if len(g) < min_n:
            continue
        out[d] = g["value"].rank().corr(g["fwd"].rank())
    return pd.Series(out, dtype=float)


def ic_stats(ic):
    if len(ic) == 0 or ic.std() == 0:
        return {"n": len(ic), "ic": np.nan, "icir": np.nan, "win": np.nan}
    return {"n": len(ic), "ic": round(float(ic.mean()), 4),
            "icir": round(float(ic.mean() / ic.std()), 3),
            "win": round(float((ic > 0).mean()), 3)}


def main():
    fwd5, fwd20 = fwd_returns(5), fwd_returns(20)
    print("载入风格因子...")
    st = load_factor(STYLES[0]).rename(columns={"value": STYLES[0]})
    for c in STYLES[1:]:
        st = st.merge(load_factor(c).rename(columns={"value": c}),
                      on=["date", "code"], how="outer")
    print(f"风格覆盖: {st['date'].min().date()} ~ {st['date'].max().date()}")

    # 1) top10 + representatives 里补几个（前 20 里的非 top10 者）
    reps = pd.read_csv(ROOT / "reports" / "alpha191_representatives.csv")
    extra = [f for f in reps["factor"].head(20).tolist() if f not in TOP10]
    allf = TOP10 + extra[:5]        # 深化检验因子集（top10 + 5 个补充）

    print(f"\n中性化 {len(allf)} 个因子（{allf[0]} 等，2022-06 起）...")
    pure, ic20 = {}, {}
    for n in allf:
        fv = load_factor(n)
        p = neutralize(fv, st)
        pure[n] = p
        ic20[n] = day_ic(p, fwd20)

    print("\n=== 1) 纯净 IC：raw vs 风格中性（T+20）===")
    print(f"{'因子':10s} {'raw IC':>15s} {'中性 IC':>15s} {'衰减':>6s}")
    for n in allf:
        r = ic_stats(day_ic(load_factor(n), fwd20)) if n in TOP10 else None
        if r is None:
            continue
        s = ic_stats(ic20[n])
        dec = 1 - abs(s["ic"]) / abs(r["ic"]) if r["ic"] else np.nan
        print(f"{n:10s} {r['ic']:+.4f}/{r['icir']:+.2f} "
              f"{s['ic']:+.4f}/{s['icir']:+.2f} {dec:>6.0%}")

    print("\n=== 2) 中性化后因子间相关（方向统一，上三角）===")
    cors = {}
    for i, a in enumerate(allf):
        for b in allf[i + 1:]:
            m = (pure[a].merge(pure[b], on=["date", "code"],
                               suffixes=("_a", "_b")))
            cc = m.groupby("date").apply(
                lambda g: g["value_a"].rank().corr(g["value_b"].rank()))
            if len(cc):
                cors[(a, b)] = round(float(cc.mean()), 3)
    vals = [abs(v) for v in cors.values()]
    print(f"两两 |corr| 均值 {np.mean(vals):.3f} / 最大 {np.max(vals):.3f}")
    for (a, b), v in sorted(cors.items(), key=lambda kv: -abs(kv[1]))[:8]:
        print(f"  {a} ~ {b}: {v:+.3f}")

    # 3) walk-forward（月度调仓事件；训练窗内估方向/权重）
    print("\n=== 3) walk-forward（12 月训练窗 + purge 21 交易日）===")
    all_days = sorted(fwd20["date"].unique())
    s = pd.Series(all_days)
    rebal = (pd.DataFrame({"d": s})
             .groupby([s.dt.year, s.dt.month])["d"].min().tolist())
    day_idx = {d: i for i, d in enumerate(all_days)}
    events = []
    for t in rebal:
        ti = day_idx[t]
        if ti < PURGE_TD:
            continue
        purge_end = all_days[ti - PURGE_TD]
        w_start = purge_end - pd.DateOffset(months=TRAIN_MONTHS)
        dirs, wts = {}, {}
        for n in TOP10:
            w = ic20[n]
            ww = w[(w.index >= w_start) & (w.index <= purge_end)]
            if len(ww) < MIN_IC_DAYS or not np.isfinite(ww.mean()):
                continue
            dirs[n] = np.sign(ww.mean())
            wts[n] = max(ww.mean() / ww.std(), 0.0) if ww.std() > 0 else 0.0
        if not any(dirs.values()):
            continue
        base = None
        for n in TOP10:
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
        cols = [n for n in TOP10 if n in base.columns]
        if not cols:
            continue
        for mode in ("eq", "icir"):
            if mode == "eq":
                base["comp"] = base[cols].mean(axis=1)
            else:
                w = {n: max(wts.get(n, 0.0), 0.0) for n in cols}
                wsum = sum(w.values())
                base["comp"] = (base[cols].mul(pd.Series(w)).sum(axis=1)
                                / (wsum or 1.0))
            m = base[["date", "code", "comp"]].merge(
                fwd20[fwd20["date"] == t], on=["date", "code"])
            if len(m) < 200:
                continue
            ic = m["comp"].rank().corr(m["fwd"].rank())
            m = m.sort_values("comp")
            k = max(int(len(m) * 0.2), 5)
            ls = m["fwd"].tail(k).mean() - m["fwd"].head(k).mean()
            events.append({"rebal": t, "mode": mode, "n": len(m),
                           "ic": round(float(ic), 4),
                           "ls20": round(float(ls), 4),
                           "dir_flip": ",".join(
                               n for n in TOP10
                               if dirs.get(n, 0) != 0 and dirs[n] != PRIOR[n])})
    ev = pd.DataFrame(events)
    ev.to_csv(ROOT / "reports" / "alpha191_walkforward.csv", index=False)
    print(f"事件明细 → reports/alpha191_walkforward.csv（{len(ev)} 条）")

    print(f"\n{'方案':>6s} {'事件数':>5s} {'OOS IC':>8s} {'ICIR':>6s} "
          f"{'win':>5s} {'多空年化':>8s} {'夏普':>6s}")
    for mode, g in ev.groupby("mode"):
        ic = g["ic"]
        ann = g["ls20"].mean() * 12
        sharpe = (g["ls20"].mean() / g["ls20"].std() * np.sqrt(12)
                  if g["ls20"].std() > 0 else np.nan)
        print(f"{mode:>6s} {len(g):>5d} {ic.mean():>+8.4f} "
              f"{ic.mean() / ic.std():>+6.2f} {(ic > 0).mean():>5.2f} "
              f"{ann:>+8.2%} {sharpe:>+6.2f}")

    # 4) 对照：全样本固定方向（前视上界）
    fixed = []
    for t in rebal:
        base = None
        for n in TOP10:
            v = pure[n][pure[n]["date"] == t][["date", "code", "value"]]
            if v.empty:
                continue
            v = v.assign(z=v.groupby("date")["value"].transform(zscore_s)
                         * PRIOR[n])[["date", "code", "z"]].rename(
                             columns={"z": n})
            base = v if base is None else base.merge(
                v, on=["date", "code"], how="inner")
        if base is None:
            continue
        cols = [n for n in TOP10 if n in base.columns]
        base["comp"] = base[cols].mean(axis=1)
        m = base[["date", "code", "comp"]].merge(fwd20[fwd20["date"] == t],
                                                 on=["date", "code"])
        if len(m) < 200:
            continue
        ic = m["comp"].rank().corr(m["fwd"].rank())
        m = m.sort_values("comp")
        k = max(int(len(m) * 0.2), 5)
        fixed.append({"rebal": t, "ic": float(ic),
                      "ls20": float(m["fwd"].tail(k).mean()
                                    - m["fwd"].head(k).mean())})
    fx = pd.DataFrame(fixed)
    ic = fx["ic"]
    print(f"\n对照（全样本固定方向 top10 等权，前视上界）: "
          f"IC {ic.mean():+.4f} / ICIR {ic.mean() / ic.std():+.2f} / "
          f"多空年化 {fx['ls20'].mean() * 12:+.2%} / 夏普 "
          f"{fx['ls20'].mean() / fx['ls20'].std() * np.sqrt(12):+.2f}")

    # 方向翻转统计
    print("\n=== 训练窗方向 vs 全样本先验（+1）翻转次数 ===")
    fc = {}
    for s_ in ev["dir_flip"]:
        for x in (s_ or "").split(","):
            if x:
                fc[x] = fc.get(x, 0) + 1
    for n, c in sorted(fc.items(), key=lambda kv: -kv[1]):
        print(f"  {n}: {c} 次")
    if not fc:
        print("  （零翻转）")

    # 分年 OOS（icir 模式）
    print("\n=== OOS IC 分年（icir 模式）===")
    gi = ev[ev["mode"] == "icir"]
    for yr, g in gi.groupby(pd.to_datetime(gi["rebal"]).dt.year):
        print(f"  {yr}: IC {g['ic'].mean():+.4f} / "
              f"多空年化 {g['ls20'].mean() * 12:+.2%}（{len(g)} 月）")

    print("\n完成。")


if __name__ == "__main__":
    main()
