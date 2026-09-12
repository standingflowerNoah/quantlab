"""全库因子多重检验校正（BH-FDR）——因子评价框架 P0-1 落地
================================================================
背景：因子库 311 个已审计因子，若直接按 IC/ICIR 排名，排名近似
"搜索了多少个因子"的噪声排名（Hou-Xue-Zhang 复现失败率 65% /
Harvey-Liu-Zhu 建议 t>3.0 的教训）。本脚本对全库因子的
IC20 t 统计量做 Benjamini-Hochberg FDR 校正，产出 q 值重排表。

统计口径：
  t_raw = |ICIR20| × √n_days      （ICIR=mean/std ⇒ t=mean/(std/√N)）
  ⚠️ IC20 序列相邻交易日共享 19/20 收益窗口 + 因子值高度重叠，
  一阶自相关 ρ≈0.9（文献常用值），√N 严重高估显著性——
  本项目"五次 IC 好≠组合好"实证与此一致。
  自相关折减：N_eff = N(1-ρ)/(1+ρ)，ρ=0.9 → N_eff ≈ N/19，
  t_adj = |ICIR20| × √N_eff ≈ t_raw/4.36。
  p = 2·(1-Φ(|t_adj|))，BH 校正得 q。主口径 = t_adj；
  t_raw 仅作对照（其 q 值会放出 ~90% 因子，无甄别力）。

⚠️ 局限（读表须知）：
  1. t 统计依赖审计时的 IC 快照，非同窗重算——跨因子窗口不完全一致
     （sue_i/hf_amihud_20 等短历史因子 n_days 小，t 被自然惩罚，合理）。
  2. BH 假设检验统计量独立或 PRDS；同库因子高度相关（312 因子来自
     73 个去重 Alpha191 + 风格族），校正偏保守，幸存者只会更少不会更多。
  3. 隐含搜索空间 = 审计在库因子（311），不含历史上被否决/证伪的因子
     （overnight_mom_20、dragon_net_20 等已删项未计入，真实搜索空间更大）。

输出：reports/fdr_factor_ranking.{json,md}
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = ROOT / "data" / "lake" / "factor_audit"
IC_CACHE_CSV = ROOT / "reports" / "alpha191_ic.csv"
OUT_JSON = ROOT / "reports" / "fdr_factor_ranking.json"
OUT_MD = ROOT / "reports" / "fdr_factor_ranking.md"

RHO = 0.9                         # IC20 序列一阶自相关（重叠 19/20 收益窗口）
DEFLATE = (1 - RHO) / (1 + RHO)   # ≈ 0.0526 → N_eff ≈ N/19


def norm_sf_abs(t: float) -> float:
    """双尾 p 值"""
    return 2.0 * stats.norm.sf(abs(t))


def main() -> None:
    # ── 1. 汇集全库 IC 统计 ──────────────────────────────────────
    cache = {}
    if IC_CACHE_CSV.exists():
        c = pd.read_csv(IC_CACHE_CSV)
        cache = {r["factor"]: int(r["n_days"]) for _, r in c.iterrows()}

    rows = []
    for fp in sorted(AUDIT_DIR.glob("*.json")):
        d = json.loads(fp.read_text(encoding="utf-8"))
        name = d.get("factor") or fp.stem
        ic = (d.get("checks") or {}).get("ic") or {}
        cov = (d.get("checks") or {}).get("coverage") or {}
        ic20, icir20 = ic.get("ic20"), ic.get("icir20")
        if ic20 is None or icir20 is None:
            rows.append({"factor": name, "has_ic": False,
                         "verdict": d.get("verdict", "?")})
            continue
        n_days = ic.get("n_days")
        if n_days is None:
            n_days = cache.get(name) or cov.get("n_days")
        rows.append({
            "factor": name, "has_ic": True,
            "ic20": float(ic20), "icir20": float(icir20),
            "n_days": int(n_days) if n_days else None,
            "direction": ic.get("direction", 1),
            "verdict": d.get("verdict", "?"),
        })

    df = pd.DataFrame(rows)
    no_ic = df[~df["has_ic"]]
    d = df[df["has_ic"]].copy()

    # ── 2. t 统计量与 BH-FDR（主口径 = 自相关折减后 t_adj）──────
    d = d.dropna(subset=["n_days"])
    d["t_raw"] = d["icir20"].abs() * d["n_days"].pow(0.5)
    d["n_eff"] = (d["n_days"] * DEFLATE).round().astype(int)
    d["t"] = d["icir20"].abs() * d["n_eff"].pow(0.5)      # t_adj 主口径
    d["p"] = d["t"].map(norm_sf_abs)
    d = d.sort_values("p").reset_index(drop=True)
    m = len(d)                                    # 检验总数
    d["rank"] = d.index + 1
    d["q_raw"] = d["p"] * m / d["rank"]
    # BH 单调化：q_i = min_{j>=i} q_raw_j
    d["q"] = d["q_raw"][::-1].cummin()[::-1]
    d["q"] = d["q"].clip(upper=1.0)
    d["harvey_pass"] = d["t"] > 3.0               # HLZ 2013 门槛（t_adj 口径）

    # ── 3. 分层汇总 ────────────────────────────────────────────
    layers = {}
    for thr in (0.05, 0.10, 0.25, 0.50):
        sub = d[d["q"] < thr]
        layers[f"q<{thr}"] = {
            "n": int(len(sub)),
            "top20": sub.nsmallest(20, "q")["factor"].tolist(),
        }
    layers["t>3.0 (HLZ)"] = {
        "n": int(d["harvey_pass"].sum()),
        "top20": d[d["harvey_pass"]].nlargest(20, "t")["factor"].tolist(),
    }

    # 生产/核心因子的位置
    core = ["size", "amihud_20", "sue_i", "hf_amihud_20", "overnight_mom_20"]
    core_pos = []
    for f in core:
        r = d[d["factor"] == f]
        if len(r):
            r = r.iloc[0]
            core_pos.append({
                "factor": f, "ic20": round(r["ic20"], 4),
                "t": round(r["t"], 2), "q": round(r["q"], 4),
                "rank_by_p": int(r["rank"]),
                "harvey_pass": bool(r["harvey_pass"]),
            })

    # ── 4. 输出 ────────────────────────────────────────────────
    out = {
        "generated": pd.Timestamp.now().isoformat(timespec="seconds"),
        "n_audited": int(len(df)), "n_with_ic": int(len(d)),
        "n_without_ic": int(len(no_ic)),
        "without_ic_factors": no_ic["factor"].tolist(),
        "layers": layers, "core_factors": core_pos,
        "ranking": d.assign(
            ic20=d["ic20"].round(4), icir20=d["icir20"].round(4),
            t_raw=d["t_raw"].round(2), n_eff=d["n_eff"],
            t=d["t"].round(2), p=d["p"].map(lambda x: f"{x:.2e}"),
            q=d["q"].round(4),
        )[["factor", "ic20", "icir20", "n_days", "n_eff", "t_raw", "t",
           "p", "q", "harvey_pass", "verdict", "rank"]].to_dict("records"),
    }
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                        encoding="utf-8")

    lines = [
        "# 全库因子 BH-FDR 多重检验校正（P0-1）",
        "",
        f"- 审计在库 {len(df)} 个，有 IC20 统计 {len(d)} 个，缺 IC {len(no_ic)} 个",
        f"- 缺 IC 因子：{', '.join(no_ic['factor'].tolist()) or '无'}",
        f"- 检验总数 m={m}；t=|ICIR20|·√n_days；BH-FDR 单调化 q 值",
        "",
        "## 分层幸存清单",
        "",
    ]
    for k, v in layers.items():
        lines.append(f"### {k}：{v['n']} 个因子幸存")
        if v["n"]:
            lines.append("- " + ", ".join(v["top20"]))
        lines.append("")
    lines += ["## 核心/生产因子位置", "",
              "| 因子 | IC20 | t | q | p 排名 | t>3.0 |", "|---|---|---|---|---|---|"]
    for c in core_pos:
        lines.append(f"| {c['factor']} | {c['ic20']} | {c['t']} | "
                     f"{c['q']} | {c['rank_by_p']}/{m} | "
                     f"{'✅' if c['harvey_pass'] else '❌'} |")
    lines += ["", "## 全量 q 值排序表（前 80）", "",
             "| rank | factor | IC20 | ICIR20 | n_days | t_raw | t_adj | q | t>3 | verdict |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in out["ranking"][:80]:
        lines.append(f"| {r['rank']} | {r['factor']} | {r['ic20']} | "
                     f"{r['icir20']} | {r['n_days']} | {r['t_raw']} | "
                     f"{r['t']} | {r['q']} | "
                     f"{'✅' if r['harvey_pass'] else '❌'} | {r['verdict']} |")
    lines += ["", f"*完整 {m} 行见 {OUT_JSON.name}。主口径 t_adj = "
              f"|ICIR20|·√N_eff，N_eff=N·(1-ρ)/(1+ρ)，ρ=0.9（IC20 相邻序列"
              "重叠 19/20 收益窗口）。局限见脚本 docstring"
              "（窗口非同窗、BH 偏保守、隐含搜索空间=在库因子）。*"]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    # 控制台摘要
    print(f"审计在库 {len(df)} / 有 IC {len(d)} / 缺 IC {len(no_ic)}")
    for k, v in layers.items():
        print(f"  {k:14s} {v['n']:3d} 个幸存")
    print("\n核心因子位置：")
    for c in core_pos:
        print(f"  {c['factor']:20s} IC20 {c['ic20']:+.4f}  t {c['t']:6.2f}  "
              f"q {c['q']:.4f}  rank {c['rank_by_p']}/{m}  "
              f"{'✓HLZ' if c['harvey_pass'] else '✗HLZ'}")
    print(f"\n已写入 {OUT_JSON}")
    print(f"已写入 {OUT_MD}")


if __name__ == "__main__":
    main()
