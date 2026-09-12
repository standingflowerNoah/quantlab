# -*- coding: utf-8 -*-
"""因子指标批量读取 CLI
========================
把分散在多层存储里的指标聚成一张宽表，支持任意因子子集：

  存储层                          提供指标
  ─────────────────────────────────────────────────────────
  data/lake/factor_audit/*.json   IC5/ICIR5/胜率5/IC20/ICIR20/方向/
                                  PIT verdict/行数/最新日期/覆盖天数
  reports/fdr_factor_ranking.json 多重检验 t_adj/q/HLZ t>3/rank
  reports/factor_eval/index.json  最新评估报告的红绿灯/报告路径
  quantlab.factor.registry        economic_rationale 机制登记（--with-registry）
  data/lake/factor_metric_daily   近 N 日动态 IC/ICIR（--recent，第四数据源；
                                  L0 原子时序，方案 factor_metric_store_proposal）

用法：
  python scripts/factor_eval/batch_metrics.py --names size,amihud_20,sue_i
  python scripts/factor_eval/batch_metrics.py --all --top 30          # 按 |t| 排序前 30
  python scripts/factor_eval/batch_metrics.py --all --out reports/factor_metrics_all.csv
  python scripts/factor_eval/batch_metrics.py --all --recent 60        # 加近60日动态列
  python scripts/factor_eval/batch_metrics.py --all --pool zz1000 --recent 60  # 池内口径
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AUDIT_DIR = ROOT / "data" / "lake" / "factor_audit"
FDR_JSON = ROOT / "reports" / "fdr_factor_ranking.json"
INDEX_JSON = ROOT / "reports" / "factor_eval" / "index.json"

COLUMNS = [
    "factor", "direction",
    "ic1", "icir1", "ic5", "icir5", "ic20", "icir20",       # legacy = rank
    "rank_ic1", "rank_icir1", "rank_ic5", "rank_icir5",
    "rank_ic20", "rank_icir20",
    "pearson_ic1", "pearson_icir1", "pearson_ic5", "pearson_icir5",
    "pearson_ic20", "pearson_icir20",
    "win1", "win5",
    "n_days", "t_adj", "q", "harvey_t3", "fdr_rank", "pit", "n_rows",
    "latest_date", "light", "rationale",
]


def load_audit(name: str) -> dict | None:
    fp = AUDIT_DIR / f"{name}.json"
    if not fp.exists():
        return None
    return json.loads(fp.read_text(encoding="utf-8"))


def load_fdr() -> dict[str, dict]:
    if not FDR_JSON.exists():
        return {}
    rows = json.loads(FDR_JSON.read_text(encoding="utf-8")).get("ranking", [])
    return {r["factor"]: r for r in rows}


def load_latest_reports() -> dict[str, dict]:
    """index.json 里每个因子最新一条报告（md/dashboard 通用）"""
    if not INDEX_JSON.exists():
        return {}
    idx = json.loads(INDEX_JSON.read_text(encoding="utf-8"))
    best: dict[str, dict] = {}
    for e in idx:
        if e.get("type") not in ("factor", "dashboard"):
            continue
        n = e.get("name")
        if n is None or (n in best
                         and best[n]["generated_at"] >= e.get("generated_at", "")):
            continue
        best[n] = e
    return best


def load_rationales() -> dict[str, str]:
    """registry 机制登记（需 import 因子库，较慢，--with-registry 才加载）"""
    sys.path.insert(0, str(ROOT))
    try:
        from quantlab.factor.registry import list_factors  # noqa: PLC0415
        df = list_factors()
        return dict(zip(df["name"], df["economic_rationale"]))
    except Exception as e:  # pragma: no cover
        print(f"[warn] registry 加载失败：{e}", file=sys.stderr)
        return {}


def load_recent(pool: str, n_days: int) -> dict[str, dict]:
    """L0 原子时序第四数据源：每因子最近 n_days 日动态 IC/ICIR/胜率。

    返回 {factor: {recent_ic5, recent_icir5, recent_win5, recent_ic20,
                   recent_icir20, recent_days, recent_date}}；
    与全史快照列并排即可回答"最近 IC 相比全史掉了几档"。
    """
    sys.path.insert(0, str(ROOT))
    try:
        from quantlab.factor.metric_store import read_l0  # noqa: PLC0415
        df = read_l0(pool)
    except Exception as e:  # pragma: no cover
        print(f"[warn] L0 读取失败：{e}", file=sys.stderr)
        return {}
    if df.empty:
        return {}
    import pandas as pd  # noqa: PLC0415
    df = df.sort_values("date")
    last = df.groupby("factor")["date"].max().rename("recent_date")
    recent = df.groupby("factor").tail(n_days)
    g = recent.groupby("factor")

    def _s(s: pd.Series) -> tuple:
        s = s.dropna()
        if len(s) < 5 or s.std() == 0:
            return (None, None, None)
        return (round(float(s.mean()), 4),
                round(float(s.mean() / s.std()), 3),
                round(float((s > 0).mean()), 3))

    out = {}
    for name, gy in g:
        ic5, ir5, w5 = _s(gy["rank_ic5"])
        ic20, ir20, _ = _s(gy["rank_ic20"])
        out[name] = {
            "recent_ic5": ic5, "recent_icir5": ir5, "recent_win5": w5,
            "recent_ic20": ic20, "recent_icir20": ir20,
            "recent_days": int(gy["rank_ic5"].notna().sum()),
            "recent_date": str(last.get(name, ""))[:10],
        }
    return out


def build_row(name: str, fdr: dict, reports: dict,
              rationales: dict | None) -> dict | None:
    a = load_audit(name)
    if a is None:
        return None
    ic = a.get("checks", {}).get("ic", {})
    pit = a.get("checks", {}).get("pit", {})
    f = fdr.get(name, {})
    rep = reports.get(name, {})
    return {
        "factor": name,
        "direction": ic.get("direction"),
        # legacy（= rank，向后兼容）
        "ic1": ic.get("rank_ic1"), "icir1": ic.get("rank_icir1"),
        "ic5": ic.get("ic5"), "icir5": ic.get("icir5"),
        "ic20": ic.get("ic20"), "icir20": ic.get("icir20"),
        # v2 双类型
        "rank_ic1": ic.get("rank_ic1"), "rank_icir1": ic.get("rank_icir1"),
        "rank_ic5": ic.get("rank_ic5"), "rank_icir5": ic.get("rank_icir5"),
        "rank_ic20": ic.get("rank_ic20"), "rank_icir20": ic.get("rank_icir20"),
        "pearson_ic1": ic.get("pearson_ic1"),
        "pearson_icir1": ic.get("pearson_icir1"),
        "pearson_ic5": ic.get("pearson_ic5"),
        "pearson_icir5": ic.get("pearson_icir5"),
        "pearson_ic20": ic.get("pearson_ic20"),
        "pearson_icir20": ic.get("pearson_icir20"),
        "win1": ic.get("rank_win1"), "win5": ic.get("win5"),
        "n_days": (a.get("checks", {}).get("coverage", {}) or {}).get("n_days"),
        "t_adj": f.get("t"), "q": f.get("q"),
        "harvey_t3": f.get("harvey_pass"), "fdr_rank": f.get("rank"),
        "pit": pit.get("status") or a.get("verdict"),
        "n_rows": a.get("n_rows"), "latest_date": a.get("latest_date"),
        "light": rep.get("light"), "rationale": (rationales or {}).get(name),
    }


def fmt_row(r: dict) -> str:
    def c(v, spec=None):
        if v is None:
            return "—"
        if spec is None:
            spec = "{:+.4f}" if isinstance(v, float) else "{}"
        return spec.format(v)
    return (f"{r['factor']:<22} {c(r['direction'],'{:>2d}'):>2} "
            f"{c(r['rank_ic1']):>8} {c(r['pearson_ic1']):>8} "
            f"{c(r['rank_ic5']):>8} {c(r['pearson_ic5']):>8} "
            f"{c(r['rank_ic20']):>8} {c(r['pearson_ic20']):>8} "
            f"{c(r['t_adj'],'{:>5.2f}'):>5} "
            f"{c(r['q'],'{:.4f}'):>7} "
            f"{r['pit'] or '—':>4} {r['light'] or '—'}")


def main() -> None:
    p = argparse.ArgumentParser(description="因子指标批量读取")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--names", help="逗号分隔因子名（支持前缀模糊）")
    g.add_argument("--all", action="store_true", help="全部已审计因子")
    p.add_argument("--top", type=int, default=0, help="按 |t_adj| 取前 N")
    p.add_argument("--with-registry", action="store_true",
                   help="附带机制登记列（需 import 因子库，较慢）")
    p.add_argument("--recent", type=int, default=0, metavar="N",
                   help="附带最近 N 日动态 IC 列（L0 第四数据源）")
    p.add_argument("--pool", default="ashare_ex",
                   help="L0 池（配合 --recent；默认 ashare_ex）")
    p.add_argument("--out", help="导出 CSV 路径（缺省打印控制台）")
    a = p.parse_args()

    if a.all:
        names = sorted(Path(f).stem for f in glob.glob(str(AUDIT_DIR / "*.json")))
    else:
        wanted = [s.strip() for s in a.names.split(",") if s.strip()]
        audited = {Path(f).stem for f in glob.glob(str(AUDIT_DIR / "*.json"))}
        names = []
        for w in wanted:
            hit = [n for n in audited if n == w or n.startswith(w)]
            names.extend(hit if hit else [w])

    fdr = load_fdr()
    reports = load_latest_reports()
    rationales = load_rationales() if a.with_registry else None

    rows = [r for n in names if (r := build_row(n, fdr, reports, rationales))]
    missing = [n for n in names if load_audit(n) is None]
    if missing:
        print(f"[warn] 无审计 JSON（先跑审计）：{', '.join(missing)}",
              file=sys.stderr)

    cols = list(COLUMNS)
    if a.recent:
        rec = load_recent(a.pool, a.recent)
        for r in rows:
            r.update(rec.get(r["factor"], {}))
        cols += ["recent_ic5", "recent_icir5", "recent_win5",
                 "recent_ic20", "recent_icir20", "recent_days",
                 "recent_date"]

    if a.top:
        rows.sort(key=lambda r: -(r["t_adj"] or 0))

    if a.out:
        import pandas as pd  # noqa: PLC0415
        df = pd.DataFrame(rows, columns=cols)
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"已导出 {len(df)} 行 × {len(cols)} 列 -> {out}")
    else:
        print(f"{'factor':<22} dr   ric1    pic1     ric5    pic5 "
              f"   ric20   pic20  t_adj       q  pit light")
        print("-" * 112)
        for r in rows[: a.top or len(rows)]:
            print(fmt_row(r))
        note = (f"\n共 {len(rows)} 因子；ric*=Rank IC、pic*=Pearson IC，"
                "h=1/5/20 三周期，来自审计快照 checks.ic（v2 双类型）；"
                "t_adj/q 来自 FDR 批表，pit=审计结论，light=最新评估红绿灯")
        if a.recent:
            note += (f"；recent*=L0 近 {a.recent} 日动态"
                     f"（pool={a.pool}，CSV 含全部列）")
        print(note)


if __name__ == "__main__":
    main()
