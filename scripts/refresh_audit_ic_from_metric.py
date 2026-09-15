# -*- coding: utf-8 -*-
"""audit JSON 无锁全历史刷新（IC 从 metric_store L0 聚合 + structure 从湖重算）
=====================================================================
背景：hf 21 因子 2022-2024 历史段经无锁路径回填后，factor_audit JSON 的
structure/coverage/ic 块仍停留在 2025-01-22 起（原 audit 只见 2025+ 分区；
refresh_audit_ic.py 的 factor_ic_extended 虽读湖因子，但 px 走主库 kline_daily，
主库被 metric_store 回填进程独占锁持有 → 不可用）。

本脚本完全无锁：
  - checks.ic      ← data/lake/factor_metric_daily/ashare_ex/part-*.parquet
                     按 factor 聚合 rank/pearson × h=1/5/20（mean/ICIR/win），
                     口径与 factor_ic_extended 等价（已对 hf_rsk_20 交叉验证
                     rank_ic20=-0.0722 与评估汇总表精确一致）
  - checks.structure/coverage ← data/lake/factor/<name>/*.parquet 湖直算
  - PIT 块不动（构造未变，原结论仍有效）

v2 schema 与 refresh_audit_ic.py 保持一致（legacy ic5/icir5/win5/ic20/icir20
= rank 值，batch_metrics/FDR 兼容）。source 标 metric_store_l0。

用法：python scripts/refresh_audit_ic_from_metric.py [--names a,b] [--dry]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

AUDIT_DIR = ROOT / "data" / "lake" / "factor_audit"
FACTOR_DIR = ROOT / "data" / "lake" / "factor"
METRIC_GLOB = str(ROOT / "data/lake/factor_metric_daily/ashare_ex/part-*.parquet")
HORIZONS = (1, 5, 20)

# 复用运行环境既有内存连接约定：小查询不需要 memory_limit 调优
CON = duckdb.connect()


def _agg_ic_metric(factor: str) -> dict | None:
    """从 metric_store L0 聚合 v2 IC 块（全历史日 IC 序列的统计量）。"""
    cols = []
    for h in HORIZONS:
        cols.append(
            f"avg(rank_ic{h}) AS rank_ic{h}, "
            f"avg(rank_ic{h})/stddev(rank_ic{h}) AS rank_icir{h}, "
            f"avg(CASE WHEN rank_ic{h} > 0 THEN 1.0 ELSE 0 END) AS rank_win{h}, "
            f"count(rank_ic{h}) AS n_days_h{h}, "
            f"avg(pearson_ic{h}) AS pearson_ic{h}, "
            f"avg(pearson_ic{h})/stddev(pearson_ic{h}) AS pearson_icir{h}, "
            f"avg(CASE WHEN pearson_ic{h} > 0 THEN 1.0 ELSE 0 END) AS pearson_win{h}"
        )
    sql = f"""
    SELECT count(*) AS n_days, avg(n_codes) AS avg_n, {", ".join(cols)}
    FROM read_parquet('{METRIC_GLOB}')
    WHERE factor = '{factor}'
      AND (rank_ic1 IS NOT NULL OR rank_ic5 IS NOT NULL OR rank_ic20 IS NOT NULL)
    """
    df = CON.execute(sql).fetchdf()
    if df.empty or df["n_days"].iloc[0] == 0:
        return None
    r = df.iloc[0]

    def _s(x):
        v = float(x)
        return None if v != v else round(v, 4)

    out: dict = {"n_days": int(r["n_days"]), "avg_n": int(round(float(r["avg_n"])))}
    for h in HORIZONS:
        out[f"rank_ic{h}"] = _s(r[f"rank_ic{h}"])
        out[f"rank_icir{h}"] = _s(r[f"rank_icir{h}"])
        out[f"rank_win{h}"] = _s(r[f"rank_win{h}"])
        out[f"n_days_h{h}"] = int(r[f"n_days_h{h}"])
        out[f"pearson_ic{h}"] = _s(r[f"pearson_ic{h}"])
        out[f"pearson_icir{h}"] = _s(r[f"pearson_icir{h}"])
        out[f"pearson_win{h}"] = _s(r[f"pearson_win{h}"])
    ic5 = out["rank_ic5"]
    out.update({
        "ic5": ic5, "icir5": out["rank_icir5"], "win5": out["rank_win5"],
        "ic20": out["rank_ic20"], "icir20": out["rank_icir20"],
        "direction": 1 if (ic5 or 0) > 0 else -1,
        "source": "metric_store_l0",
        "full_history": True,
    })
    return out


def _calc_structure(factor: str) -> dict:
    """从湖 glob 重算 structure + coverage 块（无锁）。"""
    g = (FACTOR_DIR / factor).as_posix() + "/*.parquet"
    row = CON.execute(f"""
        SELECT count(*) AS n_rows,
               count(DISTINCT (date, code)) AS n_keys,
               min(date) AS date_min, max(date) AS date_max,
               count(DISTINCT code) AS n_codes,
               count(DISTINCT CAST(date AS DATE)) AS n_days,
               sum(CASE WHEN value IS NULL THEN 1 ELSE 0 END) AS nan_values,
               sum(CASE WHEN isinf(value) THEN 1 ELSE 0 END) AS inf_values,
               avg(CASE WHEN abs(value) > 1e-12 THEN 1.0 ELSE 0 END) AS nonzero_ratio,
               max(abs(value)) AS abs_max
        FROM read_parquet('{g}')
    """).fetchone()
    cpd = CON.execute(f"""
        SELECT min(n) AS cmin, median(n) AS cmed, max(n) AS cmax
        FROM (SELECT CAST(date AS DATE) AS d, count(*) AS n
              FROM read_parquet('{g}') GROUP BY d)
    """).fetchone()
    (n_rows, n_keys, dmin, dmax, n_codes, n_days,
     nan_v, inf_v, nz, amax) = row
    return {
        "n_rows": int(n_rows),
        "date_min": str(dmin)[:10],
        "date_max": str(dmax)[:10],
        "n_codes": int(n_codes),
        "dup_date_code_rows": int(n_rows - n_keys),
        "inf_values": int(inf_v),
        "nan_values": int(nan_v),
        "nonzero_ratio": round(float(nz), 4),
        "abs_max": round(float(amax), 6),
        "_coverage": {
            "n_days": int(n_days),
            "codes_per_day_min": int(cpd[0]),
            "codes_per_day_median": int(cpd[1]),
            "codes_per_day_max": int(cpd[2]),
        },
    }


def refresh_one(factor: str, dry: bool) -> str:
    fp = AUDIT_DIR / f"{factor}.json"
    if not fp.exists():
        return "no-json"
    d = json.loads(fp.read_text(encoding="utf-8"))

    ic = _agg_ic_metric(factor)
    if ic is None:
        return "no-metric"
    st = _calc_structure(factor)
    cov = st.pop("_coverage")

    if not dry:
        d["checks"]["ic"] = ic
        d["checks"]["structure"] = st
        d["checks"]["coverage"] = cov
        d["n_rows"] = st["n_rows"]
        d["latest_date"] = st["date_max"]
        d["ic_updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        d["structure_refreshed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        d["ic_schema"] = "v2"
        fp.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                      encoding="utf-8")
    return (f"ok ic20={ic['ic20']} icir20={ic['icir20']} "
            f"rows={st['n_rows']:,} span={st['date_min']}~{st['date_max']}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--names", help="逗号分隔子集（缺省湖内全部 hf_*）")
    p.add_argument("--dry", action="store_true", help="只打印不写回")
    a = p.parse_args()

    if a.names:
        names = [s.strip() for s in a.names.split(",")]
    else:
        names = sorted(pp.name for pp in FACTOR_DIR.glob("hf_*")
                       if pp.is_dir())

    n_ok = n_skip = 0
    t0 = time.time()
    for i, name in enumerate(names, 1):
        try:
            status = refresh_one(name, a.dry)
        except Exception as e:  # noqa: BLE001
            status = f"FAIL {type(e).__name__}: {str(e)[:120]}"
        if status.startswith(("ok", "dry")) or status.startswith("ok"):
            n_ok += 1
        elif status in ("no-json", "no-metric"):
            n_skip += 1
        print(f"[{i}/{len(names)}] {name}: {status}", flush=True)
    print(f"\n完成 ok={n_ok} skip={n_skip} 耗时 {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
