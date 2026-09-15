#!/usr/bin/env python
"""L0 扩展指标回填：十分位收益 + 正交化残差 IC（逐日 × 因子 → ext 湖）

设计思想（预计算 → 落库 → 页面轻聚合）：
  重截面计算（十分位 NTILE 分桶、残差 OLS）只在回填时做一次，逐日指标
  按因子平铺 part-{factor}.parquet 落 data/lake/factor_metric_ext/{pool}/；
  页面生成（build_all_center_lake.py）只读这些列做轻统计，不再重算。

口径（与 v3 build_dashboard.py 对齐，窗口 = 因子全史）：
  前瞻收益 = clean/mirror/kline_daily (close*adj_factor) LEAD 20
  十分位   = 先剔无前瞻行再 NTILE(10) ORDER BY 因子值，存逐日组内均值
             （页面聚合为日等权全期均值；v3 为股票日池化，差异可忽略）
  残差 IC  = 因子原始值对核心生产因子（size/amihud_20，剔除自身）逐日
             截面 OLS 取残差 → 与前瞻收益 rank IC；同 subset 的原始
             rank IC 一并存储（resid_raw_ic20）

用法：
  python scripts/factor_eval/backfill_metric_ext.py              # 全部（断点续跑）
  python scripts/factor_eval/backfill_metric_ext.py --only a,b   # 指定
  python scripts/factor_eval/backfill_metric_ext.py --force      # 覆盖重算
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "factor_eval"))
FACTOR_DIR = ROOT / "data" / "lake" / "factor"
AUDIT_DIR = ROOT / "data" / "lake" / "factor_audit"
MIRROR = ROOT / "data" / "lake" / "clean" / "mirror" / "kline_daily.parquet"
EXT_DIR = ROOT / "data" / "lake" / "factor_metric_ext"
POOL = "ashare_ex"
FAIL_LOG = ROOT / "logs" / "backfill_metric_ext_fails.txt"


def _value_col(p: Path) -> str:
    """因子湖文件值列名：普通因子 value，ML 分数因子 score（多一列 schema）"""
    import pyarrow.parquet as pq
    names = pq.ParquetFile(p).schema_arrow.names
    return "score" if ("value" not in names and "score" in names) else "value"


def _pivot_factor(name: str) -> pd.DataFrame:
    """因子湖单因子 → 宽表（index=date, columns=code）"""
    fs = sorted((FACTOR_DIR / name).glob("part-*.parquet"))
    if not fs:
        return pd.DataFrame()
    vc = _value_col(fs[0])
    df = pd.concat([pd.read_parquet(p, columns=["date", "code", vc])
                    for p in fs], ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    return df.pivot_table(index="date", columns="code", values=vc,
                          aggfunc="last")


def load_fwd20() -> pd.DataFrame:
    """mirror 日线 → h=20 前瞻收益宽表（LEAD 20 per code，与 v3 同语义）"""
    con = duckdb.connect()
    px = con.execute(f"""
        SELECT date, code,
               LEAD(close * adj_factor, 20)
                   OVER (PARTITION BY code ORDER BY date)
                   / (close * adj_factor) - 1 AS fwd
        FROM read_parquet('{MIRROR.as_posix()}')
    """).df()
    con.close()
    px["date"] = pd.to_datetime(px["date"])
    return px.pivot_table(index="date", columns="code", values="fwd",
                          aggfunc="last")


def compute_factor(name: str, fwd_w: pd.DataFrame,
                   core_mats: dict[str, pd.DataFrame]) -> pd.DataFrame | None:
    """单因子逐日扩展指标 → 长表 DataFrame（date, factor, dec*, resid*）"""
    fm = _pivot_factor(name)
    if fm.empty:
        return None
    # ── 十分位：先剔无前瞻行再 NTILE(10)（与 v3 WHERE fwd IS NOT NULL 同序）──
    idx = fm.index.intersection(fwd_w.index)
    cols_ = fm.columns.intersection(fwd_w.columns)
    fmx, fwx = fm.loc[idx, cols_], fwd_w.loc[idx, cols_]
    fmr = fmx.where(fwx.notna())
    rk = fmr.rank(axis=1, method="first")
    cnt = fmr.notna().sum(axis=1)
    bucket = np.ceil(rk.mul(10).div(cnt.clip(lower=1), axis=0))
    out = pd.DataFrame(index=idx)
    for d in range(1, 11):
        sel = bucket == d
        out[f"dec{d}_ret20"] = fwx.where(sel).mean(axis=1)

    # ── 正交化残差 IC（核心生产因子 OLS，逐日 ≥200 有效行）──
    cores = [c for c in ("size", "amihud_20")
             if c != name and c in core_mats and not core_mats[c].empty]
    resid_ic = pd.Series(np.nan, index=idx)
    resid_raw = pd.Series(np.nan, index=idx)
    n_cov = pd.Series(0, index=idx, dtype=int)
    if cores:
        idx_a, cols_a = idx, cols_
        for X in [core_mats[c] for c in cores] + [fwd_w]:
            idx_a = idx_a.intersection(X.index)
            cols_a = cols_a.intersection(X.columns)
        idx, cols_ = idx_a, cols_a
        yn = fmx.loc[idx, cols_].to_numpy(dtype=float)
        fn = fwd_w.loc[idx, cols_].to_numpy(dtype=float)
        Xms = [core_mats[c].loc[idx, cols_].to_numpy(dtype=float)
               for c in cores]
        r1 = np.full(len(idx), np.nan)
        r2 = np.full(len(idx), np.nan)
        r3 = np.zeros(len(idx), dtype=int)
        for i in range(len(idx)):
            y, f = yn[i], fn[i]
            m = ~(np.isnan(y) | np.isnan(f))
            for x in Xms:
                m &= ~np.isnan(x[i])
            n = int(m.sum())
            if n < 200:
                continue
            r3[i] = n
            ym, fm_ = y[m], f[m]
            X = np.column_stack([np.ones(n)] + [x[i][m] for x in Xms])
            beta, *_ = np.linalg.lstsq(X, ym, rcond=None)
            rr = pd.Series(ym - X @ beta).rank()
            frk = pd.Series(fm_).rank()
            yrk = pd.Series(ym).rank()
            r1[i] = rr.corr(frk)
            r2[i] = yrk.corr(frk)
        resid_ic = pd.Series(r1, index=idx)
        resid_raw = pd.Series(r2, index=idx)
        n_cov = pd.Series(r3, index=idx)

    out = out.loc[resid_ic.index]
    out["resid_ic20"] = resid_ic.round(6)
    out["resid_raw_ic20"] = resid_raw.round(6)
    out["n_cov20"] = n_cov
    out["factor"] = name
    out = out.reset_index().rename(columns={"index": "date"})
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="L0 扩展指标回填（十分位+残差 IC）")
    p.add_argument("--only", default="", help="逗号分隔因子名，只跑指定")
    p.add_argument("--force", action="store_true", help="覆盖已存在文件")
    a = p.parse_args()

    names = ([x.strip() for x in a.only.split(",") if x.strip()]
             if a.only else sorted(q.stem for q in AUDIT_DIR.glob("*.json")))
    out_dir = EXT_DIR / POOL
    out_dir.mkdir(parents=True, exist_ok=True)

    todo = [n for n in names
            if a.force or not (out_dir / f"part-{n}.parquet").exists()]
    print(f"[backfill] L0 扩展指标：{len(todo)}/{len(names)} 因子待回填"
          f"（十分位 dec1~dec10_ret20 + resid_ic20，h=20）", flush=True)
    if not todo:
        print("[done] 无待回填因子", flush=True)
        return

    t0 = time.time()
    print("[preload] mirror 日线 → fwd20 宽表…", flush=True)
    fwd_w = load_fwd20()
    print("[preload] 核心生产因子矩阵（size/amihud_20）…", flush=True)
    core_mats = {c: _pivot_factor(c) for c in ("size", "amihud_20")
                 if (FACTOR_DIR / c).exists()}

    ok, fails = 0, []
    for i, name in enumerate(todo, 1):
        t1 = time.time()
        try:
            df = compute_factor(name, fwd_w, core_mats)
            if df is None or df.empty:
                raise ValueError("因子湖无数据")
            df.to_parquet(out_dir / f"part-{name}.parquet", index=False)
            ok += 1
            print(f"[{i}/{len(todo)}] ✅ {name} "
                  f"({time.time()-t1:.1f}s, {(time.time()-t0)/60:.0f}min)",
                  flush=True)
        except Exception:
            fails.append(name)
            print(f"[{i}/{len(todo)}] ❌ {name}: "
                  f"{traceback.format_exc().strip().splitlines()[-1][:120]}",
                  flush=True)

    if fails:
        FAIL_LOG.parent.mkdir(exist_ok=True)
        FAIL_LOG.write_text("\n".join(fails), encoding="utf-8")
    print(f"[done] 成功 {ok} / 失败 {len(fails)} / 共 {len(todo)}"
          f"（{(time.time()-t0)/60:.1f} min）", flush=True)
    if fails:
        print(f"失败清单：{fails}（已写 {FAIL_LOG.name}）", flush=True)


if __name__ == "__main__":
    main()
